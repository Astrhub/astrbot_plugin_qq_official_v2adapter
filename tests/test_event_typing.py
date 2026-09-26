import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from astrbot.core.platform import PlatformMetadata
from test_streaming import streaming as streaming

from v2.client import V2Client
from v2.errors import V2Error
from v2.event import V2MessageEvent
from v2.messaging.typing import TypingCore


@pytest.fixture
async def typing_events(streaming):
    settings = {}
    core = TypingCore(streaming.sender, settings=lambda: settings)
    client = V2Client(streaming.sender.identity)
    client._state.sender = streaming.sender
    client._state.typing = core

    def event(name="C2C_MESSAGE_CREATE"):
        chat = streaming.observe(name)
        return V2MessageEvent(chat.message, PlatformMetadata("qq_official_v2", "fixture", "test-v2"), client, chat.route)

    try:
        yield SimpleNamespace(event=event, core=core, client=client, settings=settings, wire=streaming)
    finally:
        await core.close()
        await client.close()


@pytest.mark.parametrize("name,enabled", [
    ("GROUP_AT_MESSAGE_CREATE", True), ("GROUP_MESSAGE_CREATE", True),
    ("AT_MESSAGE_CREATE", True), ("MESSAGE_CREATE", True),
    ("DIRECT_MESSAGE_CREATE", True), ("C2C_MESSAGE_CREATE", False),
    ("C2C_MESSAGE_CREATE", None),
])
async def test_automatic_typing_skips_without_side_effects(typing_events, monkeypatch, name, enabled):
    t = typing_events
    if enabled is not None:
        t.settings["typing_enabled"] = enabled
    event = t.event(name)
    explicit = AsyncMock(wraps=event.bot.qq.typing)
    monkeypatch.setattr(event.bot.qq, "typing", explicit)
    tasks = asyncio.all_tasks()
    assert await event.send_typing() is None
    await event.stop_typing()
    explicit.assert_not_called()
    assert asyncio.all_tasks() == tasks and not t.core.jobs
    assert t.wire.http.session is None and not t.wire.calls
    assert t.wire.store.db.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0
    assert t.wire.store.db.execute("SELECT COUNT(*) FROM charges").fetchone()[0] == 0
    assert t.wire.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    with pytest.raises(V2Error) as error:
        await event.bot.qq.typing(event.route.scene, event.route.target)
    assert error.value.as_dict()["code"] == "unsupported" and error.value.retcode == 1404
    assert not t.core.jobs and t.wire.http.session is None


async def test_automatic_typing_skips_unattached_service(typing_events, monkeypatch):
    t = typing_events
    t.settings["typing_enabled"] = True
    event = t.event()
    t.client._state.typing = None
    explicit = AsyncMock(wraps=event.bot.qq.typing)
    monkeypatch.setattr(event.bot.qq, "typing", explicit)
    assert await event.send_typing() is None
    await event.stop_typing()
    explicit.assert_not_called()
    assert not t.core.jobs and t.wire.http.session is None
    with pytest.raises(V2Error) as error:
        await event.bot.qq.typing(event.route.scene, event.route.target)
    assert error.value.code == "unsupported"


async def test_automatic_typing_reads_current_setting_and_keeps_source_and_lease(typing_events, monkeypatch):
    t = typing_events
    event = t.event()
    explicit = AsyncMock(wraps=event.bot.qq.typing)
    monkeypatch.setattr(event.bot.qq, "typing", explicit)
    assert await event.send_typing() is None
    t.settings["typing_enabled"] = True
    result = await event.send_typing()
    explicit.assert_awaited_once_with("c2c", "user-one")
    assert result["kind"] == "typing" and result["state"] == "notified" and "message_id" not in result
    assert result["expires_at"] == t.wire.clock[0] + 10
    assert t.wire.calls == [("/v2/users/user-one/messages", {
        "msg_type": 6, "input_notify": {"input_type": 1, "input_second": 10},
        "msg_seq": 1, "msg_id": "msg-one",
    })]
    assert await event.send_typing() == result
    t.settings["typing_enabled"] = False
    assert await event.send_typing() is None
    assert explicit.await_count == 2 and len(t.wire.calls) == 1
    await event.stop_typing()
    assert not t.core.jobs
    t.settings["typing_enabled"] = True
    assert await event.send_typing() == result
    assert len(t.wire.calls) == 1
    assert t.wire.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1


@pytest.mark.parametrize("failure,code", [
    ("stopped", "service_stopped"), ("generation", "stale_generation"),
    ("source", "passive_source_required"), ("permission", "permission_denied"),
    ("network", "network_failure"),
])
async def test_enabled_automatic_typing_propagates_errors(typing_events, monkeypatch, failure, code):
    t = typing_events
    t.settings["typing_enabled"] = True
    event = t.event()
    if failure == "stopped":
        await t.core.close()
    elif failure == "generation":
        await t.client.close()
    elif failure == "source":
        event.bot._source = None
    else:
        async def failed_request(*args, **kwargs):
            raise V2Error(code, "fixture", status=403 if failure == "permission" else 503)
        monkeypatch.setattr(t.wire.http, "request", failed_request)
    with pytest.raises(V2Error) as error:
        await event.send_typing()
    assert error.value.code == code
    await event.stop_typing()
    assert not t.core.jobs and not t.wire.calls


@pytest.mark.parametrize("cancel_via", ["caller", "stop_hook"])
async def test_automatic_typing_cancellation_cleans_up_without_replay(typing_events, cancel_via):
    t = typing_events
    t.settings["typing_enabled"] = True
    event = t.event()
    t.wire.modes[:] = ["wait"]
    task = asyncio.create_task(event.send_typing())
    try:
        await asyncio.wait_for(t.wire.entered.wait(), 2)
        if cancel_via == "caller":
            task.cancel()
        else:
            await asyncio.wait_for(event.stop_typing(), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not t.core.jobs and len(t.wire.calls) == 1
        assert t.wire.store.db.execute("SELECT state FROM operations").fetchone()[0] == "unknown"
        await event.stop_typing()
        assert len(t.wire.calls) == 1
    finally:
        t.wire.release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
