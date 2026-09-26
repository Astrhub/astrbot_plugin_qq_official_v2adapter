"""Finite shard failures stay local; shared fatal failures still close the group."""
import asyncio
import importlib
import time

import pytest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.manager import PlatformManager
from astrbot.core.utils.metrics import Metric
from test_gateway_shards import group_env as group_env
from test_lifecycle import context
from test_lifecycle import plugin_module as plugin_module
from test_messaging_state import chat_payload
from test_onboarding import HostConfig

from v2.errors import V2Error
from v2.transport.websocket import Gateway, GatewayClosed


class FaultSocket:
    def __init__(self, session, gate, broken):
        self.session, self.gate, self.broken = session, gate, broken
        self.calls = 0

    async def ws_connect(self, *args, **kwargs):
        await self.gate.wait()
        self.calls += 1
        if self.broken():
            raise OSError("synthetic shard connect failure")
        return await self.session.ws_connect(*args, **kwargs)


async def retry_tick(delay):
    if delay <= 15:
        await asyncio.sleep(0)
    else:
        await asyncio.Event().wait()


def observe_terminals(monkeypatch, gateway_class):
    results = asyncio.Queue()
    original = gateway_class.run

    async def observed(self):
        try:
            await original(self)
        except Exception as exc:
            results.put_nowait((self, exc))
            raise

    monkeypatch.setattr(gateway_class, "run", observed)
    return results


async def test_exhausted_shard_preserves_host_delivery_reply_and_admin_reload(group_env, plugin_module, tmp_path, monkeypatch):
    e = group_env
    e.suggestion[0] = 2
    host = HostConfig(tmp_path / "host.json", {**e.config, "shard_mode": "auto"})
    host["platform_settings"] = {}
    host.save_config()
    ctx, queue = context(), asyncio.Queue()
    ctx.get_config = lambda *args: host
    ctx.platform_manager = PlatformManager(host, queue)
    http_module = importlib.import_module(plugin_module.__package__ + ".v2.transport.http")
    ws_module = importlib.import_module(plugin_module.__package__ + ".v2.transport.websocket")
    monkeypatch.setattr(http_module.HTTPTransport, "_make_session", lambda self: e.http._factory())
    async def no_metrics(**kwargs):
        pass
    monkeypatch.setattr(Metric, "upload", no_metrics)
    gate, broken, faults = asyncio.Event(), [True], []
    original_init = ws_module.Gateway.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs, sleep=retry_tick, jitter=lambda: 0)
        if self.shard[0] == 1:
            socket = FaultSocket(self.ws_session, gate, lambda: broken[0])
            faults.append(socket)
            self.ws_session = socket
    monkeypatch.setattr(ws_module.Gateway, "__init__", init)
    terminals = observe_terminals(monkeypatch, ws_module.Gateway)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    instances = []
    try:
        await ctx.platform_manager.reload(host["platform"][0])
        instance = next(iter(owner.instances))
        instances.append(instance)
        assert (await asyncio.wait_for(e.handshakes.get(), 2))["d"]["shard"] == [0, 2]
        await asyncio.wait_for(e.beats.get(), 2)
        gate.set()
        failed, error = await asyncio.wait_for(terminals.get(), 2)
        assert error.code == "reconnect_exhausted" and failed.attempts == 6 and faults[0].calls == 6
        await e.sockets[0].send_json(chat_payload(message_id="healthy-after-peer-exhausted", timestamp=time.time(), text="hello"))
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.message_obj.message_id == "healthy-after-peer-exhausted"
        await event.send(MessageChain([Plain("still receiving")]))
        event.cleanup_temporary_local_files()
        assert e.messages == [("/v2/groups/group-one/messages", {"content": "still receiving", "msg_type": 0,
            "msg_id": "healthy-after-peer-exhausted", "msg_seq": 1})]
        assert instance.http.session.calls[-1][1] == "https://api.bot.qq.com/v2/groups/group-one/messages"
        status = instance.runtime_status()
        assert not instance._run_task.done() and status["state"] == "degraded" and not status["online"]
        assert status["message_ready"] and status["ws_available"]
        group = status["gateway_group"]
        assert group["planned"] == 2 and group["connected"] == 1 and len(group["shards"]) == 2
        node = next(s for s in group["shards"] if s["index"] == 1)
        assert node["count"] == 2 and node["state"] == "failed" and node["recovery"] == "reload_required"
        assert node["failure"]["code"] == "reconnect_exhausted"
        assert node["failure"]["details"] == {"attempts": 6, "last_transport_failure": {"code": "gateway_io_failure"}}
        assert status["last_transport_failure"]["code"] == "reconnect_exhausted"
        assert failed.ws is None and not failed.tasks and instance.gateway.budget.remaining == 93
        assert faults[0].calls == 6 and e.handshakes.empty() and not instance.gateway.session.closed
        assert sum(not t.done() for t in instance.gateway.tasks) == 1
        broken[0] = False
        await ctx.platform_manager.reload(host["platform"][0])
        replacement = next(iter(owner.instances))
        instances.append(replacement)
        logins = [await asyncio.wait_for(e.handshakes.get(), 2) for _ in range(2)]
        for _ in range(2):
            await asyncio.wait_for(e.beats.get(), 2)
        assert sorted(login["d"]["shard"] for login in logins) == [[0, 2], [1, 2]]
        assert all(login["op"] == 2 for login in logins)
        assert replacement.identity.generation != instance.identity.generation and replacement.runtime_status()["online"]
        assert replacement.gateway.budget is instance.gateway.budget and replacement.gateway.budget.remaining == 91
        assert e.requests.count(("GET", "/gateway/bot")) == 1 and faults[0].calls == 6
        assert instance.gateway.session.closed and instance.http.closed and instance.consumer.task.done()
        assert not instance.gateway.tasks and all(g.ws is None and not g.tasks for g in instance.gateway.gateways)
    finally:
        gate.set()
        await owner.terminate()
    assert not owner.instances and not ctx.platform_manager.get_insts() and not owner.delivery_slots.events
    assert all(i._run_task.done() and i.gateway.session.closed and i.ingress.worker.done() for i in instances)
    assert all(not g.tasks and g.ws is None for i in instances for g in i.gateway.gateways)


@pytest.mark.parametrize("peer_state", ["connecting", "backoff"])
async def test_exhausted_shard_does_not_cancel_pending_peer(group_env, monkeypatch, peer_state):
    e = group_env
    e.suggestion[0] = 2
    peer_release, fail_gate = asyncio.Event(), asyncio.Event()
    fail_gate.set()
    original_init, original_connect = Gateway.__init__, Gateway._connect
    faults = {}
    async def peer_sleep(delay):
        if delay <= 15:
            await peer_release.wait()
        else:
            await asyncio.Event().wait()
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs, sleep=peer_sleep if kwargs["shard"][0] == 1 else retry_tick, jitter=lambda: 0)
        index = self.shard[0]
        faults[index] = FaultSocket(self.ws_session, fail_gate,
            lambda: index == 0 or peer_state == "backoff" and faults[index].calls == 1)
        self.ws_session = faults[index]
    async def connect(self):
        if self.shard[0] == 1 and peer_state == "connecting":
            await peer_release.wait()
        await original_connect(self)
    monkeypatch.setattr(Gateway, "__init__", init)
    monkeypatch.setattr(Gateway, "_connect", connect)
    terminals = observe_terminals(monkeypatch, Gateway)
    task = asyncio.create_task(e.group.run())
    try:
        failed, error = await asyncio.wait_for(terminals.get(), 2)
        assert failed.shard == (0, 2) and error.code == "reconnect_exhausted" and faults[0].calls == 6
        peer_release.set()
        login = await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert login["d"]["shard"] == [1, 2]
        assert not task.done() and e.group.state == "degraded" and not e.group.online and e.group.available
        assert e.group.status()["planned"] == 2 and faults[0].calls == 6
    finally:
        peer_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await e.group.close()
    assert e.group.session.closed and not e.group.tasks and all(g.ws is None and not g.tasks for g in e.group.gateways)


async def test_all_shards_exhaust_and_close_without_task_restarts(group_env, monkeypatch):
    e = group_env
    e.suggestion[0] = 2
    gate = asyncio.Event()
    gate.set()
    original_init = Gateway.__init__
    faults = []
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs, sleep=retry_tick, jitter=lambda: 0)
        self.ws_session = FaultSocket(self.ws_session, gate, lambda: True)
        faults.append(self.ws_session)
    monkeypatch.setattr(Gateway, "__init__", init)
    task = asyncio.create_task(e.group.run())
    with pytest.raises(V2Error) as exc:
        await asyncio.wait_for(task, 2)
    assert exc.value.code == "reconnect_exhausted" and e.group.state == "failed"
    assert [f.calls for f in faults] == [6, 6] and e.group.budget.remaining == 88
    assert all(s["state"] == "failed" and s["failure"]["code"] == "reconnect_exhausted" for s in e.group.status()["shards"])
    assert e.group.session.closed and not e.group.tasks and not e.group.available
    assert all(g.ws is None and not g.tasks for g in e.group.gateways)


@pytest.mark.parametrize("failure", [4010, 4011, 4012, 4013, 4014, 4914, 4915, "invalid_gateway", "stale_generation", "unsupported_environment", "program_error"])
async def test_non_exhaustion_failure_still_terminates_healthy_group(group_env, monkeypatch, failure):
    e = group_env
    e.suggestion[0] = 2
    release = asyncio.Event()
    original_connect = Gateway._connect
    error = (GatewayClosed(failure) if type(failure) is int else RuntimeError("synthetic program error")
             if failure == "program_error" else V2Error(failure, "synthetic group failure"))
    async def connect(self):
        if self.shard[0] == 1:
            await release.wait()
            raise error
        await original_connect(self)
    monkeypatch.setattr(Gateway, "_connect", connect)
    task = asyncio.create_task(e.group.run())
    try:
        await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert e.group.available
        release.set()
        with pytest.raises(RuntimeError) as exc:
            await asyncio.wait_for(task, 2)
        assert exc.value is error and e.group.state == "failed"
        assert not e.group.tasks and e.group.session.closed and not e.group.available
        assert all(g.ws is None and not g.tasks for g in e.group.gateways)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
