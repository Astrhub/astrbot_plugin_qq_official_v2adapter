"""Bounded WS groups with one robot-wide Identify budget and per-socket cursors."""
import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager

import aiohttp
from astrbot.api import logger
from astrbot.core.utils.http_ssl import build_ssl_context_with_certifi

from ..connection_config import MAX_AUTO_SHARDS
from ..errors import V2Error
from ..protocol import RequestSpec, openapi_base
from .http import retry_after_seconds


def gateway_info(data):
    if not isinstance(data, dict) or not isinstance(data.get("url"), str) or not data["url"]:
        raise V2Error("invalid_gateway", "QQ gateway response lacks a URL.", status=502)
    limits = data.get("session_start_limit")
    if (type(data.get("shards")) is not int or not 1 <= data["shards"] <= 1024
            or not isinstance(limits, dict) or any(type(limits.get(k)) is not int for k in
                ("total", "remaining", "reset_after", "max_concurrency"))
            or not 0 <= limits["remaining"] <= limits["total"]
            or not 1 <= limits["reset_after"] <= 86400000 or limits["max_concurrency"] < 1):
        raise V2Error("invalid_gateway", "QQ gateway shard count or session limits are invalid.", status=502)
    return data


class IdentifyBudget:
    def __init__(self, *, clock=time.monotonic, sleep=asyncio.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = asyncio.Lock()
        self.info = None
        self.last_refresh_at = None
        self.refresh_not_before = 0
        self.reset_at = 0
        self.remaining = 0
        self.starts = deque()

    async def _discover(self, http, *, refresh=False):
        if refresh or self.info is None or self.clock() >= self.reset_at:
            response = await http.request(RequestSpec(http.identity.robot.environment, "GET", "/gateway/bot"))
            data = gateway_info(response.data)
            remaining = data["session_start_limit"]["remaining"]
            if refresh and self.info is not None and self.clock() < self.reset_at:
                remaining = min(self.remaining, remaining)
            self.info = data
            self.remaining = remaining
            self.reset_at = self.clock() + data["session_start_limit"]["reset_after"] / 1000
            self.last_refresh_at = self.clock()
            self.refresh_not_before = 0
        return self.info

    async def discover(self, http, *, refresh=False):
        openapi_base(http.identity.robot.environment)
        async with self.lock:
            if refresh and self.info is not None:
                now = self.clock()
                if ((self.last_refresh_at is not None and now - self.last_refresh_at < 60)
                        or now < self.refresh_not_before):
                    return self.info
                self.last_refresh_at = now
                try:
                    return await self._discover(http, refresh=True)
                except V2Error as exc:
                    delay = retry_after_seconds(exc.retry_after)
                    if delay is not None:
                        self.refresh_not_before = max(self.refresh_not_before, self.clock() + delay)
                    raise
            return await self._discover(http, refresh=refresh)

    async def acquire(self, http, guard):
        async with self.session_start(http, guard) as data:
            return data

    @asynccontextmanager
    async def session_start(self, http, guard, *, resume=False):
        openapi_base(http.identity.robot.environment)
        if resume:
            yield self.info if self.info is not None else await self.discover(http)
            return
        while True:
            guard()
            await self.lock.acquire()
            try:
                data = await self._discover(http)
                now = self.clock()
                while self.starts and self.starts[0] <= now - 5:
                    self.starts.popleft()
                if self.remaining == 0:
                    delay = self.reset_at - now
                elif len(self.starts) >= data["session_start_limit"]["max_concurrency"]:
                    delay = self.starts[0] + 5 - now
                else:
                    guard()
                    self.remaining -= 1
                    break
            except BaseException:
                self.lock.release()
                raise
            self.lock.release()
            await self.sleep(max(0.001, delay))
        try:
            # Serialize bounded handshakes so a late Identify cannot overtake its rate window.
            yield data
        finally:
            self.starts.append(self.clock())
            self.lock.release()


class ShardIngress:
    def __init__(self, ingress):
        self.ingress = ingress
        self.last_sequence = None

    async def accept(self, envelope):
        result = await self.ingress.accept(envelope)
        seq = envelope.payload.get("s")
        if type(seq) is int and (self.last_sequence is None or seq > self.last_sequence):
            self.last_sequence = seq
        return result


class GatewayGroup:
    def __init__(self, http, ingress, budget, *, guard=lambda: None, sleep=asyncio.sleep, jitter=random.random, on_fresh=lambda: None):
        self.http, self.ingress, self.budget, self.guard = http, ingress, budget, guard
        self.sleep, self.jitter = sleep, jitter
        self.on_fresh = on_fresh
        self.gateways = []
        self.tasks = set()
        self.terminal_failures = {}
        self.discovery_failure = None
        self.next_retry_at = None
        self.session = None
        self.recommended = None
        self.planned = 0
        self._state = "idle"
        self.stopped = False

    def _make_session(self):
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(limit=MAX_AUTO_SHARDS,
            ssl=build_ssl_context_with_certifi()), cookie_jar=aiohttp.DummyCookieJar(),
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10), trust_env=False)

    @property
    def online(self):
        return not self.stopped and bool(self.gateways) and all(g.online for g in self.gateways)

    @property
    def available(self):
        return not self.stopped and any(g.online for g in self.gateways)

    @property
    def state(self):
        if self._state in {"failed", "stopped"}:
            return self._state
        if self.online:
            return "online"
        if self.terminal_failures or any(g.online or g.state == "backoff" for g in self.gateways):
            return "degraded"
        return self._state

    @property
    def last_failure(self):
        if self.terminal_failures:
            return next(reversed(self.terminal_failures.values()))
        return next((g.last_failure for g in self.gateways if g.last_failure), self.discovery_failure)

    @property
    def last_error(self):
        failure = self.last_failure
        return failure["code"] if failure else None

    def status(self):
        return {"mode": self.http.identity.shard_mode, "recommended": self.recommended,
                "planned": self.planned, "connected": sum(g.online for g in self.gateways), "state": self.state,
                "available": self.available, "discovery_failure": self.discovery_failure,
                "next_retry_at": self.next_retry_at,
                "shards": [{"index": g.shard[0], "count": g.shard[1],
                            "state": "failed" if g.shard[0] in self.terminal_failures else g.state,
                            "online": g.online, "failure": self.terminal_failures.get(g.shard[0], g.last_failure),
                            "cleanup_failure": g.cleanup_failure, "attempts": g.attempts,
                            "consecutive_failures": g.consecutive_failures, "next_retry_at": g.next_retry_at,
                            "recovery": "reload_required" if g.shard[0] in self.terminal_failures else None}
                           for g in self.gateways]}

    async def run(self):
        from .websocket import Gateway, reconnect_delay
        self._state = "connecting"
        try:
            failures = 0
            while True:
                self.guard()
                try:
                    data = await self.budget.discover(self.http)
                    self.next_retry_at = None
                    self._state = "connecting"
                    if failures:
                        logger.info(f"QQ V2 gateway discovery recovered after {failures} failures")
                    break
                except V2Error as exc:
                    if (exc.code not in {"connect_failed", "network_failure", "request_deadline", "qq_rate_limited"}
                            and exc.http_status not in {408, 409, 429, 500, 502, 503, 504}):
                        raise
                    failures += 1
                    self.discovery_failure = exc.as_dict()
                    self._state = "backoff"
                    delay = reconnect_delay(failures, exc.retry_after, jitter=self.jitter)
                    self.next_retry_at = time.time() + delay
                    if failures in {1, 3, 6} or failures % 10 == 0:
                        logger.warning(f"QQ V2 gateway discovery retry: code={exc.code}, "
                                       f"http={exc.http_status}, failures={failures}, delay={delay:.1f}s")
                    await self.sleep(delay)
            self.guard()
            self.recommended = data["shards"]
            if self.http.identity.shard_mode == "auto":
                if self.recommended > MAX_AUTO_SHARDS:
                    raise V2Error("shard_capacity", f"Automatic groups support at most {MAX_AUTO_SHARDS} shards; none started.", status=409)
                shards = [(i, self.recommended) for i in range(self.recommended)]
            else:
                shards = [self.http.identity.shard]
            self.planned = len(shards)
            self.session = self._make_session()
            self.gateways = [Gateway(self.http, ShardIngress(self.ingress), guard=self.guard,
                budget=self.budget, shard=shard, ws_session=self.session, on_fresh=self.on_fresh) for shard in shards]
            by_task = {asyncio.create_task(g.run(), name=f"qq-v2-shard-{g.shard[0]}"): g for g in self.gateways}
            self.tasks = set(by_task)
            pending = set(self.tasks)
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        await task
                    except V2Error as exc:
                        if exc.code not in {"reconnect_exhausted", "ws_close_timeout"}:
                            raise
                        gateway = by_task[task]
                        # Exhaustion is terminal for this shard; only an explicit reload retries it.
                        self.terminal_failures[gateway.shard[0]] = {**exc.as_dict(), "details": {
                            "attempts": gateway.attempts, "last_transport_failure": gateway.last_failure}}
                    else:
                        return
            code = "reconnect_exhausted" if all(f["code"] == "reconnect_exhausted" for f in self.terminal_failures.values()) else "gateway_group_failed"
            raise V2Error(code, "No QQ shards remain runnable; reload to retry.", status=503)
        except asyncio.CancelledError:
            self._state = "stopped"
            raise
        except Exception:
            self._state = "failed"
            raise
        finally:
            await self.close()

    async def close(self):
        self.stopped = True
        tasks, self.tasks = self.tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.gather(*(g.close() for g in self.gateways))
        finally:
            if self.session is not None:
                await self.session.close()
            if self._state != "failed":
                self._state = "stopped"
