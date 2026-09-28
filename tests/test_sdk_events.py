"""Public event views and bounded real-time observation contracts."""

import asyncio
import os
import subprocess
import sys

import pytest
from v2.sdk.http_catalog import HTTP_TARGETS

from v2.errors import V2Error
from v2.sdk.catalog import CONNECTION_EVENTS, EVENT_INTENTS
from v2.sdk.events import EventBus, EventContext, NativeEvent


def context(*, recovered=False):
    return EventContext("p", "app", "production", "g", "websocket", (0, 1), "session", 100.0, 1,
                        recovered=recovered)


def event(name="GROUP_MEMBER_ADD", *, recovered=False):
    return NativeEvent({"id": "event", "op": 0, "d": {"nested": ["original"]}, "s": 7, "t": name},
                       context(recovered=recovered))


def test_sdk_core_imports_without_host_or_botpy():
    code = """import builtins
original = builtins.__import__
def deny(name, *args, **kwargs):
    if name == 'astrbot' or name.startswith('astrbot.') or name == 'botpy' or name.startswith('botpy.'):
        raise AssertionError('host dependency: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = deny
import v2.sdk, v2.sdk.api.groups, v2.profiles
"""
    proc = subprocess.run([sys.executable, "-c", code], env=os.environ.copy(), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_catalog_excludes_only_documented_deprecated_announces():
    assert len(HTTP_TARGETS) == 98
    assert sum(entry.support != "excluded" for entry in HTTP_TARGETS.values()) == 96
    assert HTTP_TARGETS["A026"].path == "/v2/groups/{group_openid}/members/{member_openid}"
    assert HTTP_TARGETS["A027"].pagination == "cursor"
    assert HTTP_TARGETS["A058"].effect == "write"
    assert HTTP_TARGETS["A041"].support == "native"
    assert HTTP_TARGETS["A047"].support == HTTP_TARGETS["A048"].support == "native"
    assert {item.identifier for item in HTTP_TARGETS.values() if item.support == "excluded"} == {"A079", "A080"}
    assert all(item.support == "native" for item in HTTP_TARGETS.values() if item.identifier not in {"A079", "A080"})


def test_event_names_and_payload_are_defensive():
    assert len(EVENT_INTENTS) == 56 and CONNECTION_EVENTS == {"READY", "RESUMED"}
    notice = event()
    with pytest.raises(TypeError):
        notice.payload["d"]["nested"] = []
    with pytest.raises(TypeError):
        notice.d["nested"][0] = "poison"
    raw = notice.raw()
    raw["d"]["nested"][0] = "changed"
    assert notice.d["nested"] == ("original",)
    assert NativeEvent({"op": 0, "t": "RESUMED", "d": ""}, context()).d == ""
    assert NativeEvent({"op": 0, "t": "FUTURE", "d": []}, context()).known is False
    assert notice.key == ("app", "production", "event")
    assert NativeEvent({"op": 0, "t": "RESUMED", "d": ""}, context()).key == ("app", "production", "p", "g", 1)


async def test_full_queue_gap_recovered_filter_and_owner_close():
    bus = EventBus(max_bytes=256)
    owner = object()
    sub = bus.subscribe({"GROUP_MEMBER_ADD"}, owner=owner, capacity=1)
    bus.publish(event(recovered=True))
    assert sub.queue.empty()
    bus.publish(event())
    assert sub.queue.qsize() == 1
    bus.publish(event())
    assert sub.closed and sub.gap == "subscription_gap" and bus.diagnostics()["gaps"] == 1
    recovered = bus.stream({"GROUP_MEMBER_ADD"}, owner=owner, include_recovered=True)
    bus.publish(event(recovered=True))
    assert recovered.queue.qsize() == 1
    bus.close_owner(owner)
    assert recovered.closed and bus.diagnostics()["subscriptions"] == 0
    await bus.close()


async def test_callback_timeout_isolated_and_close_is_idempotent():
    bus = EventBus()
    received = []
    async def blocked(_event):
        await asyncio.Event().wait()
    async def ok(notice):
        received.append(notice.t)
    slow = bus.subscribe({"GROUP_MEMBER_ADD"}, owner=object(), callback=blocked, timeout=0.01)
    fast = bus.subscribe({"GROUP_MEMBER_ADD"}, owner=object(), callback=ok)
    bus.publish(event())
    async with asyncio.timeout(1):
        while not received or not slow.failures:
            await asyncio.sleep(0)
    assert received == ["GROUP_MEMBER_ADD"] and slow.failures == 1
    await slow.close()
    await slow.close()
    await bus.close()
    assert not bus.subscriptions
    assert bus.diagnostics()["failures"] == 1
    with pytest.raises(V2Error) as exc:
        bus.subscribe({"GROUP_MEMBER_ADD"}, owner=object())
    assert exc.value.code == "service_stopped"
