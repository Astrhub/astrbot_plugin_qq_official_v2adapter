"""A single-loop supervised gateway with bounded reconnect and ACK deadlines."""

import asyncio
import random
import time
from urllib.parse import urlsplit

import aiohttp

from ..errors import V2Error
from ..protocol import RawEnvelope, RequestSpec, openapi_base

FATAL_CLOSES = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}
FRESH_CLOSES = {4006, 4007, *range(4900, 4914)}
GATEWAY_HOSTS = {"api.bot.qq.com"}


class GatewayClosed(V2Error):
    def __init__(self, close_code):
        super().__init__("gateway_closed", "QQ gateway closed the connection.", status=503, business_code=close_code)


class Gateway:
    def __init__(self, http, ingress, *, guard=lambda: None, clock=time.monotonic,
                 sleep=asyncio.sleep, jitter=random.random, attempts=6, hello_timeout=10):
        self.http, self.ingress, self.guard = http, ingress, guard
        self.clock, self.sleep, self.jitter = clock, sleep, jitter
        self.attempt_limit, self.hello_timeout = attempts, hello_timeout
        self.state = "idle"
        self.connected = False
        self.stopped = False
        self.ws = None
        self.session_id = None
        self.last_error = None
        self.last_failure = None
        self.attempts = 0
        self.interval = 30
        self.ack_pending = False
        self.last_ack = 0
        self.ack_sent_at = 0
        self.tasks = set()

    @property
    def online(self):
        return self.connected and not self.stopped and self.clock() - self.last_ack <= self.interval * 2

    @staticmethod
    def validate_gateway(url):
        try:
            p = urlsplit(url)
            if (p.scheme != "wss" or p.hostname not in GATEWAY_HOSTS or p.port not in (None, 443)
                    or p.username is not None or p.password is not None or p.fragment
                    or "\\" in url or any(ord(c) <= 32 for c in url)):
                raise ValueError
        except (ValueError, TypeError):
            raise V2Error("invalid_gateway", "QQ gateway is not an approved WSS origin.", status=502) from None

    def _fresh(self):
        self.session_id = None
        self.ingress.last_sequence = None

    async def run(self):
        try:
            for attempt in range(self.attempt_limit):
                self.guard()
                if self.stopped:
                    return
                self.attempts = attempt + 1
                self.state = "connecting"
                try:
                    await self._connect()
                except asyncio.CancelledError:
                    raise
                except V2Error as exc:
                    self.last_error = exc.code
                    self.last_failure = exc.as_dict()
                    code = exc.business_code
                    if code in FATAL_CLOSES | {100007, 100016, 10004} or exc.code in {"invalid_gateway", "stale_generation", "unsupported_environment"}:
                        raise
                    if code in FRESH_CLOSES:
                        self._fresh()
                except (aiohttp.ClientError, OSError, TimeoutError):
                    self.last_error = "gateway_io_failure"
                    self.last_failure = {"code": self.last_error}
                finally:
                    self.connected = False
                    await self._disconnect()
                if attempt + 1 < self.attempt_limit:
                    self.state = "backoff"
                    await self.sleep(min(15, 0.5 * 2**attempt) + self.jitter() * 0.25)
            raise V2Error("reconnect_exhausted", "QQ reconnect budget exhausted; inspect configuration before reloading.", status=503)
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception:
            self.state = "failed"
            raise
        finally:
            self.connected = False
            await self._disconnect()

    async def _connect(self):
        openapi_base(self.http.identity.robot.environment)
        response = await self.http.request(RequestSpec(self.http.identity.robot.environment, "GET", "/gateway/bot"))
        if not isinstance(response.data, dict):
            raise V2Error("invalid_gateway", "QQ gateway response is incomplete.", status=502)
        url = response.data.get("url")
        self.validate_gateway(url)
        limits = response.data.get("session_start_limit", {})
        if isinstance(limits, dict) and limits.get("remaining") == 0 and not self.session_id:
            raise V2Error("gateway_start_limit", "QQ session start limit is exhausted.", status=429)
        self.guard()
        async with asyncio.timeout(self.hello_timeout):
            self.ws = await self.http.session.ws_connect(url, autoping=True, heartbeat=None,
                                                        max_msg_size=1024 * 1024, headers={"User-Agent": "AstrBot-QQ-V2"})
            # aiohttp may redirect the unauthenticated handshake; never Identify on another origin.
            self.validate_gateway(str(self.ws._response.url))
            hello = await self._receive()
            if hello.payload.get("op") != 10 or not isinstance(hello.payload.get("d"), dict):
                raise V2Error("invalid_hello", "Expected QQ Hello.", status=502)
            interval = hello.payload["d"].get("heartbeat_interval")
            if type(interval) not in (int, float) or not 10 <= interval <= 300000:
                raise V2Error("invalid_hello", "Invalid QQ heartbeat interval.", status=502)
            self.interval = interval / 1000
            self.ack_pending = False
            token = await self.http.token()
            self.guard()
            if self.session_id is not None and self.ingress.last_sequence is not None:
                await self.ws.send_json({"op": 6, "d": {"token": f"QQBot {token}", "session_id": self.session_id,
                                                       "seq": self.ingress.last_sequence}})
            else:
                await self.ws.send_json({"op": 2, "d": {"token": f"QQBot {token}", "intents": self.http.identity.intents,
                                                       "shard": list(self.http.identity.shard), "properties": {}}})
        self.state = "authenticating"
        self.tasks = {asyncio.create_task(self._read(), name="qq-v2-ws-read"),
                      asyncio.create_task(self._heartbeat(), name="qq-v2-ws-heartbeat")}
        done, _ = await asyncio.wait(self.tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            await task

    async def _receive(self):
        self.guard()
        msg = await self.ws.receive()
        self.guard()
        if msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
            code = msg.data if type(msg.data) is int else self.ws.close_code
            raise GatewayClosed(code)
        if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            raise GatewayClosed(self.ws.close_code)
        return RawEnvelope.parse(msg.data.encode() if isinstance(msg.data, str) else msg.data)

    async def _read(self):
        deadline = self.clock() + self.hello_timeout
        while True:
            if not self.connected:
                async with asyncio.timeout(max(0, deadline - self.clock())):
                    envelope = await self._receive()
            else:
                envelope = await self._receive()
            payload = envelope.payload
            op = payload["op"]
            if op == 11:
                self.ack_pending = False
                self.last_ack = self.clock()
            elif op == 1:
                await self._send_heartbeat()
            elif op == 7:
                raise GatewayClosed(4009)
            elif op == 9:
                if payload.get("d") is not True:
                    self._fresh()
                raise GatewayClosed(4009)
            elif op == 0:
                ready = payload.get("t") == "READY"
                resumed = payload.get("t") == "RESUMED"
                if ready:
                    data = payload.get("d")
                    if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not 1 <= len(data["session_id"]) <= 512:
                        raise V2Error("invalid_ready", "QQ READY lacks a session ID.", status=502)
                    self.ingress.last_sequence = None
                await self.ingress.accept(envelope)
                if ready:
                    self.session_id = data["session_id"]
                if ready or resumed:
                    if resumed and self.session_id is None:
                        raise V2Error("invalid_resume", "QQ resumed without a local session.", status=502)
                    self.connected = True
                    self.state = "online"
                    self.last_ack = self.clock()
            else:
                await self.ingress.accept(envelope)

    async def _send_heartbeat(self):
        self.guard()
        if not self.ack_pending:
            self.ack_pending = True
            self.ack_sent_at = self.clock()
            await self.ws.send_json({"op": 1, "d": self.ingress.last_sequence})

    async def _heartbeat(self):
        next_tick = self.clock() + self.interval
        while True:
            await self.sleep(max(0, next_tick - self.clock()))
            self.guard()
            if self.ack_pending:
                if self.clock() >= self.ack_sent_at + self.interval:
                    raise V2Error("heartbeat_ack_timeout", "QQ heartbeat ACK deadline exceeded.", status=503)
                next_tick = self.ack_sent_at + self.interval
                continue
            await self._send_heartbeat()
            next_tick = self.clock() + self.interval

    async def _disconnect(self):
        tasks, self.tasks = self.tasks, set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.ws is not None:
            ws, self.ws = self.ws, None
            try:
                async with asyncio.timeout(2):
                    await ws.close()
            except TimeoutError:
                raise V2Error("ws_close_timeout", "QQ socket close deadline exceeded.", status=503) from None

    async def close(self):
        self.stopped = True
        self.connected = False
        await self._disconnect()
        self.state = "stopped"
