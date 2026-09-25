import asyncio
import copy
import functools
import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_messaging_state import NOW, chat_payload
from test_streaming import chains
from test_streaming import streaming as streaming

from v2.commands import binding_fingerprint
from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.convert import convert_chat
from v2.messaging.store import MessageStore, robot_key
from v2.messaging.typing import TypingCore
from v2.models import InstanceKey
from v2.protocol import RawEnvelope
from v2.settings import DEFAULTS, SettingsStore


def test_real_host_partial_binding_and_unknown_callables_do_not_execute():
    class Owner:
        def __repr__(self):
            raise AssertionError("private state must not be represented")
    owner = Owner()
    plugin = SimpleNamespace(name="fixture", star_cls=owner)
    def function(self, event):
        raise AssertionError("not executable during collection")
    handler = SimpleNamespace(handler=function, handler_module_path="fixture", handler_full_name="fixture.command")
    original = binding_fingerprint(plugin, handler, [], [])
    handler.handler = functools.partial(function, owner)
    assert binding_fingerprint(plugin, handler, [], []) == original
    for unsafe in [functools.partial(function, object()), functools.partial(function, owner, token="private"), lambda: None]:
        handler.handler = unsafe
        if isinstance(unsafe, functools.partial):
            assert binding_fingerprint(plugin, handler, [], []) is None
        else:
            assert binding_fingerprint(plugin, handler, [], []) != original


def test_p3_state_upgrade_keeps_unknown_charges_and_rejects_forward_schema(tmp_path, config):
    path = tmp_path / "messages"
    identity = InstanceKey.from_config(config)
    store = MessageStore(path, clock=lambda: NOW)
    chat = convert_chat(identity, RawEnvelope(chat_payload("C2C_MESSAGE_CREATE"), NOW))
    store.observe(chat)
    store.reserve(chat.route, chat.source, "binding", "legacy-operation")
    store.prepare_attempt(chat.route, chat.source, "legacy-operation")
    store.db.execute("INSERT INTO charges VALUES(?,?,?,?,?)", (robot_key(identity.robot), "legacy-operation", "message_qps", "bot", NOW + 86400))
    store.db.execute("PRAGMA user_version=1")
    store.db.commit()
    store.close()
    restored = MessageStore(path, clock=lambda: NOW)
    assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert restored.operation(identity.robot, "legacy-operation")["state"] == "unknown"
    assert restored.db.execute("SELECT used FROM sources").fetchone()[0] == 1
    assert restored.db.execute("SELECT count(*) FROM charges").fetchone()[0] > 0
    with pytest.raises(V2Error):
        restored.reserve(chat.route, chat.source, "binding", "legacy-operation")
    restored.db.execute("PRAGMA user_version=3")
    restored.db.commit()
    restored.close()
    with pytest.raises(V2Error) as error:
        MessageStore(path)
    assert error.value.code == "message_state_corrupt"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM operations").fetchone()[0] == "unknown"
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3


def test_legacy_settings_normalize_without_losing_selections_or_history(tmp_path):
    store = SettingsStore(tmp_path / "settings")
    old = copy.deepcopy(DEFAULTS)
    old.pop("extensions")
    old["schema_version"] = 1
    old["title"] = "retained choice"
    old["panels"]["group"] = {"mode": "custom", "selected": ["real-handler"]}
    body = json.dumps(old)
    with store.db:
        store.db.execute("INSERT INTO settings VALUES(?,?,?,?,?,?)", ("key", 3, body, 3, body, "legacy"))
        store.db.execute("INSERT INTO versions VALUES(?,?,?)", ("key", 3, body))
    current = store.get("key")
    assert current["revision"] == 3 and current["draft"]["schema_version"] == 2
    assert current["draft"]["panels"]["group"]["selected"] == ["real-handler"]
    assert not current["applied"]["extensions"]["management_writes"]
    assert store.versions("key") == [3]
    store.close()


async def test_typing_is_one_passive_notification_with_no_fabricated_id(streaming):
    s = streaming
    typing = TypingCore(s.sender, settings=lambda: {"typing_enabled": True})
    chat = s.observe()
    try:
        result = await typing.start(chat.route, chat.source, seconds=10)
        assert result["kind"] == "typing" and "message_id" not in result
        assert s.calls[0][1]["input_notify"] == {"input_type": 1, "input_second": 10}
        assert s.calls[0][1]["msg_type"] == 6 and s.calls[0][1]["msg_id"] == "msg-one"
        await typing.stop(chat.route, source=chat.source)
        await typing.start(chat.route, chat.source, seconds=10)
        assert len(s.calls) == 1 and s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
        with pytest.raises(V2Error):
            await typing.start(chat.route, None)
    finally:
        await typing.close()
    assert not typing.jobs


async def test_typing_checks_fixed_deadline_after_token_wait(streaming, monkeypatch):
    s = streaming
    typing = TypingCore(s.sender, settings=lambda: {"typing_enabled": True})
    chat = s.observe()
    original = s.http.token
    async def slow_token(**kwargs):
        token = await original(**kwargs)
        s.clock[0] += 11
        return token
    monkeypatch.setattr(s.http, "token", slow_token)
    try:
        with pytest.raises(V2Error) as error:
            await typing.start(chat.route, chat.source, seconds=10)
        assert error.value.code == "typing_expired" and error.value.phase == "not_sent" and not s.calls
        assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    finally:
        await typing.close()


async def test_typing_stop_cancels_owned_wire_and_preserves_unknown(streaming):
    s = streaming
    typing = TypingCore(s.sender, settings=lambda: {"typing_enabled": True})
    chat = s.observe()
    s.modes[:] = ["wait"]
    task = asyncio.create_task(typing.start(chat.route, chat.source))
    await asyncio.wait_for(s.entered.wait(), 2)
    await typing.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not typing.jobs and len(s.calls) == 1
    assert s.store.db.execute("SELECT state FROM operations").fetchone()[0] == "unknown"


@pytest.mark.parametrize("slot", [0, 1, 2])
async def test_stream_timeouts_at_every_fragment_remain_unknown(streaming, slot):
    s = streaming
    chat = s.observe()
    deadline = asyncio.timeout(None)
    s.core.timeout_factory = lambda seconds: deadline
    s.modes[:] = ["ok"] * slot + ["wait"]
    task = asyncio.create_task(s.core.send(chat.route, chains("first", "second"), source=chat.source, operation_id="timeout-stream"))
    await asyncio.wait_for(s.entered.wait(), 2)
    deadline.reschedule(asyncio.get_running_loop().time() - 1)
    with pytest.raises(V2Error) as error:
        await task
    assert error.value.phase == "result_unknown"
    assert s.store.operation(chat.route.robot, "timeout-stream")["state"] == "unknown"
    assert len(s.calls) == slot + 1 and not s.core.tasks


async def test_stream_generation_rotation_stops_continuations(streaming):
    s = streaming
    chat = s.observe()
    def rotated():
        raise V2Error("stale_generation", "Fixture rotated.")
    async def changed():
        yield from_here("first")
        s.sender.guard = rotated
        yield from_here("second")
    def from_here(text):
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain
        return MessageChain([Plain(text)])
    with pytest.raises(V2Error) as error:
        await s.core.send(chat.route, changed(), source=chat.source, operation_id="rotated")
    assert error.value.code == "stale_generation" and error.value.phase == "result_unknown"
    assert len(s.calls) == 1


def test_extension_lanes_recovery_and_secret_free_fences(tmp_path, config):
    identity = InstanceKey.from_config(config)
    store = MessageStore(tmp_path / "messages", clock=lambda: NOW)
    state = ExtensionStore(store, capacity=1)
    state.begin(identity.robot, "ordinary", "management", "digest")
    state.attempt(identity.robot, "ordinary")
    state.begin(identity.robot, "priority", "interaction_ack", "digest")
    state.attempt(identity.robot, "priority")
    state2 = ExtensionStore(store, capacity=1)
    assert state2.operation(identity.robot, "ordinary")["state"] == "unknown"
    assert state2.operation(identity.robot, "priority")["state"] == "unknown"
    with pytest.raises(V2Error):
        state2.begin(identity.robot, "ordinary", "management", "digest")
    store.close()


async def test_extension_disk_error_after_wire_stops_new_mutations(streaming, monkeypatch):
    from v2.protocol import RequestSpec
    s = streaming
    def unavailable(*args, **kwargs):
        raise sqlite3.OperationalError("fixture disk failure")
    monkeypatch.setattr(s.state, "finish", unavailable)
    spec = RequestSpec("production", "POST", "/channels/c/messages", json_body={"content": "fixture"})
    with pytest.raises(V2Error) as error:
        await s.state.execute(s.http, spec, op_id="disk-failure", kind="fixture")
    assert error.value.code == "extension_storage_unavailable" and error.value.phase == "result_unknown"
    assert s.state.operation(s.http.identity.robot, "disk-failure")["state"] == "in_flight"
    with pytest.raises(V2Error):
        await s.state.execute(s.http, spec, op_id="another", kind="fixture")
    assert len(s.calls) == 1


async def test_typing_ambiguous_business_failure_does_not_refund(streaming):
    s = streaming
    chat = s.observe()
    s.modes[:] = ["ambiguous"]
    typing = TypingCore(s.sender, settings=lambda: {"typing_enabled": True})
    try:
        with pytest.raises(V2Error) as error:
            await typing.start(chat.route, chat.source)
        assert error.value.business_code == 50055001 and error.value.phase == "result_unknown"
        assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
        assert s.store.db.execute("SELECT state FROM operations").fetchone()[0] == "unknown"
    finally:
        await typing.close()
