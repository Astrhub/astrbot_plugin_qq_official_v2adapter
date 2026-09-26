"""Bounded HTTP and lazy single-flight tokens, with no ambiguous write replay."""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import aiohttp
from astrbot.core.utils.http_ssl import build_ssl_context_with_certifi

from ..errors import V2Error
from ..media.types import FilePart
from ..protocol import RequestSpec, decode_response, openapi_base, validate_openapi_url

TOKEN_URL = "https://api.bot.qq.com/app/getAppAccessToken"
READ_METHODS = {"GET", "HEAD"}


@dataclass(frozen=True)
class HTTPResult:
    data: object
    status: int
    trace_id: str | None
    phase: str = "response_received"


def retry_delay(value, attempt, *, wall=time.time):
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - wall()
        except (ValueError, TypeError, AttributeError, OverflowError):
            delay = 0.25 * 2**attempt
    return max(0, delay) if math.isfinite(delay) and delay <= 5 else None


class HTTPTransport:
    def __init__(self, identity, secret, *, guard=lambda: None, session_factory=None,
                 clock=time.monotonic, sleep=asyncio.sleep, timeout=10, max_attempts=3, token_provider=None):
        self.identity, self._secret, self.guard = identity, secret, guard
        self.token_provider = token_provider
        self.clock, self.sleep = clock, sleep
        self.timeout, self.max_attempts = timeout, max_attempts
        self._factory = session_factory or self._make_session
        self.session = None
        self._token = ""
        self._expires = 0
        self._refresh = None
        self._active = set()
        # Leave connector capacity for the owned websocket and single-flight token refresh.
        self._slots = asyncio.Semaphore(6)
        self.closed = False
        self._closing = None
        self.last_outcome = "not_sent"

    def _make_session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout),
                                     connector=aiohttp.TCPConnector(ssl=build_ssl_context_with_certifi(), limit=8), trust_env=False,
                                     cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False)

    def check(self):
        if self.closed:
            raise V2Error("stale_generation", "Transport generation is closed.", status=409)
        self.guard()
        openapi_base(self.identity.robot.environment)

    def start(self):
        self.check()
        if self.session is None:
            self.session = self._factory()

    async def _exchange(self, method, url, *, headers=None, json_body=None, multipart=None):
        validate_openapi_url(url, self.identity.robot.environment)
        self.start()
        # All destinations are constructed internally; redirects never receive credentials.
        kwargs = {"headers": {"Accept-Encoding": "identity", **(headers or {})}, "allow_redirects": False}
        if json_body is not None:
            kwargs["json"] = json_body
        if multipart is not None:
            form = aiohttp.FormData()
            if not isinstance(multipart, dict):
                raise V2Error("invalid_request", "Multipart requires a finite mapping.")
            total = 0
            for key, value in multipart.items():
                if not isinstance(key, str) or not isinstance(value, (str, bytes, FilePart)):
                    raise V2Error("invalid_request", "Multipart fields must be text, bytes or owned media.")
                if isinstance(value, FilePart):
                    if value.blob.closed or value.blob.pool.blobs.get(value.blob.handle) is not value.blob:
                        raise V2Error("invalid_media_handle", "Multipart requires an open owned media resource.")
                    total += value.blob.size
                    form.add_field(key, value.payload(), filename=value.name)
                else:
                    total += len(value.encode() if isinstance(value, str) else value)
                    form.add_field(key, value, **({"filename": key} if isinstance(value, bytes) else {}))
            limit = 20_000_000 if any(isinstance(v, FilePart) for v in multipart.values()) else 8 * 1024 * 1024
            if total > limit:
                raise V2Error("request_too_large", "Multipart exceeds its bounded request size.", status=413)
            kwargs["data"] = form
        self.check()
        self.last_outcome = "result_unknown"
        try:
            async with asyncio.timeout(self.timeout):
                async with self.session.request(method, url, **kwargs) as response:
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(65536):
                        body.extend(chunk)
                        if len(body) > 1024 * 1024:
                            safe = self._safe_headers(dict(response.headers))
                            raise V2Error("response_too_large", "QQ response exceeds 1 MiB.", status=502, phase="result_unknown",
                                          http_status=response.status, trace_id=safe.get("x-tps-trace-id"), retry_after=safe.get("retry-after"))
                    try:
                        self.check()
                    except V2Error as exc:
                        safe = self._safe_headers(dict(response.headers))
                        raise V2Error(exc.code, str(exc), status=exc.status, phase="result_unknown",
                                      http_status=response.status, trace_id=safe.get("x-tps-trace-id")) from None
                    return response.status, bytes(body), dict(response.headers)
        except asyncio.CancelledError:
            raise
        except aiohttp.ClientConnectorError:
            self.last_outcome = "not_sent"
            raise V2Error("connect_failed", "QQ connection could not be established.", status=503, phase="not_sent") from None
        except (TimeoutError, aiohttp.ClientError, OSError):
            raise V2Error("network_failure", "QQ request interrupted; a write may have reached QQ.", status=503, phase="result_unknown") from None

    def _safe_headers(self, headers):
        result = {}
        for name in ("x-tps-trace-id", "retry-after"):
            value = next((v for k, v in headers.items() if k.lower() == name), None)
            if isinstance(value, str) and len(value) <= 128 and all(32 <= ord(c) < 127 for c in value):
                if not any(s and s in value for s in (self._secret, self._token)):
                    result[name] = value
        return result

    async def _fetch_token(self):
        for attempt in range(self.max_attempts):
            self.check()
            try:
                status, body, headers = await self._exchange("POST", TOKEN_URL, json_body={
                    "appId": self.identity.robot.appid, "clientSecret": self._secret})
                data = decode_response(status, body, self._safe_headers(headers), phase="rejected")
                if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not 1 <= len(data["access_token"]) <= 4096:
                    raise V2Error("invalid_token_response", "QQ token response is incomplete.", status=502)
                if any(not 33 <= ord(c) < 127 for c in data["access_token"]) or type(data.get("expires_in")) is bool:
                    raise V2Error("invalid_token_response", "QQ returned an invalid token or expiry.", status=502)
                duration = float(data.get("expires_in", 0))
                if not math.isfinite(duration) or not 1 <= duration <= 7200:
                    raise ValueError
                self.check()
                self._token = data["access_token"]
                self._expires = self.clock() + duration - min(60, duration * 0.1)
                return self._token
            except (ValueError, TypeError):
                raise V2Error("invalid_token_response", "QQ token expiry is invalid.", status=502) from None
            except V2Error as exc:
                retryable = (exc.business_code == 100001 if exc.business_code is not None else
                             exc.code in {"connect_failed", "network_failure"} or exc.status in {429, 500, 502, 503, 504})
                if not retryable or attempt + 1 >= self.max_attempts:
                    raise
                delay = retry_delay(exc.retry_after, attempt)
                if delay is None:
                    raise
                await self.sleep(delay)
        raise AssertionError("unreachable")

    async def token(self, *, rejected=None):
        self.check()
        if self.token_provider is not None:
            token = await self.token_provider(rejected=rejected)
            self.check()
            self._token = token
            return token
        if rejected is not None and self._token == rejected:
            self._expires = 0
        if self._token and self.clock() < self._expires:
            return self._token
        if self._refresh is None or self._refresh.done():
            self._refresh = asyncio.create_task(self._fetch_token(), name="qq-v2-token")
            self._refresh.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        token = await asyncio.shield(self._refresh)
        self.check()
        return token

    async def request(self, spec: RequestSpec, *, before_send=None):
        self.check()
        if spec.environment != self.identity.robot.environment or spec.method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"}:
            raise V2Error("invalid_request", "Request must use this instance's environment and a supported method.")
        url = spec.url
        validate_openapi_url(url, self.identity.robot.environment)
        if spec.json_body is not None:
            try:
                if len(json.dumps(spec.json_body, allow_nan=False).encode()) > 1024 * 1024:
                    raise ValueError
            except (TypeError, ValueError):
                raise V2Error("invalid_request", "JSON must be finite and no larger than 1 MiB.") from None
        if len(self._active) >= 32:
            raise V2Error("request_capacity", "Too many outstanding QQ requests.", status=429)
        task = asyncio.current_task()
        self._active.add(task)
        request_phase = "not_sent"
        try:
            async with asyncio.timeout(self.timeout * 5), self._slots:
                try:
                    token = await self.token()
                except V2Error as exc:
                    raise V2Error(exc.code, str(exc), retcode=exc.retcode, status=exc.status,
                                  business_code=exc.business_code, trace_id=exc.trace_id,
                                  retry_after=exc.retry_after, phase="not_sent", http_status=exc.http_status) from None
                refreshed = False
                attempts = 0
                while True:
                    self.check()
                    try:
                        if before_send is not None:
                            before_send()
                        request_phase = "result_unknown"
                        status, body, headers = await self._exchange(spec.method, url, headers={
                            "Authorization": f"QQBot {token}", "X-Union-Appid": self.identity.robot.appid},
                            json_body=spec.json_body, multipart=spec.multipart)
                        safe = self._safe_headers(headers)
                        # Only explicit HTTP 401 is used for credential retry; never guess from text.
                        if status == 401 and not refreshed:
                            request_phase = "rejected"
                            refreshed = True
                            try:
                                token = await self.token(rejected=token)
                            except V2Error as exc:
                                raise V2Error("token_refresh_failed", "QQ rejected the message authentication and token refresh failed.",
                                    status=exc.status, business_code=exc.business_code, trace_id=exc.trace_id,
                                    retry_after=exc.retry_after, phase="rejected", http_status=exc.http_status) from None
                            continue
                        phase = "result_unknown" if status >= 500 or status == 408 or 200 <= status < 300 else "rejected"
                        data = decode_response(status, body, safe, phase=phase, path=spec.path)
                        self.last_outcome = "response_received"
                        return HTTPResult(data, status, safe.get("x-tps-trace-id"))
                    except V2Error as exc:
                        self.last_outcome = exc.phase
                        request_phase = exc.phase
                        retryable = exc.business_code is None and (exc.code in {"network_failure", "connect_failed"} or exc.status in {429, 500, 502, 503, 504})
                        attempts += 1
                        if spec.method not in READ_METHODS or not retryable or attempts >= self.max_attempts:
                            raise
                        delay = retry_delay(exc.retry_after, attempts - 1)
                        if delay is None:
                            raise
                        await self.sleep(delay)
        except asyncio.CancelledError as exc:
            exc.phase = request_phase
            raise
        except TimeoutError:
            raise V2Error("request_deadline", "QQ request deadline exceeded.", status=504, phase=request_phase) from None
        finally:
            self._active.discard(task)

    async def close(self):
        if self._closing is None or (self._closing.done() and (self._closing.cancelled() or self._closing.exception() is not None)):
            self.closed = True
            self._closing = asyncio.create_task(self._close_owned(asyncio.current_task()), name="qq-v2-http-close")
            self._closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._closing)

    async def _close_owned(self, caller):
        tasks = set(self._active)
        if self._refresh is not None:
            tasks.add(self._refresh)
        tasks.discard(caller)
        pending = set()
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                done, pending = await asyncio.wait(tasks, timeout=1)
                for task in done:
                    if not task.cancelled():
                        task.exception()
        finally:
            self._token = self._secret = ""
            self._expires = 0
            self._refresh = None
            if self.session is not None:
                try:
                    async with asyncio.timeout(1):
                        await self.session.close()
                except TimeoutError:
                    raise V2Error("http_cleanup_timeout", "HTTP session close deadline exceeded.", status=503) from None
        if any(not task.done() for task in pending):
            raise V2Error("http_cleanup_incomplete", "Some HTTP callers did not stop within the deadline.", status=503)
