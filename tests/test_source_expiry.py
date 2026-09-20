import pytest
from test_messaging_state import chat_payload, observed
from test_messaging_state import clock as clock
from test_messaging_state import state as state

from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.store import MessageStore, robot_key
from v2.models import InstanceKey
from v2.protocol import RawEnvelope


@pytest.mark.parametrize("event", ["GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
@pytest.mark.parametrize("outcome", [None, "reserved", "in_flight", "unknown", "sent", "rejected", "not_sent"])
def test_sources_expire_on_boundary_except_protected_operations(state, clock, config, event, outcome):
    chat = observed(config, event)
    state.observe(chat)
    state.mark_delivered(chat)
    if outcome:
        state.reserve(chat.route, chat.source, "digest", "operation")
        if outcome != "reserved":
            state.mark_in_flight(chat.route.robot, "operation")
        if outcome not in {"reserved", "in_flight"}:
            state.finish(chat.route.robot, "operation", outcome, result={"message_id": "real-result"} if outcome == "sent" else None)
    clock[0] = chat.source.expires - 0.001
    state.prune()
    assert state.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
    before = state.operation(chat.route.robot, "operation") if outcome else None
    clock[0] = chat.source.expires
    state.prune()
    protected = outcome in {"reserved", "in_flight", "unknown"}
    assert state.db.execute("SELECT count(*) FROM sources").fetchone()[0] == int(protected)
    if protected:
        row = state.db.execute("SELECT seq,used FROM sources").fetchone()
        assert tuple(row) == (1, 1)
    if outcome:
        assert state.operation(chat.route.robot, "operation") == before
    assert state.delivered(chat)
    assert state.reference(chat.route, chat.source.message_id) == chat.source.ref_idx
    with pytest.raises(V2Error) as exc:
        state.reserve(chat.route, chat.source, "new-digest", "new-operation")
    assert exc.value.code == "reply_expired"
    if outcome == "sent":
        assert state.reserve(chat.route, chat.source, "digest", "operation")["result"] == {"message_id": "real-result"}
    clock[0] -= 120
    with pytest.raises(V2Error) as exc:
        state.check_source(chat.route, chat.source)
    assert exc.value.code == "reply_expired"


def test_expired_sources_release_their_budget_before_next_chat(config, tmp_path, clock):
    store = MessageStore(tmp_path / "state.db", clock=lambda: clock[0], source_capacity=2)
    try:
        for number in range(3):
            payload = chat_payload("AT_MESSAGE_CREATE", message_id=f"source-{number}", timestamp=clock[0])
            chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, clock[0]))
            store.observe(chat)
            store.mark_delivered(chat)
            clock[0] = chat.source.expires
        store.prune()
        assert store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
        assert store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 3
        assert store.db.execute("SELECT count(*) FROM refs").fetchone()[0] == 3
    finally:
        store.close()


def test_source_protection_is_scoped_to_robot_scene_target_and_id(state, config, clock):
    protected = observed(config)
    alternatives = [observed(config, target="another-group"), observed(config, "C2C_MESSAGE_CREATE"),
                    observed({**config, "appid": "other-app"}), observed({**config, "environment": "sandbox"})]
    for chat in [protected, *alternatives]:
        state.observe(chat)
    state.reserve(protected.route, protected.source, "digest", "unknown-op")
    state.mark_in_flight(protected.route.robot, "unknown-op")
    state.finish(protected.route.robot, "unknown-op", "unknown")
    clock[0] = max(chat.source.expires for chat in [protected, *alternatives])
    state.prune()
    rows = state.db.execute("SELECT robot,scene,target,message_id FROM sources").fetchall()
    assert [tuple(row) for row in rows] == [(robot_key(protected.route.robot), protected.route.scene, protected.route.target, protected.source.message_id)]
    assert state.operation(protected.route.robot, "unknown-op")["state"] == "unknown"


def test_restart_prunes_released_reservations_but_preserves_unknown_sources(config, tmp_path, clock):
    path = tmp_path / "restart.db"
    store = MessageStore(path, clock=lambda: clock[0])
    try:
        chats = [observed(config, message_id=mode) for mode in ("reserved", "in_flight")]
        for chat in chats:
            store.observe(chat)
            store.reserve(chat.route, chat.source, "digest", chat.source.message_id)
        store.mark_in_flight(chats[1].route.robot, "in_flight")
        clock[0] = chats[0].source.expires
    finally:
        store.close()
    restored = MessageStore(path, clock=lambda: clock[0])
    try:
        restored.prune()
        assert restored.db.execute("SELECT message_id FROM sources").fetchall()[0][0] == "in_flight"
        assert restored.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 1
        assert restored.operation(chats[0].route.robot, "reserved")["state"] == "not_sent"
        assert restored.operation(chats[1].route.robot, "in_flight")["state"] == "unknown"
    finally:
        restored.close()
