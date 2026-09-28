"""Native interaction ACK recovery requires a matching durable request and real SQLite state."""

import json
from types import SimpleNamespace

import pytest

from v2.errors import V2Error
from v2.extensions.interactions import ExtensionDispatcher
from v2.extensions.state import ExtensionStore, digest
from v2.messaging.store import MessageStore, robot_key
from v2.models import InstanceKey, RobotKey
from v2.protocol import RequestSpec

NOW = 10_000.0


class ACKHTTP:
    def __init__(self, identity, calls):
        self.identity, self.calls = identity, calls

    async def request(self, spec, *, before_send=None):
        if before_send is not None:
            before_send()
        self.calls.append((spec.method, spec.url, spec.json_body))
        return SimpleNamespace(data={}, status=200)

    async def close(self):
        pass


def lane(path, clock, calls):
    identity = InstanceKey("platform", RobotKey("app"))
    messages = MessageStore(path, clock=lambda: clock[0])
    extension = ExtensionStore(messages)
    owner = SimpleNamespace(messages=messages, extension_state=extension)
    adapter = SimpleNamespace(identity=identity, owner=owner, check_generation=lambda: None)
    dispatcher = ExtensionDispatcher(adapter, ACKHTTP(identity, calls))
    return SimpleNamespace(messages=messages, extension=extension, adapter=adapter, dispatcher=dispatcher)


async def close_lane(value):
    await value.dispatcher.close()
    await value.extension.close()
    value.messages.close()


def observed(value, *, code=3, op_id="ack-op", claimed=True):
    metadata = {"interaction_type": 14, "sent_at": NOW - 1, "received_at": NOW - 1}
    if claimed:
        metadata["native_ack"] = {"code": code, "operation_id": op_id}
    with value.messages.transaction():
        value.messages.db.execute("INSERT INTO extension_events VALUES(?,?,?,?,?,?,?,?,?)",
            (robot_key(value.adapter.identity.robot), "interaction:owned", "INTERACTION_CREATE", NOW,
             json.dumps(metadata), "pending" if claimed else "not_required", "typed_notice", None, NOW))


def ack_spec(identity, *, interaction="owned", code=3):
    return RequestSpec(identity.robot.environment, "PUT", "/interactions/" + interaction, json_body={"code": code})


async def test_confirmed_native_ack_survives_crash_and_uses_original_operation(tmp_path):
    path, clock, calls = tmp_path / "messages.sqlite3", [NOW], []
    first = lane(path, clock, calls)
    try:
        observed(first)
        result = await first.extension.execute(first.dispatcher.ack_http, ack_spec(first.adapter.identity),
            op_id="ack-op", kind="interaction_ack", priority=True, validate=lambda data: {"code": 3})
        assert result == {"code": 3} and len(calls) == 1
        row = first.messages.db.execute("SELECT ack FROM extension_events WHERE event_key='interaction:owned'").fetchone()
        assert row["ack"] == "pending"
    finally:
        await close_lane(first)
    clock[0] = NOW + 301
    reopened = lane(path, clock, calls)
    try:
        assert reopened.messages.db.execute("SELECT ack FROM extension_events WHERE event_key='interaction:owned'").fetchone()[0] == "unknown"
        with pytest.raises(V2Error) as different:
            await reopened.dispatcher.reply_interaction("owned", 4)
        assert different.value.code == "operation_conflict"
        assert await reopened.dispatcher.reply_interaction("owned", 3, operation_id="new-id") == {}
        assert len(calls) == 1
        assert reopened.messages.db.execute("SELECT ack FROM extension_events WHERE event_key='interaction:owned'").fetchone()[0] == "succeeded"
        with pytest.raises(V2Error) as revoked:
            await reopened.dispatcher.reply_interaction("owned", 3,
                guard=lambda: (_ for _ in ()).throw(V2Error("owner_revoked", "fixture", status=409)))
        assert revoked.value.code == "owner_revoked"
        check = reopened.adapter.check_generation
        def stale():
            raise V2Error("stale_generation", "fixture", status=409)
        reopened.adapter.check_generation = stale
        with pytest.raises(V2Error) as outdated:
            await reopened.dispatcher.reply_interaction("owned", 3)
        assert outdated.value.code == "stale_generation" and len(calls) == 1
        reopened.adapter.check_generation = check
        observed_expired = {"interaction_type": 14, "sent_at": NOW - 1, "received_at": NOW - 1}
        with reopened.messages.transaction():
            reopened.messages.db.execute("INSERT INTO extension_events VALUES(?,?,?,?,?,?,?,?,?)",
                (robot_key(reopened.adapter.identity.robot), "interaction:expired-new", "INTERACTION_CREATE", NOW,
                 json.dumps(observed_expired), "not_required", "typed_notice", None, NOW))
        with pytest.raises(V2Error) as expired:
            await reopened.dispatcher.reply_interaction("expired-new", 0)
        assert expired.value.code == "interaction_expired" and len(calls) == 1
        await reopened.dispatcher.close()
        with pytest.raises(V2Error) as closed:
            await reopened.dispatcher.reply_interaction("owned", 3)
        assert closed.value.code == "service_stopped"
    finally:
        await close_lane(reopened)


@pytest.mark.parametrize("variant", [
    "other_robot", "other_kind", "other_interaction", "other_method", "other_code", "wrong_result",
    "unknown", "missing", "history_evicted",
])
async def test_native_ack_recovery_rejects_unrelated_or_unconfirmed_operations(tmp_path, variant):
    path, clock, calls = tmp_path / "messages.sqlite3", [NOW], []
    first = lane(path, clock, calls)
    try:
        robot = RobotKey("other") if variant == "other_robot" else first.adapter.identity.robot
        spec = ack_spec(first.adapter.identity,
            interaction="another" if variant == "other_interaction" else "owned",
            code=4 if variant == "other_code" else 3)
        if variant == "other_method":
            spec = RequestSpec(first.adapter.identity.robot.environment, "POST",
                               "/interactions/owned", json_body={"code": 3})
        kind = "group_mute" if variant == "other_kind" else "interaction_ack"
        if variant != "missing":
            first.extension.begin(robot, "ack-op", kind,
                digest([spec.method, spec.path, spec.params, spec.json_body]))
            first.extension.attempt(robot, "ack-op")
            outcome = "unknown" if variant == "unknown" else "succeeded"
            first.extension.finish(robot, "ack-op", outcome,
                result={"code": 4 if variant in {"other_code", "wrong_result"} else 3} if outcome == "succeeded" else None)
            if variant == "history_evicted":
                with first.messages.transaction():
                    first.messages.db.execute("UPDATE extension_ops SET state='history_evicted',result=NULL WHERE robot=? AND op_id='ack-op'",
                                              (robot_key(robot),))
        if variant in {"other_kind", "other_interaction", "other_method", "other_code"}:
            observed(first, claimed=False)
            with pytest.raises(V2Error) as collision:
                await first.dispatcher.reply_interaction("owned", 3, operation_id="ack-op")
            assert collision.value.code == "operation_conflict"
        else:
            observed(first)
        assert not calls
    finally:
        await close_lane(first)
    reopened = lane(path, clock, calls)
    try:
        assert reopened.messages.db.execute("SELECT ack FROM extension_events WHERE event_key='interaction:owned'").fetchone()[0] == "unknown"
        with pytest.raises(V2Error) as unconfirmed:
            await reopened.dispatcher.reply_interaction("owned", 3, operation_id="new-id")
        assert unconfirmed.value.code == "extension_result_unknown"
        assert not calls
        assert reopened.messages.db.execute("SELECT ack FROM extension_events WHERE event_key='interaction:owned'").fetchone()[0] == "unknown"
    finally:
        await close_lane(reopened)
