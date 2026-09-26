import asyncio
import json

import pytest
from aiohttp import web
from test_transport_http import MappedSession, spec, upstream
from test_transport_receive import (
    HELLO,
    READY,
    Callback,
    FakeGatewayHTTP,
    FakeWS,
    StepClock,
    event,
    gateway_document,
)

from v2.errors import V2Error
from v2.models import InstanceKey
from v2.protocol import RawEnvelope
from v2.transport.http import HTTPTransport
from v2.transport.inbox import Ingress, RawInbox
from v2.transport.webhook import Webhook
from v2.transport.websocket import Gateway


@pytest.mark.parametrize("body,phase", [(b"<html>ambiguous</html>", "result_unknown"),
    (b'{"code":40093002}', "rejected"), (b'{"code":"never-echo-this-credential"}', "result_unknown"),
    (b"x" * (1024 * 1024 + 1), "result_unknown")])
async def test_write_200_invalid_body_is_not_success_or_replayed(config, body, phase):
    count = 0
    async def handler(request):
        nonlocal count
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "fixture-token", "expires_in": 7200})
        count += 1
        return web.Response(body=body)
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base))
        try:
            with pytest.raises(V2Error) as exc:
                await http.request(spec(config, "POST"))
            assert exc.value.phase == phase and count == 1
            assert exc.value.http_status == 200
            assert "never-echo" not in json.dumps(exc.value.as_dict())
        finally:
            await http.close()


async def test_long_retry_after_is_not_ignored(config):
    count = 0
    async def handler(request):
        nonlocal count
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "fixture-token", "expires_in": 7200})
        count += 1
        return web.Response(status=429, headers={"Retry-After": "120"})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base))
        try:
            with pytest.raises(V2Error) as exc:
                await http.request(spec(config))
            assert exc.value.retry_after == "120" and count == 1
        finally:
            await http.close()


async def test_token_business_rejection_means_action_not_sent(config):
    paths = []
    async def handler(request):
        paths.append(request.path)
        return web.json_response({"code": 100016})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base))
        try:
            with pytest.raises(V2Error) as exc:
                await http.request(spec(config, "POST"))
            assert exc.value.phase == "not_sent" and exc.value.business_code == 100016
            assert exc.value.http_status == 200
            assert paths == ["/app/getAppAccessToken"]
        finally:
            await http.close()


async def test_rotation_during_refresh_never_publishes_old_token(config):
    entered, release = asyncio.Event(), asyncio.Event()
    valid = [True]
    def guard():
        if not valid[0]:
            raise V2Error("stale_generation", "fixture rotation")
    async def handler(request):
        entered.set()
        await release.wait()
        return web.json_response({"access_token": "old-generation-token", "expires_in": 7200})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], guard=guard, session_factory=lambda: MappedSession(base))
        pending = asyncio.create_task(http.token())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            valid[0] = False
            release.set()
            with pytest.raises(V2Error) as exc:
                await pending
            assert exc.value.code == "stale_generation" and not http._token
        finally:
            release.set()
            await http.close()


@pytest.mark.parametrize("resumable", [True, False])
async def test_op9_resume_or_identify(config, tmp_path, resumable):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    first = FakeWS([HELLO, READY, {"op": 9, "d": resumable}])
    second = FakeWS([HELLO, READY, 4014])
    gateway = Gateway(FakeGatewayHTTP(InstanceKey.from_config(config), [first, second]), ingress,
                      attempts=2, sleep=lambda delay: asyncio.sleep(0) if delay < 1 else asyncio.Event().wait(), jitter=lambda: 0)
    try:
        with pytest.raises(V2Error):
            await asyncio.wait_for(gateway.run(), 2)
        assert second.sent[0]["op"] == (6 if resumable else 2)
    finally:
        await gateway.close()
        await ingress.close()
        inbox.close()


async def test_initial_connect_budget_and_backoff_cancellation(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [OSError("fixture")] * 3)
    gateway = Gateway(http, ingress, attempts=3, sleep=lambda _: asyncio.sleep(0), jitter=lambda: 0)
    try:
        with pytest.raises(V2Error) as exc:
            await gateway.run()
        assert exc.value.code == "reconnect_exhausted" and http.connects == 3
        clock = StepClock()
        gateway = Gateway(FakeGatewayHTTP(http.identity, [OSError("fixture")]), ingress, sleep=clock.sleep)
        task = asyncio.create_task(gateway.run())
        await asyncio.wait_for(clock.waits.get(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert gateway.state == "stopped" and not gateway.tasks
    finally:
        await gateway.close()
        await ingress.close()
        inbox.close()


@pytest.mark.parametrize("redirect", [False, True], ids=["direct", "redirect"])
async def test_actual_aiohttp_websocket_handshake_ingress_and_heartbeat(config, tmp_path, redirect):
    heartbeat_received, release = asyncio.Event(), asyncio.Event()
    server_frames, handshakes = [], []
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "fixture-token", "expires_in": 7200})
        if request.path == "/gateway/bot":
            assert request.headers["Authorization"] == "QQBot fixture-token"
            return web.json_response(gateway_document(gateway_url))
        assert "Authorization" not in request.headers
        handshakes.append(str(request.url))
        if request.path == "/redirect":
            raise web.HTTPFound(target_url)
        assert request.path == "/websocket" and request.query["route"] == "fixture"
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json(HELLO)
        server_frames.append(await ws.receive_json())
        await ws.send_json(READY)
        await ws.send_json(event(seq=8))
        await ws.send_json({"op": 1})
        server_frames.append(await ws.receive_json())
        await ws.send_json({"op": 11})
        heartbeat_received.set()
        await release.wait()
        await ws.close()
        return ws
    class LocalSession(MappedSession):
        async def ws_connect(self, url, **kwargs):
            assert url == gateway_url
            return await self.inner.ws_connect(url, **kwargs)
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    async with upstream(handler) as base, upstream(handler) as ws_base:
        target_url = ws_base + "/websocket?route=fixture"
        gateway_url = (base + "/redirect" if redirect else target_url).replace("http://", "ws://", 1)
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: LocalSession(base))
        gateway = Gateway(http, ingress)
        task = asyncio.create_task(gateway.run())
        try:
            await asyncio.wait_for(heartbeat_received.wait(), 2)
            assert handshakes == ([base + "/redirect", target_url] if redirect else [target_url])
            assert gateway.online and server_frames[0]["op"] == 2
            assert server_frames[0]["d"]["token"] == "QQBot fixture-token"
            assert server_frames[1] == {"op": 1, "d": 8}
            assert ingress.last_sequence == 8 and inbox.pending("app")[-1]["payload"] == event(seq=8)
        finally:
            task.cancel()
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await gateway.close()
            await http.close()
            await ingress.close()
            inbox.close()
        assert http.session.closed and not gateway.tasks


async def test_callback_stream_limit_and_stop_cancel_inflight(tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    hook = Webhook("app", "fixture-secret", ingress, clock=lambda: 1000)
    entered = asyncio.Event()
    class Blocked(Callback):
        async def stream(self):
            entered.set()
            await asyncio.Event().wait()
            yield b""
    try:
        too_large = Callback({}, {"X-Bot-Appid": "app"}, raw=b"x" * (1024 * 1024 + 1))
        assert (await hook.handle(too_large))[1] == 413
        deep = Callback({}, {"X-Bot-Appid": "app"}, raw=b"[" * 2000 + b"]" * 2000)
        assert (await hook.handle(deep))[1] == 400
        pending = asyncio.create_task(hook.handle(Blocked({}, {"X-Bot-Appid": "app"})))
        await entered.wait()
        await hook.aclose()
        assert await pending == ({"code": "service_stopped"}, 503)
        assert not hook.tasks and not hook.active and not inbox.count("app")
    finally:
        await hook.aclose()
        await ingress.close()
        inbox.close()


def test_inbox_migration_and_byte_limit(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE inbox(row_id INTEGER PRIMARY KEY,owner TEXT,event_id TEXT,body TEXT,received REAL,delivered REAL,UNIQUE(owner,event_id)); PRAGMA user_version=1;")
    payload = json.dumps(event())
    db.execute("INSERT INTO inbox VALUES (1,'app','outer',?,100,NULL)", (payload,))
    db.commit()
    db.close()
    inbox = RawInbox(path, max_bytes=len(payload.encode()) + 1)
    try:
        assert inbox.db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert inbox.diagnostics("app") == {"pending": 1}
        assert path.stat().st_mode & 0o777 == 0o600
        assert inbox.pending("app")[0]["payload"] == event()
        with pytest.raises(V2Error) as exc:
            inbox.accept("app", RawEnvelope.parse(json.dumps(event("more")).encode()))
        assert exc.value.code == "inbox_full" and inbox.count("app") == 1
    finally:
        inbox.close()


async def test_unsigned_challenge_is_not_a_dispatch_signing_oracle(tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    hook = Webhook("app", "fixture-secret", ingress, clock=lambda: 1000)
    try:
        request = Callback({"op": 13, "d": {"plain_token": json.dumps(event()), "event_ts": "1000"}}, {"X-Bot-Appid": "app"})
        body, status = await hook.handle(request)
        assert status == 400 and "signature" not in body
        assert not hook.online and not inbox.count("app")
    finally:
        await hook.aclose()
        await ingress.close()
        inbox.close()


async def test_http_close_survives_repeated_caller_cancellation(config):
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    class Session:
        closed = False
        calls = 0
        async def close(self):
            self.calls += 1
            entered.set()
            await release.wait()
            self.closed = True
            finished.set()
    session = Session()
    http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: session)
    http.start()
    first = asyncio.create_task(http.close())
    await entered.wait()
    try:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(http.close())
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await http.close()
        assert session.closed and session.calls == 1 and http._closing.done()
        assert not http._secret and not http._token and http._refresh is None
    finally:
        release.set()
        await http.close()
