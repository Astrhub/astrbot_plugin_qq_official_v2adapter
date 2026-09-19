import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web

from v2.errors import V2Error
from v2.models import InstanceKey
from v2.protocol import RequestSpec
from v2.transport.http import HTTPTransport


class MappedSession:
    """Explicit fixture: preserve the driver but route fixed QQ URLs to local mock I/O."""
    def __init__(self, base):
        self.base = base
        self.inner = aiohttp.ClientSession()
        self.calls = []

    @property
    def closed(self):
        return self.inner.closed

    def request(self, method, url, **kwargs):
        parsed = urlsplit(url)
        assert parsed.scheme == "https" and parsed.hostname in {"api.bot.qq.com", "q.qq.com"}
        assert parsed.port in (None, 443) and parsed.username is None and parsed.password is None
        self.calls.append((method, url, kwargs))
        return self.inner.request(method, self.base + parsed.path + ("?" + parsed.query if parsed.query else ""), **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    async def close(self):
        await self.inner.close()


@asynccontextmanager
async def upstream(handler):
    app = web.Application(client_max_size=9 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        yield f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    finally:
        await runner.cleanup()


def spec(config, method="GET", path="/items", **kwargs):
    return RequestSpec(config["environment"], method, path, **kwargs)


async def test_real_http_query_multipart_empty_array_and_errors(config):
    received = []
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            assert (await request.json())["clientSecret"] == config["secret"]
            return web.json_response({"access_token": "fixture-token", "expires_in": "7200"})
        assert request.headers["Authorization"] == "QQBot fixture-token"
        received.append(request.path)
        if request.path == "/items":
            assert request.query.getall("ids") == ["a", "b"] and request.query["cursor"] == "a+b"
            return web.json_response([{"id": "real-fixture-result"}], headers={"X-Tps-Trace-Id": "trace"})
        if request.path == "/upload":
            form = await request.post()
            assert form["file"].file.read() == b"bytes" and form["value"] == "x"
            return web.Response(status=204)
        return web.json_response({"code": 40093001, "message": config["secret"]}, status=400,
                                 headers={"X-Tps-Trace-Id": "trace", "Retry-After": "3"})
    async with upstream(handler) as base:
        session = MappedSession(base)
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: session)
        try:
            result = await http.request(spec(config, params={"ids": ["a", "b"], "cursor": "a+b"}))
            assert result.data == [{"id": "real-fixture-result"}] and result.trace_id == "trace"
            assert (await http.request(spec(config, "POST", "/upload", multipart={"file": b"bytes", "value": "x"}))).data is None
            with pytest.raises(V2Error) as exc:
                await http.request(spec(config, "POST", "/error"))
            assert exc.value.business_code == 40093001 and exc.value.phase == "rejected"
            assert exc.value.retry_after == "3" and config["secret"] not in str(exc.value)
            assert all(not call[2]["allow_redirects"] for call in session.calls)
        finally:
            await http.close()
        assert session.closed and http._token == http._secret == ""


@pytest.mark.parametrize("status,method,expected,phase", [(401, "POST", 2, "rejected"), (429, "GET", 3, "rejected"),
    (503, "GET", 3, "result_unknown"), (503, "POST", 1, "result_unknown"), (429, "POST", 1, "rejected"), (302, "POST", 1, "rejected")])
async def test_retry_limits_and_write_disposition(config, status, method, expected, phase):
    counts = {"token": 0, "api": 0}
    delays = []
    async def sleep(delay):
        delays.append(delay)
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            counts["token"] += 1
            return web.json_response({"access_token": f"token-{counts['token']}", "expires_in": 7200})
        counts["api"] += 1
        return web.Response(status=status, headers={"Retry-After": "1", "Location": "https://evil.invalid/"})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base), sleep=sleep)
        try:
            with pytest.raises(V2Error) as exc:
                await http.request(spec(config, method))
            assert exc.value.phase == phase
            assert counts["api"] == expected and counts["token"] == (2 if status == 401 else 1)
            if status == 429 and method == "GET":
                assert delays == [1, 1]
        finally:
            await http.close()


async def test_single_flight_expiry_cancellation_and_rotation(config):
    now = [0]
    entered, release = asyncio.Event(), asyncio.Event()
    count = 0
    valid = [True]
    def guard():
        if not valid[0]:
            raise V2Error("stale_generation", "fixture revoked")
    async def handler(request):
        nonlocal count
        count += 1
        entered.set()
        await release.wait()
        return web.json_response({"access_token": f"token-{count}", "expires_in": 100})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], guard=guard,
                             session_factory=lambda: MappedSession(base), clock=lambda: now[0])
        try:
            first = asyncio.create_task(http.token())
            await entered.wait()
            second = asyncio.create_task(http.token())
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            release.set()
            assert await second == "token-1" and count == 1
            assert len(set(await asyncio.gather(*(http.token() for _ in range(20))))) == 1
            now[0] = 90
            assert await http.token() == "token-2" and count == 2
            valid[0] = False
            with pytest.raises(V2Error) as exc:
                await http.token()
            assert exc.value.code == "stale_generation"
        finally:
            await http.close()


async def test_unknown_write_timeout_is_not_replayed(config):
    received, finish = asyncio.Event(), asyncio.Event()
    count = 0
    async def handler(request):
        nonlocal count
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "t", "expires_in": 7200})
        count += 1
        received.set()
        await finish.wait()
        return web.json_response({"id": "already-sent"})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base), timeout=0.05)
        try:
            with pytest.raises(V2Error) as exc:
                await http.request(spec(config, "POST"))
            assert received.is_set() and count == 1 and exc.value.phase == "result_unknown"
        finally:
            finish.set()
            await http.close()


async def test_stop_cancels_pending_refresh_and_closes_session(config):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        entered.set()
        await release.wait()
        return web.json_response({"access_token": "must-not-survive", "expires_in": 7200})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base))
        task = asyncio.create_task(http.request(spec(config)))
        await entered.wait()
        await http.close()
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        assert http.session.closed and http._refresh is None and task.done() and not http._active
        assert not http._token
