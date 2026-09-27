"""Gateway outages wait for recovery without replaying outbound messages."""
import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from test_gateway_shards import group_env as group_env
from test_transport_http import upstream
from test_transport_receive import (
    HELLO,
    READY,
    FakeGatewayHTTP,
    FakeWS,
    gateway_document,
)

from v2.errors import V2Error
from v2.models import InstanceKey
from v2.transport.http import retry_after_seconds
from v2.transport.inbox import Ingress, RawInbox
from v2.transport.websocket import Gateway, reconnect_delay


async def test_six_hour_expiry_can_resume_more_than_six_times(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    sockets = [FakeWS([HELLO, READY if i == 0 else {"op": 0, "s": i + 1, "t": "RESUMED", "d": ""},
                       {"op": 1}, {"op": 11}]) for i in range(9)]
    http = FakeGatewayHTTP(InstanceKey.from_config(config), sockets)
    now, delays = [0], []
    async def sleep(delay):
        if delay > 15:
            await asyncio.Event().wait()  # Only the heartbeat timer waits in this fixture.
        delays.append(delay)
        await asyncio.sleep(0)
    gateway = Gateway(http, ingress, clock=lambda: now[0], sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(sockets[0].idle.wait(), 2)
        for previous, following in zip(sockets, sockets[1:]):
            now[0] += 21600
            previous.frames.put_nowait(4009)
            await asyncio.wait_for(following.idle.wait(), 2)
            assert gateway.online and gateway.consecutive_failures == 0 and not task.done()
            assert following.sent[0]["op"] == 6 and following.sent[0]["d"]["seq"] >= 1
        assert http.connects == 9 and delays == [1] * 8
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
        await ingress.close()
        inbox.close()
    assert all(socket.closed for socket in sockets)


async def test_stale_gateway_address_is_refreshed_after_repeated_failures(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    now = [0]
    old_url, new_url = "wss://retired.example/websocket", "wss://new.example/websocket"
    initial = FakeWS([HELLO, READY, {"op": 1}, {"op": 11}])
    restored = FakeWS([HELLO, {"op": 0, "s": 2, "t": "RESUMED", "d": ""}, {"op": 1}, {"op": 11}])
    class HTTP:
        identity = InstanceKey.from_config(config)
        session = None
        discovered = 0
        destinations = []
        async def request(self, _):
            self.discovered += 1
            url = old_url if self.discovered == 1 else new_url
            return SimpleNamespace(data=gateway_document(url, remaining=99))
        async def token(self):
            return "fixture-access-token"
        async def ws_connect(self, url, **kwargs):
            self.destinations.append(url)
            if len(self.destinations) == 1:
                return initial
            if url == old_url:
                raise OSError("retired gateway")
            return restored
    http = HTTP()
    http.session = http
    delays = []
    async def sleep(delay):
        if delay > 15:
            await asyncio.Event().wait()
        delays.append(delay)
        await asyncio.sleep(0)
    gateway = Gateway(http, ingress, clock=lambda: now[0], sleep=sleep, jitter=lambda: 0)
    gateway.budget.clock = lambda: now[0]
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(initial.idle.wait(), 2)
        now[0] = 21600
        initial.frames.put_nowait(4009)
        await asyncio.wait_for(restored.idle.wait(), 2)
        assert delays == [1, 2, 4] and http.discovered == 2
        assert http.destinations == [old_url] * 3 + [new_url]
        assert restored.sent[0]["op"] == 6 and restored.sent[0]["d"]["seq"] == 1
        assert gateway.last_failure["login"] == "resume" and gateway.last_failure["stage"] == "connecting"
        assert gateway.online and gateway.budget.remaining == 98
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
        await ingress.close()
        inbox.close()


async def test_repeated_io_failure_stays_in_backoff_at_cap(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [OSError("fixture") for _ in range(12)])
    parked, delays = asyncio.Event(), []
    async def sleep(delay):
        delays.append(delay)
        if delay >= 720:
            parked.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)
    gateway = Gateway(http, ingress, sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        assert delays == [1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 720]
        assert http.connects == 11 and not task.done() and gateway.state == "backoff"
        assert gateway.next_retry_at is not None and gateway.last_failure["code"] == "gateway_io_failure"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
        await ingress.close()
        inbox.close()


async def test_token_rate_limit_preserves_retry_after_across_gateway_retries(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    socket = FakeWS([HELLO])
    class LimitedTokenHTTP(FakeGatewayHTTP):
        async def token(self):
            raise V2Error("qq_rate_limited", "fixture", status=429, http_status=429, retry_after="60")
    http = LimitedTokenHTTP(InstanceKey.from_config(config), [socket])
    parked = asyncio.Event()
    delays = []
    async def sleep(delay):
        delays.append(delay)
        parked.set()
        await asyncio.Event().wait()
    gateway = Gateway(http, ingress, sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        assert delays == [60] and gateway.state == "backoff" and gateway.attempts == 1
        assert gateway.last_failure["login"] == "identify" and gateway.last_failure["http_status"] == 429
        assert socket.closed and gateway.budget.remaining == 99 and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
        await ingress.close()
        inbox.close()


def test_retry_after_date_and_missing_header():
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    assert 118 <= retry_after_seconds(future) <= 121
    assert reconnect_delay(1, "120", jitter=lambda: 0) == 120
    assert reconnect_delay(1, "120", jitter=lambda: 1) == 125
    assert reconnect_delay(12, None, jitter=lambda: 0) == 720
    assert reconnect_delay(12, None, jitter=lambda: 1) == 900
    assert reconnect_delay(12, "invalid", jitter=lambda: 0) == 720


async def test_websocket_close_4008_without_header_waits_at_least_five_seconds(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    first = FakeWS([HELLO, READY, 4008])
    restored = FakeWS([HELLO, {"op": 0, "s": 2, "t": "RESUMED", "d": ""}, {"op": 1}, {"op": 11}])
    http = FakeGatewayHTTP(InstanceKey.from_config(config), [first, restored])
    parked, resume = asyncio.Event(), asyncio.Event()
    delays = []
    async def sleep(delay):
        if delay > 15:
            await asyncio.Event().wait()
        if not parked.is_set():
            delays.append(delay)
            parked.set()
            await resume.wait()
        else:
            await asyncio.Event().wait()
    gateway = Gateway(http, ingress, sleep=sleep, jitter=lambda: 0)
    task = asyncio.create_task(gateway.run())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        assert delays == [5] and gateway.state == "backoff" and not task.done()
        resume.set()
        await asyncio.wait_for(restored.idle.wait(), 2)
        assert restored.sent[0]["op"] == 6 and gateway.online
    finally:
        resume.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await gateway.close()
        await ingress.close()
        inbox.close()


async def test_http_gateway_discovery_waits_for_retry_after_without_terminating(group_env, monkeypatch):
    e = group_env
    e.suggestion[0] = 1
    original = e.group.budget.discover
    calls, delays = [], []
    parked, resume = asyncio.Event(), asyncio.Event()
    async def discover(http, *, refresh=False):
        calls.append(refresh)
        if len(calls) == 1:
            raise V2Error("qq_rate_limited", "fixture", status=429, http_status=429, retry_after="120")
        return await original(http, refresh=refresh)
    async def sleep(delay):
        delays.append(delay)
        parked.set()
        await resume.wait()
    monkeypatch.setattr(e.group.budget, "discover", discover)
    e.group.sleep, e.group.jitter = sleep, lambda: 0
    task = asyncio.create_task(e.group.run())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        status = e.group.status()
        assert delays == [120] and status["state"] == "backoff" and status["connected"] == 0
        assert status["discovery_failure"]["retry_after"] == "120" and status["next_retry_at"] is not None
        assert not task.done() and e.group.session is None
        resume.set()
        assert (await asyncio.wait_for(e.handshakes.get(), 2))["op"] == 2
        await asyncio.wait_for(e.beats.get(), 2)
        assert e.group.online and e.group.next_retry_at is None and calls == [False, False]
    finally:
        resume.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await e.group.close()


async def test_websocket_handshake_429_uses_header_and_recovers(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    responses, logins = [], []
    release_server, parked, retry, ready = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def handle(request):
        assert request.path == "/ws" and "Authorization" not in request.headers
        responses.append(request.path)
        if len(responses) == 1:
            return web.Response(status=429, headers={"Retry-After": "120"})
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json(HELLO)
        logins.append(await socket.receive_json())
        await socket.send_json(READY)
        await socket.send_json({"op": 1})
        await socket.receive_json()
        await socket.send_json({"op": 11})
        ready.set()
        await release_server.wait()
        return socket
    async with upstream(handle) as base:
        url = base.replace("http:", "ws:") + "/ws"
        class HTTP:
            identity = InstanceKey.from_config(config)
            session = aiohttp.ClientSession()
            async def request(self, _):
                return SimpleNamespace(data=gateway_document(url))
            async def token(self):
                return "fixture-access-token"
        http = HTTP()
        delays = []
        async def sleep(delay):
            if not parked.is_set():
                delays.append(delay)
                parked.set()
                await retry.wait()
            else:
                await asyncio.Event().wait()
        gateway = Gateway(http, ingress, sleep=sleep, jitter=lambda: 0)
        task = asyncio.create_task(gateway.run())
        try:
            await asyncio.wait_for(parked.wait(), 2)
            assert delays == [120] and gateway.last_failure["http_status"] == 429
            assert gateway.last_failure["retry_after"] == 120 and not task.done()
            assert gateway.last_failure["stage"] == "waiting_identify" and gateway.last_failure["login"] == "identify"
            retry.set()
            await asyncio.wait_for(ready.wait(), 2)
            assert gateway.online
            assert logins[0]["op"] == 2 and len(responses) == 2
        finally:
            retry.set()
            release_server.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await gateway.close()
            await http.session.close()
            await ingress.close()
            inbox.close()


async def test_websocket_handshake_401_is_terminal(config, tmp_path):
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "app")
    ingress.start()
    requests = []
    async def handle(request):
        requests.append(request.path)
        return web.Response(status=401)
    async with upstream(handle) as base:
        url = base.replace("http:", "ws:") + "/ws"
        class HTTP:
            identity = InstanceKey.from_config(config)
            session = aiohttp.ClientSession()
            async def request(self, _):
                return SimpleNamespace(data=gateway_document(url))
            async def token(self):
                return "fixture-access-token"
        http = HTTP()
        gateway = Gateway(http, ingress)
        try:
            with pytest.raises(V2Error) as error:
                await asyncio.wait_for(gateway.run(), 2)
            assert error.value.code == "gateway_handshake_rejected" and error.value.http_status == 401
            assert gateway.last_failure["http_status"] == 401 and requests == ["/ws"]
        finally:
            await gateway.close()
            await http.session.close()
            await ingress.close()
            inbox.close()
