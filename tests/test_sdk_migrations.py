"""Schema upgrades, operation alias fences and explicit sequence reservations."""

import asyncio
import sqlite3

import pytest

from test_management import management
from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.store import MessageStore, robot_key
from v2.models import RobotKey, SessionRoute
from v2.profiles.store import ProfileStore


def test_message_schema_upgrade_keeps_unknown_and_legacy_profile(tmp_path):
    path = tmp_path / "messages.sqlite3"
    robot = RobotKey("app")
    first = MessageStore(path)
    try:
        first.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                         (robot_key(robot), "unknown", "group", "g", None, "digest", None,
                          "unknown", 1, 1, None, None))
        first.db.execute("INSERT INTO identities VALUES(?,?,?,?,?,?,?,?)",
                         (robot_key(robot), "member_openid", "group:g", "001",
                          '{"user_id":"001","nickname":"historical"}', 1, 100, "msg"))
        first.db.commit()
    finally:
        first.close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE sdk_bindings")
    db.execute("DROP TABLE sdk_sequences")
    db.execute("PRAGMA user_version=2")
    db.commit()
    db.close()
    upgraded = MessageStore(path)
    profiles = ProfileStore(tmp_path / "profiles.sqlite3")
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert upgraded.operation(robot, "unknown")["state"] == "unknown"
        assert profiles.migrate_identities(upgraded) == 1
        assert profiles.get_member(robot, "group", "g", "001")["fields"]["nickname"]["value"] == "historical"
        assert profiles.get_member(robot, "group", "g", "001")["membership"] == "unknown"
        assert upgraded.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 1
    finally:
        profiles.close()
        upgraded.close()
    db = sqlite3.connect(path)
    db.execute("PRAGMA user_version=5")
    db.commit()
    db.close()
    with pytest.raises(V2Error, match="schema"):
        MessageStore(path)

def test_v3_source_schema_adds_event_id_without_erasing_pending_operations(tmp_path):
    path = tmp_path / "v3.sqlite3"
    robot = RobotKey("app")
    first = MessageStore(path)
    try:
        with first.transaction():
            first.db.execute("INSERT INTO sources(robot,scene,target,message_id,started,received,expires) VALUES(?,?,?,?,?,?,?)",
                             (robot_key(robot), "group", "g", "incoming", 1, 1, 9999999999))
            first.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (robot_key(robot), "pending", "group", "g", "incoming", "digest", 1,
                              "unknown", 1, 1, None, None))
    finally:
        first.close()
    db = sqlite3.connect(path)
    try:
        db.execute("DROP INDEX source_event_id")
        db.execute("ALTER TABLE sources DROP COLUMN event_id")
        db.execute("PRAGMA user_version=3")
        db.commit()
    finally:
        db.close()
    upgraded = MessageStore(path)
    try:
        assert upgraded.db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert upgraded.db.execute("SELECT event_id FROM sources WHERE message_id='incoming'").fetchone()[0] is None
        assert upgraded.operation(robot, "pending")["state"] == "unknown"
    finally:
        upgraded.close()

async def test_extension_v3_to_v4_adds_owned_tool_operations_without_erasing_unknown(tmp_path):
    path = tmp_path / "messages.sqlite3"
    robot = RobotKey("app")
    messages = MessageStore(path)
    extension = ExtensionStore(messages)
    try:
        extension.begin(robot, "legacy-unknown", "group_mute", "group:g/member:u")
        with messages.transaction():
            messages.db.execute("UPDATE extension_ops SET state='unknown' WHERE op_id='legacy-unknown'")
        assert messages.db.execute("SELECT version FROM extension_schema").fetchone()[0] == 4
    finally:
        await extension.close()
        messages.close()
    db = sqlite3.connect(path)
    db.execute("DROP TABLE tool_ops")
    db.execute("UPDATE extension_schema SET version=3")
    db.commit()
    db.close()
    reopened = MessageStore(path)
    upgraded = ExtensionStore(reopened)
    try:
        assert reopened.db.execute("PRAGMA user_version").fetchone()[0] == 4
        assert reopened.db.execute("SELECT version FROM extension_schema").fetchone()[0] == 4
        assert upgraded.operation(robot, "legacy-unknown")["state"] == "unknown"
        upgraded.claim_tool(robot, "tool-operation", "p", "g", "actor", "p:GroupMessage:g", "mute_members", {"members": ["u"]})
        assert upgraded.tool_owner(robot, "tool-operation", "p", "g", "actor", "p:GroupMessage:g") == "mute_members"
    finally:
        await upgraded.close()
        reopened.close()
    db = sqlite3.connect(path)
    db.execute("UPDATE extension_schema SET version=5")
    db.commit()
    db.close()
    check = MessageStore(path)
    try:
        with pytest.raises(V2Error) as future:
            ExtensionStore(check)
        assert future.value.code == "extension_state_corrupt"
    finally:
        check.close()




def test_cross_lane_binding_and_sequence_collision(tmp_path):
    robot = RobotKey("app")
    route = SessionRoute(robot, "group", "g")
    messages = MessageStore(tmp_path / "messages.sqlite3")
    extension = ExtensionStore(messages)
    try:
        with messages.transaction():
            messages._bind_operation(robot, "op1", "send", "abc")
        with pytest.raises(V2Error) as exc:
            extension.begin(robot, "op1", "group_kick", "xyz")
        assert exc.value.code == "operation_conflict"
        assert messages.claim_sequence(route, "message", "source", "op2", 2, lane="extension", binding="same") == 2
        assert messages.claim_sequence(route, "message", "source", "op2", 2, lane="extension", binding="same") == 2
        with pytest.raises(V2Error, match="another write"):
            messages.claim_sequence(route, "message", "source", "op2", 2, lane="extension", binding="changed")
        assert messages.claim_sequence(route, "message", "source", "op3", 1, lane="extension", binding="same") == 1  # Unused lower number is valid.
        with pytest.raises(V2Error, match="already claimed"):
            messages.claim_sequence(route, "message", "source", "op4", 2, lane="extension", binding="same")
        with pytest.raises(V2Error, match="another source"):
            messages.claim_sequence(SessionRoute(robot, "group", "other"), "message", "source", "op2", 2, lane="extension", binding="same")
        assert messages.claim_sequence(route, "event", "source", "op5", None, lane="extension", binding="same") == 1
        assert messages.claim_sequence(route, "message", "source", "op6", None, lane="extension", binding="same") == 3
        with messages.transaction():
            messages.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (robot_key(robot), "legacy", "group", "g", "observed", "digest", 4,
                 "sent", 1, 1, '{"message_id":"server"}', None))
        with pytest.raises(V2Error, match="retained send"):
            messages.claim_sequence(route, "message", "observed", "op7", 4, lane="extension", binding="same")
        assert messages.claim_sequence(route, "message", "observed", "op8", lane="extension", binding="same") == 5
        messages.prune()
        assert messages.db.execute("SELECT seq FROM sdk_sequences WHERE robot=? AND op_id='op2'", (robot_key(robot),)).fetchone()[0] == 2
    finally:
        messages.close()


async def test_group_ban_derived_expiry_is_frozen_before_extension_binding(management):
    m = management
    first = await m.service.group_ban("g", "u", 60, operation_id="ban-frozen")
    original = [body for method, path, _, body in m.calls if method == "POST" and path.endswith("restrict_chat_setting")]
    assert len(original) == 1
    m.clock[0] += 120
    repeated = await m.service.group_ban("g", "u", 60, operation_id="ban-frozen")
    assert repeated == first
    assert [body for method, path, _, body in m.calls if method == "POST" and path.endswith("restrict_chat_setting")] == original
    with pytest.raises(V2Error) as exc:
        await m.service.group_ban("g", "u", 61, operation_id="ban-frozen")
    assert exc.value.code == "operation_conflict"
    with pytest.raises(V2Error) as target:
        await m.service.group_ban("other", "u", 60, operation_id="ban-frozen")
    assert target.value.code == "operation_conflict"
    m.policy["management_writes"] = False
    with pytest.raises(V2Error) as disabled:
        await m.service.group_ban("g", "u", 60, operation_id="ban-frozen")
    assert disabled.value.code == "management_disabled"
    assert len([method for method, path, *_ in m.calls if method == "POST" and path.endswith("restrict_chat_setting")]) == 1


async def test_group_ban_parallel_calls_do_not_write_twice(management):
    m = management
    outcomes = await asyncio.gather(
        m.service.group_ban("g", "u", 30, operation_id="parallel-ban"),
        m.service.group_ban("g", "u", 30, operation_id="parallel-ban"), return_exceptions=True)
    assert sum(not isinstance(result, BaseException) for result in outcomes) >= 1
    assert all(not isinstance(result, BaseException) or
               isinstance(result, V2Error) and result.code in {"operation_already_attempted", "extension_result_unknown"}
               for result in outcomes)
    assert len([method for method, path, *_ in m.calls if method == "POST" and path.endswith("restrict_chat_setting")]) == 1


async def test_group_ban_receipt_expiry_does_not_reuse_stale_frozen_write(management):
    m = management
    await m.service.group_ban("g", "u", 60, operation_id="ban-expired")
    with m.store.transaction():
        m.store.db.execute("DELETE FROM extension_ops WHERE op_id='ban-expired'")
    before = len([1 for method, path, *_ in m.calls if method == "POST" and path.endswith("restrict_chat_setting")])
    with pytest.raises(V2Error) as exc:
        await m.service.group_ban("g", "u", 60, operation_id="ban-expired")
    assert exc.value.code == "operation_result_not_retained"
    assert len([1 for method, path, *_ in m.calls if method == "POST" and path.endswith("restrict_chat_setting")]) == before


async def test_group_ban_unknown_result_never_replays(management):
    m = management
    await m.service.group_ban("g", "u", 60, operation_id="ban-unknown")
    with m.store.transaction():
        m.store.db.execute("UPDATE extension_ops SET state='unknown' WHERE op_id='ban-unknown'")
    before = len(m.calls)
    with pytest.raises(V2Error) as exc:
        await m.service.group_ban("g", "u", 60, operation_id="ban-unknown")
    assert exc.value.code == "extension_result_unknown"
    assert len([method for method, path, *_ in m.calls[before:] if method == "POST" and path.endswith("restrict_chat_setting")]) == 0
