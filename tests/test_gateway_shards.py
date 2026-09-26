"""Real loopback WS groups and deterministic robot-wide Session budgets."""
import asyncio
import copy
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_lifecycle import plugin_module as plugin_module
from test_transport_http import MappedSession, upstream
from test_transport_receive import (
    HELLO,
    READY,
    FakeGatewayHTTP,
    FakeWS,
    StepClock,
    gateway_document,
)

from v2.errors import V2Error
from v2.models import InstanceKey
from v2.protocol import RawEnvelope, RequestSpec
from v2.transport.http import HTTPTransport
from v2.transport.inbox import Ingress, RawInbox
from v2.transport.shards import GatewayGroup, IdentifyBudget, ShardIngress, gateway_info
from v2.transport.websocket import Gateway


@pytest.mark.parametrize("key,value", [("shards", v) for v in (None, 0, -1, True, "2", 1025)] + [
    ("total", True), ("total", 0), ("remaining", -1), ("remaining", 101), ("remaining", False),
    ("reset_after", 0), ("reset_after", 86400001), ("reset_after", "5000"),
    ("max_concurrency", 0), ("max_concurrency", True), ("max_concurrency", -1)])
def test_gateway_contract_never_coerces_or_falls_back(key, value):
    data = gateway_document("wss://api.sgroup.qq.com/websocket")
    (data if key == "shards" else data["session_start_limit"])[key] = value
    with pytest.raises(V2Error) as exc:
        gateway_info(data)
    assert exc.value.code == "invalid_gateway"


class Discovery:
    def __init__(self, config, *, remaining=100, concurrency=1, reset=20000):
        self.identity = InstanceKey.from_config(config)
        self.data = gateway_document("wss://api.sgroup.qq.com/websocket", remaining=remaining, concurrency=concurrency, reset=reset)
        self.calls = []

    async def request(self, spec):
        self.calls.append(spec.url)
        assert spec.url == "https://api.bot.qq.com/gateway/bot"
        return SimpleNamespace(data=copy.deepcopy(self.data))


async def test_shared_budget_five_second_window_remaining_reset_and_cancellation(config):
    clock = StepClock()
    budget = IdentifyBudget(clock=lambda: clock.now, sleep=clock.sleep)
    first, second = Discovery(config, remaining=2, concurrency=1), Discovery(config, remaining=100)
    await budget.acquire(first, lambda: None)
    pending = asyncio.create_task(budget.acquire(second, lambda: None))
    await clock.tick()
    await pending
    assert clock.now == 5 and budget.remaining == 0 and second.calls == []
    pending = asyncio.create_task(budget.acquire(first, lambda: None))
    first.data["session_start_limit"]["remaining"] = 3
    await clock.tick()
    await pending
    assert clock.now == 20 and budget.remaining == 2 and len(first.calls) == 2
    pending = asyncio.create_task(budget.acquire(second, lambda: None))
    delay, future = await asyncio.wait_for(clock.waits.get(), 1)
    assert delay == 5
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert budget.remaining == 2 and future.cancelled() and not budget.lock.locked()


async def test_handshake_completion_sets_rate_window_and_uncertainty_does_not_refund(config):
    clock = StepClock()
    budget = IdentifyBudget(clock=lambda: clock.now, sleep=clock.sleep)
    http = Discovery(config)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def uncertain():
        async with budget.session_start(http, lambda: None):
            entered.set()
            await finish.wait()
    task = asyncio.create_task(uncertain())
    await entered.wait()
    clock.now = 3
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert budget.remaining == 99 and list(budget.starts) == [3]
    pending = asyncio.create_task(budget.acquire(http, lambda: None))
    await clock.tick()
    await pending
    assert clock.now == 8 and budget.remaining == 98


async def test_resume_with_no_budget_bypasses_wait_and_keeps_real_seq(config, tmp_path):
    inbox = RawInbox(tmp_path / "raw")
    ingress = Ingress(inbox, "stable-owner")
    ingress.start()
    cursor = ShardIngress(ingress)
    budget = IdentifyBudget()
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [FakeWS([HELLO, {"op": 0, "s": 3, "t": "RESUMED", "d": {}}, 4014])])
    budget.info = gateway_document("wss://api.sgroup.qq.com/websocket", remaining=0)
    budget.remaining, budget.reset_at = 0, 0
    gateway = Gateway(http, cursor, budget=budget, attempts=1)
    gateway.session_id, cursor.last_sequence = "observed-session", 2
    socket = http.sockets[0]
    try:
        with pytest.raises(V2Error) as exc:
            await gateway.run()
        assert exc.value.business_code == 4014
        assert socket.sent[0] == {"op": 6, "d": {"token": "QQBot fixture-access-token", "session_id": "observed-session", "seq": 2}}
        assert budget.remaining == 0 and not budget.starts
    finally:
        await gateway.close()
        await ingress.close()
        inbox.close()


async def test_independent_cursors_advance_only_on_durable_accept(config, tmp_path, monkeypatch):
    inbox = RawInbox(tmp_path / "raw")
    ingress = Ingress(inbox, "unchanged-settings-key")
    ingress.start()
    left, right = ShardIngress(ingress), ShardIngress(ingress)
    try:
        await left.accept(RawEnvelope({**READY, "s": 100}, 1))
        await right.accept(RawEnvelope({**READY, "s": 2, "d": {"session_id": "other", "user": {"id": "bot"}}}, 1))
        def reject(*args):
            raise V2Error("inbox_full", "fixture full")
        monkeypatch.setattr(inbox, "accept", reject)
        with pytest.raises(V2Error):
            await right.accept(RawEnvelope({**READY, "s": 3}, 2))
        assert left.last_sequence == 100 and right.last_sequence == 2
        assert inbox.count("unchanged-settings-key") == 2
    finally:
        await ingress.close()
        inbox.close()


@pytest.fixture
async def group_env(config, tmp_path):
    handshakes, beats = asyncio.Queue(), asyncio.Queue()
    sockets, requests = {}, []
    messages = []
    suggestion = [3]
    info = {"url": ""}
    async def handle(request):
        requests.append((request.method, request.path))
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "fixture-group-token", "expires_in": 7200})
        if request.path == "/gateway/bot":
            assert request.headers["Authorization"] == "QQBot fixture-group-token"
            return web.json_response(gateway_document(info["url"], shards=suggestion[0]))
        if request.path == "/check":
            assert request.headers["Authorization"] == "QQBot fixture-group-token"
            return web.json_response({"alive": True})
        if request.path in {"/v2/groups/group-one/messages", "/channels/channel-one/messages"}:
            assert request.method == "POST" and request.headers["Authorization"] == "QQBot fixture-group-token"
            messages.append((request.path, await request.json()))
            return web.json_response({"id": f"reply-{len(messages)}", "timestamp": "2027-01-15T08:00:00+00:00"})
        assert request.path == "/ws" and "Authorization" not in request.headers and "Cookie" not in request.headers
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(HELLO)
        login = await ws.receive_json()
        shard = login["d"]["shard"][0] if login["op"] == 2 else int(login["d"]["session_id"].split("-")[-1])
        sockets[shard] = ws
        handshakes.put_nowait(login)
        seq = 100 if shard == 0 else shard
        await ws.send_json({"op": 0, "s": seq, "t": "READY" if login["op"] == 2 else "RESUMED",
            "d": {"session_id": f"session-{shard}", "user": {"id": "real-bot"}}})
        await ws.send_json({"op": 1})
        async for msg in ws:
            value = msg.json()
            if value["op"] == 1:
                beats.put_nowait((shard, value["d"]))
                await ws.send_json({"op": 11})
        return ws
    inbox = RawInbox(tmp_path / "raw")
    ingress = Ingress(inbox, "original-owner")
    ingress.start()
    async with upstream(handle) as base:
        info["url"] = base.replace("http:", "ws:") + "/ws"
        http = HTTPTransport(InstanceKey.from_config({**config, "shard_mode": "auto"}), config["secret"], session_factory=lambda: MappedSession(base))
        group = GatewayGroup(http, ingress, IdentifyBudget())
        try:
            yield SimpleNamespace(group=group, http=http, inbox=inbox, ingress=ingress, sockets=sockets,
                handshakes=handshakes, beats=beats, suggestion=suggestion, requests=requests, messages=messages, config=config)
        finally:
            await group.close()
            await http.close()
            await ingress.close()
            inbox.close()


async def start_group(e, count):
    e.suggestion[0] = count
    task = asyncio.create_task(e.group.run())
    logins = [await asyncio.wait_for(e.handshakes.get(), 2) for _ in range(count)]
    beats = [await asyncio.wait_for(e.beats.get(), 2) for _ in range(count)]
    return task, logins, beats


@pytest.mark.parametrize("count", [2, 9])
async def test_auto_group_opens_every_shard_without_starving_http_or_token(group_env, count):
    e = group_env
    task = None
    try:
        task, logins, beats = await start_group(e, count)
        assert sorted(x["d"]["shard"] for x in logins) == [[i, count] for i in range(count)]
        assert all(x["op"] == 2 for x in logins)
        assert sorted(beats) == [(i, 100 if i == 0 else i) for i in range(count)]
        assert e.group.online and e.group.status()["connected"] == count
        assert e.group.status()["recommended"] == e.group.status()["planned"] == count
        e.http._expires = 0
        responses = await asyncio.wait_for(asyncio.gather(*(e.http.request(RequestSpec("production", "GET", "/check")) for _ in range(6))), 2)
        assert all(r.data == {"alive": True} for r in responses)
        assert e.requests.count(("POST", "/app/getAppAccessToken")) == 2
        assert e.requests.count(("GET", "/gateway/bot")) == 1
        assert e.http.session.inner.connector is not e.group.session.connector
        assert e.group.session.connector.limit == 32
        from astrbot.core.utils.http_ssl import build_ssl_context_with_certifi
        assert e.group.session.connector._ssl is build_ssl_context_with_certifi()
        assert e.group.session.connector._ssl.check_hostname
    finally:
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await e.group.close()
    assert not e.group.tasks and e.group.session.closed
    assert all(not g.tasks and g.ws is None for g in e.group.gateways)
    assert e.inbox.count("original-owner") == count


async def test_manual_group_ignores_recommendation_but_reports_it(group_env):
    e = group_env
    e.http.identity = InstanceKey.from_config({**e.config, "shard": [2, 5]})
    task = asyncio.create_task(e.group.run())
    try:
        login = await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert login["d"]["shard"] == [2, 5] and e.group.online
        assert e.group.status()["recommended"] == 3 and e.group.status()["planned"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("count,code", [(0, "invalid_gateway"), (True, "invalid_gateway"), (33, "shard_capacity")])
async def test_invalid_or_unsupported_auto_group_starts_no_sockets(group_env, count, code):
    e = group_env
    e.suggestion[0] = count
    with pytest.raises(V2Error) as exc:
        await e.group.run()
    assert exc.value.code == code and e.group.session is None and e.group.status()["state"] == "failed"
    assert not e.sockets and not e.group.tasks


async def test_one_shard_resume_then_identify_preserves_others_and_aggregates_failure(group_env, monkeypatch):
    e = group_env
    original = Gateway.__init__
    waiting, release = asyncio.Event(), asyncio.Event()
    async def short_backoff(delay):
        if delay < 15:
            waiting.set()
            await release.wait()
        else:
            await asyncio.Event().wait()
    def init(self, *args, **kwargs):
        original(self, *args, **kwargs, sleep=short_backoff, jitter=lambda: 0)
    monkeypatch.setattr(Gateway, "__init__", init)
    task = None
    try:
        task, _, _ = await start_group(e, 2)
        await e.sockets[1].close(code=4009)
        await asyncio.wait_for(waiting.wait(), 2)
        assert not e.group.online and e.group.state == "degraded" and e.group.gateways[0].online
        release.set()
        resume = await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert resume["op"] == 6 and resume["d"]["seq"] == 1 and resume["d"]["session_id"] == "session-1"
        assert e.group.budget.remaining == 98
        await e.sockets[1].close(code=4006)
        fresh = await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert fresh["op"] == 2 and fresh["d"]["shard"] == [1, 2] and e.group.budget.remaining == 97
        assert e.group.gateways[0].ingress.last_sequence == 100
        await e.sockets[1].close(code=4014)
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(task, 2)
        assert exc.value.business_code == 4014 and e.group.state == "failed"
        assert not e.group.online and e.group.session.closed and not e.group.tasks
    finally:
        release.set()
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_host_reload_group_retains_raw_owner_and_leaves_no_children(group_env, plugin_module, tmp_path, monkeypatch):
    import importlib
    import time

    from astrbot.core.platform.manager import PlatformManager
    from test_lifecycle import context
    from test_messaging_state import chat_payload
    from test_onboarding import HostConfig

    e = group_env
    cfg = {**e.config, "shard_mode": "auto"}
    host = HostConfig(tmp_path / "host.json", cfg)
    host["platform_settings"] = {}
    host.save_config()
    ctx, queue = context(), asyncio.Queue()
    ctx.get_config = lambda *args: host
    ctx.platform_manager = PlatformManager(host, queue)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    module = importlib.import_module(plugin_module.__package__ + ".v2.transport.http")
    monkeypatch.setattr(module.HTTPTransport, "_make_session", lambda self: e.http._factory())
    instances = []
    try:
        key = InstanceKey.from_config(cfg).settings_key
        old = chat_payload(message_id="retained-before-shards", timestamp=time.time(), text="hello")
        owner.inbox.accept(key, RawEnvelope(old, time.time()))
        for _ in range(3):
            await ctx.platform_manager.reload(host["platform"][0])
            instance = next(iter(owner.instances))
            instances.append(instance)
            logins = [await asyncio.wait_for(e.handshakes.get(), 2) for _ in range(3)]
            for _ in range(3):
                await asyncio.wait_for(e.beats.get(), 2)
            assert all(login["op"] == 2 for login in logins), "reload must not restore Session/seq"
            assert instance.runtime_status()["online"] and instance.gateway.status()["planned"] == 3
            assert instance.identity.settings_key == key
            assert len({id(g.ingress) for g in instance.gateway.gateways}) == 3
            assert all(g.ingress.ingress is instance.ingress for g in instance.gateway.gateways)
            if len(instances) == 1:
                event = await asyncio.wait_for(queue.get(), 2)
                assert event.raw_data == old and event.session_id == "group-one"
                event.cleanup_temporary_local_files()
            else:
                assert queue.empty()
            assert all(i.gateway.session.closed and i.http.session.closed and i.consumer.task.done() for i in instances[:-1])
        assert e.requests.count(("GET", "/gateway/bot")) == 1
        assert instances[-1].gateway.budget.remaining == 91
    finally:
        await owner.terminate()
    assert not owner.instances and not owner.delivery_slots.events and not ctx.platform_manager.get_insts()
    assert all(i.gateway.session.closed and not i.gateway.tasks and i.ingress.worker.done() for i in instances)
