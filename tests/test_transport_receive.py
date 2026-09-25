import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict

from v2.errors import V2Error
from v2.models import InstanceKey
from v2.protocol import RawEnvelope
from v2.transport.inbox import Ingress, RawInbox
from v2.transport.webhook import Webhook, signing_key
from v2.transport.websocket import Gateway


@pytest.fixture
def inbox(tmp_path):
    store = RawInbox(tmp_path / "inbox.sqlite3")
    yield store
    store.close()


def event(id="outer", seq=2):
    return {"op": 0, "s": seq, "id": id, "t": "FUTURE_EVENT", "d": {"id": "not-message", "extra": [1, 2]}}


async def test_durable_intake_restart_scope_and_capacity(tmp_path):
    path = tmp_path / "inbox.sqlite3"
    store = RawInbox(path, max_rows=2)
    ingress = Ingress(store, "app-production")
    ingress.start()
    raw = RawEnvelope.parse(json.dumps(event()).encode(), now=123)
    try:
        assert await ingress.accept(raw)
        assert not await ingress.accept(raw)
        assert ingress.last_sequence == 2
        assert store.pending("another-app") == []
        assert store.pending("app-production")[0]["payload"] == raw.payload
        assert store.accept("another-app", raw)
        with pytest.raises(V2Error):
            await ingress.accept(RawEnvelope.parse(json.dumps(event("new", 99)).encode()))
        assert ingress.last_sequence == 2
    finally:
        await ingress.close()
        store.close()
    recovered = RawInbox(path)
    try:
        assert recovered.count("app-production") == 1
        fresh = Ingress(recovered, "app-production")
        assert fresh.last_sequence is None  # Never resume from an in-memory reception tail.
        receipt = recovered.pending("app-production")[0]["receipt"]
        recovered.acknowledge("another-app", receipt)
        assert recovered.count("app-production") == 1
        recovered.acknowledge("app-production", receipt)
        assert recovered.count("app-production") == 0
    finally:
        recovered.close()


async def test_queue_full_and_stop_do_not_advance(inbox):
    ingress = Ingress(inbox, "app", capacity=1)
    ingress.worker = asyncio.create_task(asyncio.Event().wait())  # Consumer deliberately stalled.
    raw = RawEnvelope.parse(json.dumps(event()).encode())
    queued = asyncio.create_task(ingress.accept(raw))
    await asyncio.sleep(0)
    with pytest.raises(V2Error) as exc:
        await ingress.accept(raw)
    assert exc.value.code == "queue_full" and ingress.last_sequence is None
    await ingress.close()
    with pytest.raises(V2Error):
        await queued
    assert inbox.queued_bytes == 0 and inbox.count("app") == 0


class Callback:
    def __init__(self, payload, headers=None, *, raw=None):
        self.raw = raw if raw is not None else json.dumps(payload, separators=(",", ":")).encode()
        self.headers = CIMultiDict(headers or {})
        self.method = "POST"

    async def stream(self):
        for n in range(0, len(self.raw), 4096):
            yield self.raw[n:n + 4096]


def signed(payload, *, appid="app", secret="fixture-secret", now=1000):
    req = Callback(payload, {"X-Bot-Appid": appid, "X-Signature-Timestamp": str(now)})
    req.headers["X-Signature-Ed25519"] = signing_key(secret).sign(str(now).encode() + req.raw).hex()
    return req


def test_official_ed25519_fixture():
    key = signing_key("naOC0ocQE3shWLAfffVLB1rhYPG7")
    assert list(key.public_key().public_bytes_raw()) == [215,195,98,254,120,174,248,31,242,50,135,180,147,98,139,93,176,42,60,79,227,11,33,94,77,25,96,155,93,118,103,58]
    challenge_key = signing_key("DG5g3B4j9X2KOErG")
    assert challenge_key.sign(b"1725442341Arq0D5A61EgUu4OxUvOp").hex() == "87befc99c42c651b3aac0278e71ada338433ae26fcb24307bdc5ad38c1adc2d01bcfcadc0842edac85e85205028a1132afe09280305f13aa6909ffc2d652c706"


async def test_webhook_challenge_signature_replay_and_status(inbox):
    now = [1000]
    ingress = Ingress(inbox, "app")
    ingress.start()
    hook = Webhook("app", "fixture-secret", ingress, clock=lambda: now[0])
    try:
        assert not hook.online
        req = Callback({"op": 13, "d": {"plain_token": "challenge", "event_ts": "1000"}}, {"X-Bot-Appid": "app"})
        response, code = await hook.handle(req)
        assert code == 200 and response["plain_token"] == "challenge"
        signing_key("fixture-secret").public_key().verify(bytes.fromhex(response["signature"]), b"1000challenge")
        assert hook.challenge_answered and not hook.online
        req = signed(event())
        assert await hook.handle(req) == ({"op": 12}, 200)
        assert await hook.handle(signed(event())) == ({"op": 12}, 200)
        assert hook.online and inbox.count("app") == 1
        assert inbox.pending("app")[0]["payload"]["id"] == "outer"
        now[0] = 1400
        assert (await hook.handle(req))[1] == 401 and not hook.online
        hook.close()
        assert (await hook.handle(signed(event(), now=1400)))[1] == 503
    finally:
        hook.close()
        await ingress.close()


@pytest.mark.parametrize("bad", ["signature", "body", "appid", "timestamp", "method", "size", "unsigned", "opcode"])
async def test_webhook_rejects_before_intake(inbox, bad):
    ingress = Ingress(inbox, "app")
    ingress.start()
    hook = Webhook("app", "fixture-secret", ingress, clock=lambda: 1000)
    req = signed(event())
    if bad == "signature":
        req.headers["X-Signature-Ed25519"] = "00" * 64
    if bad == "body":
        req.raw += b" "
    if bad == "appid":
        req.headers["X-Bot-Appid"] = "another"
    if bad == "timestamp":
        req = signed(event(), now=2000)
    if bad == "method":
        req.method = "GET"
    if bad == "size":
        req.headers["Content-Length"] = str(1024 * 1024 + 1)
    if bad == "unsigned":
        req.headers.pop("X-Signature-Ed25519")
    if bad == "opcode":
        req = signed({"op": 12})
    try:
        _, status = await hook.handle(req)
        assert status >= 400 and inbox.count("app") == 0 and not hook.online
    finally:
        hook.close()
        await ingress.close()


async def test_webhook_full_queue_never_acknowledges(inbox):
    inbox.max_rows = 0
    ingress = Ingress(inbox, "app")
    ingress.start()
    hook = Webhook("app", "fixture-secret", ingress, clock=lambda: 1000)
    try:
        assert (await hook.handle(signed(event())))[1] == 503
        assert not hook.online and not hook.replays.items
        inbox.max_rows = 10
        assert (await hook.handle(signed(event())))[1] == 200
        hook.active = 16
        assert (await hook.handle(signed(event("another"))))[1] == 503
        hook.active = 0
    finally:
        hook.close()
        await ingress.close()


class FakeWS:
    def __init__(self, frames):
        self.frames = asyncio.Queue()
        for frame in frames:
            self.frames.put_nowait(frame)
        self.sent = []
        self.closed = False
        self.close_code = 1000
        self.idle = asyncio.Event()

    async def receive(self):
        if self.frames.empty():
            self.idle.set()
        frame = await self.frames.get()
        if isinstance(frame, int):
            self.close_code = frame
            return SimpleNamespace(type=aiohttp.WSMsgType.CLOSE, data=frame)
        return SimpleNamespace(type=aiohttp.WSMsgType.TEXT, data=json.dumps(frame))

    async def send_json(self, value):
        self.sent.append(value)

    async def close(self):
        self.closed = True


class FakeGatewayHTTP:
    def __init__(self, identity, sockets):
        self.identity, self.sockets = identity, list(sockets)
        self.session = self
        self.connects = 0

    async def request(self, _):
        return SimpleNamespace(data={"url": "wss://api.bot.qq.com/websocket"})

    async def token(self):
        return "fixture-access-token"

    async def ws_connect(self, *args, **kwargs):
        self.connects += 1
        item = self.sockets.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


HELLO = {"op": 10, "d": {"heartbeat_interval": 45000}}
READY = {"op": 0, "s": 1, "t": "READY", "d": {"session_id": "fixture-session", "user": {"id": "bot"}}}


async def reconnect_sleep(delay):
    if delay >= 1:
        await asyncio.Event().wait()
    await asyncio.sleep(0)


async def test_gateway_first_connect_resume_full_envelope_and_fatal(config, inbox):
    ingress = Ingress(inbox, "app")
    ingress.start()
    first = FakeWS([HELLO, READY, event(), {"op": 7}])
    second = FakeWS([HELLO, {"op": 0, "s": 3, "t": "RESUMED", "d": ""}, event(), 4014])
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [OSError("first connect"), first, second])
    gateway = Gateway(http, ingress, attempts=3, sleep=lambda delay: asyncio.sleep(0) if delay < 2 else asyncio.Event().wait(), jitter=lambda: 0)
    try:
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(gateway.run(), 2)
        assert exc.value.business_code == 4014 and http.connects == 3
        assert first.sent[0]["op"] == 2 and first.sent[0]["d"]["intents"] == config["intents"]
        assert second.sent[0]["op"] == 6 and second.sent[0]["d"]["seq"] == 2
        assert [r["payload"] for r in inbox.pending("app") if r["payload"].get("id") == "outer"] == [event()]
        assert not gateway.online and first.closed and second.closed and not gateway.tasks
    finally:
        await gateway.close()
        await ingress.close()


@pytest.mark.parametrize("code", [4006, 4007, 4900, 4013])
async def test_gateway_close_policy(config, inbox, code):
    ingress = Ingress(inbox, "app")
    ingress.start()
    first, second = FakeWS([HELLO, READY, code]), FakeWS([HELLO, READY, 4014])
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [first, second])
    gateway = Gateway(http, ingress, attempts=2, sleep=reconnect_sleep, jitter=lambda: 0)
    try:
        with pytest.raises(V2Error):
            await asyncio.wait_for(gateway.run(), 2)
        assert http.connects == (1 if code == 4013 else 2)
        if code != 4013:
            assert second.sent[0]["op"] == 2
    finally:
        await gateway.close()
        await ingress.close()


class StepClock:
    def __init__(self):
        self.now = 0
        self.waits = asyncio.Queue()

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waits.put_nowait((delay, future))
        await future

    async def tick(self):
        delay, future = await asyncio.wait_for(self.waits.get(), 1)
        self.now += delay
        future.set_result(None)


async def test_ack_deadline_and_reconnect_budget_with_virtual_clock(config, inbox):
    ingress = Ingress(inbox, "app")
    ingress.start()
    clock = StepClock()
    ws = FakeWS([HELLO, READY])
    gateway = Gateway(FakeGatewayHTTP(InstanceKey.from_config(config), [ws]), ingress,
                      attempts=1, clock=lambda: clock.now, sleep=clock.sleep)
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(ws.idle.wait(), 1)
        assert gateway.online
        await clock.tick()
        await clock.tick()
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(task, 1)
        assert exc.value.code == "reconnect_exhausted" and gateway.last_error == "heartbeat_ack_timeout"
        assert not gateway.online and ws.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
        await ingress.close()


async def test_gateway_cancel_online_cleans_children(config, inbox):
    ingress = Ingress(inbox, "app")
    ingress.start()
    ws = FakeWS([HELLO, READY])
    gateway = Gateway(FakeGatewayHTTP(InstanceKey.from_config(config), [ws]), ingress, sleep=reconnect_sleep)
    task = asyncio.create_task(gateway.run())
    await asyncio.wait_for(ws.idle.wait(), 1)
    assert gateway.online
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await gateway.close()
    await ingress.close()
    assert ws.closed and not gateway.tasks and ingress.worker.done() and not gateway.online


@pytest.mark.parametrize("disconnect", [4009, {"op": 7}], ids=["close-4009", "opcode-7"])
async def test_documented_resume_then_invalid_session_identifies(config, inbox, disconnect):
    ingress = Ingress(inbox, "app")
    ingress.start()
    first = FakeWS([HELLO, READY, event(), disconnect])
    resumed = FakeWS([HELLO, {"op": 0, "s": 3, "t": "RESUMED", "d": ""}, 4006])
    fresh = FakeWS([HELLO, READY, 4014])
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [first, resumed, fresh])
    gateway = Gateway(http, ingress, attempts=3, jitter=lambda: 0,
                      sleep=lambda delay: asyncio.sleep(0) if delay < 2 else asyncio.Event().wait())
    try:
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(gateway.run(), 2)
        assert exc.value.business_code == 4014 and http.connects == 3
        assert [ws.sent[0]["op"] for ws in (first, resumed, fresh)] == [2, 6, 2]
        assert resumed.sent[0]["d"]["session_id"] == READY["d"]["session_id"]
        assert resumed.sent[0]["d"]["seq"] == event()["s"]
        assert "session_id" not in fresh.sent[0]["d"] and "seq" not in fresh.sent[0]["d"]
        assert not gateway.online and not gateway.tasks
        assert all(ws.closed for ws in (first, resumed, fresh))
    finally:
        await gateway.close()
        await ingress.close()
