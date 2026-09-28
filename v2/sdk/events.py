"""Bounded, read-only live observations independent of AstrBot."""

import asyncio
import copy
import json
import math
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from ..errors import V2Error
from .catalog import EVENT_INTENTS, EVENT_NAMES


def validate_event(payload):
    if not isinstance(payload, dict):
        raise V2Error("invalid_sdk_event", "SDK dispatch must be an object.")
    pending, count = [(payload, 0)], 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 32 or count > 4096:
            raise V2Error("invalid_sdk_event", "SDK dispatch structure exceeds its bound.")
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)
        elif type(value) is float and not math.isfinite(value):
            raise V2Error("invalid_sdk_event", "SDK dispatch contains a non-finite number.")
        elif type(value) not in (str, int, float, bool, type(None)):
            raise V2Error("invalid_sdk_event", "SDK dispatch contains an unsupported value.")


def freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class EventContext:
    platform_id: str
    appid: str
    environment: str
    generation: str
    transport: str | None
    shard: tuple[int, int] | None
    session_id: str | None
    received_at: float
    receipt: int
    recovered: bool = False
    core_state: str = "done"
    host_state: str = "pending"


class NativeEvent:
    """A frozen view; raw() returns a detached, writable copy."""

    def __init__(self, payload: dict, context: EventContext, client: Any = None, reply_context: Any = None, diagnostic=None):
        validate_event(payload)
        self._payload = freeze(copy.deepcopy(payload))
        self.context = context
        self.diagnostic = diagnostic
        self.client = client
        self.reply_context = reply_context
        self.id = payload.get("id")
        self.key = ((context.appid, context.environment, self.id) if isinstance(self.id, str) and self.id
                    else (context.appid, context.environment, context.platform_id, context.generation, context.receipt))
        self.op = payload.get("op")
        self.s = payload.get("s")
        self.t = payload.get("t")
        self.d = self._payload.get("d")
        self.known = diagnostic is None and self.op == 0 and self.t in EVENT_NAMES
        self.intent = EVENT_INTENTS.get(self.t) if self.op == 0 else None

    @property
    def payload(self):
        return self._payload

    def raw(self):
        return copy.deepcopy(self._thaw(self._payload))

    @staticmethod
    def _thaw(value):
        if isinstance(value, MappingProxyType):
            return {key: NativeEvent._thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [NativeEvent._thaw(item) for item in value]
        return value


_STOP = object()


class Subscription:
    def __init__(self, bus, names, callback, owner, capacity, timeout, include_recovered):
        self.bus, self.names, self.callback, self.owner = bus, frozenset(names), callback, owner
        self.queue = asyncio.Queue(maxsize=capacity)
        self.timeout, self.include_recovered = timeout, include_recovered
        self.closed = False
        self.gap = None
        self.failures = 0
        self.bytes = 0
        self.task = asyncio.create_task(self._run(), name="qq-v2-sdk-observer") if callback else None

    async def _run(self):
        while True:
            event, size = await self.queue.get()
            self.bytes -= size
            self.bus.queued_bytes -= size
            try:
                async with asyncio.timeout(self.timeout):
                    await self.callback(event)
            except asyncio.CancelledError:
                if not self.closed and not self.bus.closed:
                    self.failures += 1
                    self.bus.failures += 1
                    self._stop("subscription_gap")
                raise
            except Exception:
                self.failures += 1
                self.bus.failures += 1
            finally:
                self.queue.task_done()

    def _stop(self, reason=None):
        if self.closed:
            return
        self.closed = True
        self.gap = reason
        self.bus.subscriptions.discard(self)
        while not self.queue.empty():
            _, size = self.queue.get_nowait()
            self.bytes -= size
            self.bus.queued_bytes -= size
            self.queue.task_done()
        if self.task:
            self.task.cancel()
        else:
            self.queue.put_nowait((_STOP, 0))
        if reason:
            self.bus.gaps += 1

    async def close(self):
        self._stop()
        if self.task and self.task is not asyncio.current_task():
            await asyncio.gather(self.task, return_exceptions=True)

    async def __aiter__(self):
        if self.callback:
            raise V2Error("invalid_subscription", "Callback subscriptions are not event streams.")
        try:
            while not self.closed:
                event, size = await self.queue.get()
                if event is _STOP:
                    self.queue.task_done()
                    break
                self.bytes -= size
                self.bus.queued_bytes -= size
                self.queue.task_done()
                yield event
        finally:
            await self.close()


class EventBus:
    """Instance-scoped, bounded real-time subscriber delivery."""

    def __init__(self, guard=lambda: None, *, capacity=32, max_bytes=16 * 1024 * 1024, progress=None):
        self.guard, self._progress = guard, progress
        self.capacity, self.max_bytes = capacity, max_bytes
        self.subscriptions = set()
        self.queued_bytes = 0
        self.gaps = 0
        self.failures = 0
        self.closed = False

    def progress(self, receipt):
        self.guard()
        if self.closed:
            raise V2Error("service_stopped", "SDK observer is closed.", status=503)
        if self._progress is None:
            raise V2Error("progress_unavailable", "No raw inbox is attached to this event bus.", status=503)
        return self._progress(receipt)


    def subscribe(self, names, *, callback=None, owner, capacity=64, timeout=5, include_recovered=False):
        self.guard()
        if self.closed:
            raise V2Error("service_stopped", "SDK observer is closed.", status=503)
        if (not isinstance(names, (set, frozenset, list, tuple)) or not names
                or any(not isinstance(name, str) or name not in EVENT_NAMES | {"*"} for name in names)
                or owner is None or type(capacity) is not int or not 1 <= capacity <= 64
                or type(timeout) not in (int, float) or not 0 < timeout <= 60
                or type(include_recovered) is not bool
                or callback is not None and not asyncio.iscoroutinefunction(callback)):
            raise V2Error("invalid_subscription", "Use an owner, known event names and an async callback.")
        if len(self.subscriptions) >= self.capacity:
            raise V2Error("subscription_capacity", "SDK subscription limit reached.", status=503)
        sub = Subscription(self, names, callback, owner, capacity, timeout, include_recovered)
        self.subscriptions.add(sub)
        return sub

    def stream(self, names, *, owner, include_recovered=False):
        return self.subscribe(names, owner=owner, include_recovered=include_recovered)

    def publish(self, event, *, raw_only=False):
        self.guard()
        if self.closed:
            return
        if not self.subscriptions:
            return
        size = len(json.dumps(event.raw(), ensure_ascii=False).encode())
        for sub in tuple(self.subscriptions):
            if (raw_only and "*" not in sub.names or
                    event.t not in sub.names and "*" not in sub.names or
                    event.context.recovered and not sub.include_recovered):
                continue
            if sub.queue.full() or self.queued_bytes + size > self.max_bytes:
                sub._stop("subscription_gap")
                continue
            sub.queue.put_nowait((event, size))
            sub.bytes += size
            self.queued_bytes += size

    def close_owner(self, owner):
        for sub in tuple(self.subscriptions):
            if sub.owner is owner:
                sub._stop()

    def invalidate(self):
        self.closed = True
        for sub in tuple(self.subscriptions):
            sub._stop()


    def diagnostics(self):
        return {"subscriptions": len(self.subscriptions), "queued_bytes": self.queued_bytes,
                "gaps": self.gaps, "failures": self.failures}

    async def close(self):
        self.closed = True
        tasks = [sub.task for sub in self.subscriptions if sub.task]
        for sub in tuple(self.subscriptions):
            sub._stop()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
