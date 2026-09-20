"""Real chat observations, durable quotas and crash recovery, without network."""
from datetime import UTC, datetime

import pytest

from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, RobotKey, SessionRoute
from v2.protocol import RawEnvelope

NOW = 1800000000.0


def chat_payload(event="GROUP_AT_MESSAGE_CREATE", *, message_id="msg-one", target="group-one", sender="user-one", text="/v2menu", timestamp=NOW):
    scene = {"C2C_MESSAGE_CREATE": "c2c", "DIRECT_MESSAGE_CREATE": "dm", "MESSAGE_CREATE": "channel", "AT_MESSAGE_CREATE": "channel"}.get(event, "group")
    field = {"group": "member_openid", "c2c": "user_openid", "channel": "id", "dm": "id"}[scene]
    data = {"id": message_id, "author": {field: sender, "username": "same-name"}, "content": text,
            "timestamp": datetime.fromtimestamp(timestamp, UTC).isoformat(), "message_type": 0}
    if scene == "group":
        data["group_openid"] = target
    elif scene in ("channel", "dm"):
        data.update(channel_id=target if scene == "channel" else "dm-channel", guild_id="guild-one" if scene == "channel" else target)
    if scene in ("c2c", "group"):
        data["message_scene"] = {"source": "default", "ext": [f"msg_idx=REFIDX_{message_id}", "auth_token=fixture-not-for-identity-db"]}
    return {"op": 0, "s": 12, "t": event, "id": "event-" + message_id, "d": data}


def observed(config, event="GROUP_AT_MESSAGE_CREATE", **kwargs):
    return convert_chat(InstanceKey.from_config(config), RawEnvelope(chat_payload(event, **kwargs), NOW))


@pytest.fixture
def clock():
    return [NOW]


@pytest.fixture
def state(tmp_path, clock):
    store = MessageStore(tmp_path / "messaging.sqlite3", clock=lambda: clock[0])
    yield store
    store.close()


@pytest.mark.parametrize("event,scene", [("GROUP_AT_MESSAGE_CREATE", "group"), ("GROUP_MESSAGE_CREATE", "group"),
    ("C2C_MESSAGE_CREATE", "c2c"), ("AT_MESSAGE_CREATE", "channel"), ("MESSAGE_CREATE", "channel"), ("DIRECT_MESSAGE_CREATE", "dm")])
def test_six_real_message_shapes_keep_identifiers(config, event, scene):
    chat = observed(config, event)
    assert chat.route.scene == scene and SessionRoute.decode(chat.message.session_id) == chat.route
    assert chat.message.message_id == "msg-one" and chat.source.event_id == "event-msg-one"
    assert chat.source.interaction_id is None and chat.source.received_at == NOW and chat.source.sent_at == NOW
    assert chat.message.timestamp == NOW and chat.message.sender.user_id == "user-one"
    assert chat.message.self_id != config["appid"]
    assert chat.message.raw_message == chat_payload(event)
    assert chat.source.ref_idx == ("REFIDX_msg-one" if scene in ("group", "c2c") else None)
    assert chat.message.message_str == "/v2menu"


def test_identity_provenance_isolation_ttl_and_restart(config, tmp_path, clock):
    path = tmp_path / "persistent"
    store = MessageStore(path, clock=lambda: clock[0], identity_capacity=2)
    chat = observed(config)
    payload = chat_payload()
    payload["d"]["mentions"] = [{"member_openid": "mentioned", "username": "same-name"}]
    payload["d"]["ark_data"] = {"fields": {"nickname": "fake", "avatar": "https://q.qlogo.cn/fake", "member_openid": "not-observed"}}
    chat = convert_chat(chat.identity, RawEnvelope(payload, NOW))
    store.observe(chat)
    row = store.lookup(chat.route.robot, "member_openid", "group:group-one", "mentioned")
    assert row["source_message_id"] == "msg-one" and row["first_seen"] == NOW
    for robot, kind, scope, user in [(RobotKey("other"), "member_openid", "group:group-one", "user-one"),
        (RobotKey(config["appid"], "sandbox"), "member_openid", "group:group-one", "user-one"),
        (chat.route.robot, "user_openid", "group:group-one", "user-one"),
        (chat.route.robot, "member_openid", "group:other", "user-one"),
        (chat.route.robot, "member_openid", "group:group-one", "not-observed")]:
        with pytest.raises(V2Error, match="observation"):
            store.lookup(robot, kind, scope, user)
    store.close()
    store = MessageStore(path, clock=lambda: clock[0], identity_capacity=2)
    try:
        assert store.lookup(chat.route.robot, "member_openid", "group:group-one", "user-one")["nickname"] == "same-name"
        dump = "\n".join(store.db.iterdump())
        assert "fixture-not-for-identity-db" not in dump and "/v2menu" not in dump and config["secret"] not in dump
        clock[0] += 86401
        with pytest.raises(V2Error) as exc:
            store.lookup(chat.route.robot, "member_openid", "group:group-one", "user-one")
        assert exc.value.code == "identity_not_observed"
    finally:
        store.close()


def test_passive_reservations_share_seq_and_do_not_revive(config, state, clock):
    chat = observed(config)
    state.observe(chat)
    operations = [state.reserve(chat.route, chat.source, "digest", f"op{i}") for i in range(5)]
    assert [op["seq"] for op in operations] == [1, 2, 3, 4, 5]
    with pytest.raises(V2Error) as exc:
        state.reserve(chat.route, chat.source, "digest", "overflow")
    assert exc.value.code == "passive_quota_exhausted"
    state.finish(chat.route.robot, "op0", "not_sent")
    assert state.reserve(chat.route, chat.source, "digest", "op5")["seq"] == 6
    state.mark_in_flight(chat.route.robot, "op1")
    state.finish(chat.route.robot, "op1", "unknown")
    clock[0] += 301
    state.prune()
    state.observe(convert_chat(chat.identity, RawEnvelope(chat_payload(), clock[0])))
    with pytest.raises(V2Error) as exc:
        state.reserve(chat.route, chat.source, "digest", "expired")
    assert exc.value.code == "reply_expired"


def test_crash_recovery_unknown_no_replay_or_refund(config, tmp_path, clock):
    path = tmp_path / "recovery"
    store = MessageStore(path, clock=lambda: clock[0])
    chat = observed(config)
    store.observe(chat)
    store.reserve(chat.route, chat.source, "digest", "inflight")
    store.mark_in_flight(chat.route.robot, "inflight")
    store.reserve(chat.route, chat.source, "digest", "queued")
    store.close()
    store = MessageStore(path, clock=lambda: clock[0])
    try:
        assert store.operation(chat.route.robot, "inflight")["state"] == "unknown"
        assert store.operation(chat.route.robot, "queued")["state"] == "not_sent"
        with pytest.raises(V2Error) as exc:
            store.reserve(chat.route, chat.source, "digest", "inflight")
        assert exc.value.code == "send_result_unknown"
        with pytest.raises(V2Error) as exc:
            store.reserve(chat.route, chat.source, "different", "queued")
        assert exc.value.code == "operation_conflict"
        assert store.reserve(chat.route, chat.source, "digest", "new")["seq"] == 3
    finally:
        store.close()


def test_no_unknown_eviction_and_different_shards_share_quota(config, tmp_path):
    store = MessageStore(tmp_path / "capacity", clock=lambda: NOW, operation_capacity=2)
    chat = observed(config)
    other = observed({**config, "id": "second", "shard": [1, 2]})
    try:
        store.observe(chat)
        store.observe(other)
        first = store.reserve(chat.route, chat.source, "x", "first")
        store.mark_in_flight(chat.route.robot, "first")
        store.finish(chat.route.robot, "first", "unknown")
        second = store.reserve(other.route, other.source, "x", "second")
        assert first["seq"] == 1 and second["seq"] == 2
        with pytest.raises(V2Error) as exc:
            store.reserve(chat.route, chat.source, "x", "third")
        assert exc.value.code == "message_state_full"
        assert store.operation(chat.route.robot, "first")["state"] == "unknown"
    finally:
        store.close()


@pytest.mark.parametrize("mutation", ["depth", "nodes", "bytes", "missing_sender", "missing_time", "projected"])
def test_bad_chats_fail_without_invented_fields(config, mutation):
    payload = chat_payload()
    if mutation == "depth":
        child = payload["d"]
        for _ in range(12):
            child["msg_elements"] = [{}]
            child = child["msg_elements"][0]
    elif mutation == "nodes":
        payload["d"]["msg_elements"] = [{}] * 300
    elif mutation == "bytes":
        payload["d"]["content"] = "x" * (128 * 1024)
    elif mutation == "missing_sender":
        payload["d"]["author"] = {"username": "not-an-id"}
    elif mutation == "missing_time":
        del payload["d"]["timestamp"]
    else:
        payload["derived_from_interaction"] = True
    with pytest.raises(V2Error):
        convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))


def test_structured_mentions_references_attachments_and_isolation(config):
    payload = chat_payload()
    payload["d"].update(content="literal <@made-up>", mentions=[{"member_openid": "mentioned"}],
        attachments=[{"url": "https://multimedia.nt.qq.com.cn/download?fileid=fixture", "content_type": "image/png", "size": 64}],
        msg_elements=[{"message_type": 103, "msg_idx": "REFIDX_old", "author": {"member_openid": "quoted"}, "content": "quoted text"}])
    payload["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_old")
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW), isolated=True)
    from astrbot.core.message.components import At, Reply
    assert chat.route.user == "user-one" and chat.message.raw_message == payload
    assert [c.qq for c in chat.message.message if isinstance(c, At)] == ["mentioned"]
    reply = next(c for c in chat.message.message if isinstance(c, Reply))
    assert reply.id == "REFIDX_old" and reply.sender_id == "quoted"
    assert {o["user_id"] for o in chat.observations} == {"user-one", "mentioned", "quoted"}
    assert chat.attachments[0]["size"] == 64 and "literal <@made-up>" == chat.message.message_str


def test_expired_source_stays_expired_after_clock_rollback_and_restart(config, tmp_path):
    clock = [NOW]
    path = tmp_path / "clock"
    state = MessageStore(path, clock=lambda: clock[0])
    chat = observed(config)
    state.observe(chat)
    clock[0] += 301
    with pytest.raises(V2Error) as exc:
        state.reserve(chat.route, chat.source, "x", "expired")
    assert exc.value.code == "reply_expired"
    state.close()
    clock[0] = NOW
    state = MessageStore(path, clock=lambda: clock[0])
    try:
        with pytest.raises(V2Error) as exc:
            state.reserve(chat.route, chat.source, "x", "still-expired")
        assert exc.value.code == "reply_expired"
    finally:
        state.close()


def test_forwarded_profiles_do_not_create_observed_identities(config):
    payload = chat_payload()
    payload["d"]["msg_elements"] = [{"message_type": 102, "author": {"member_openid": "forwarded-fake"}, "content": "history"}]
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))
    assert {item["user_id"] for item in chat.observations} == {"user-one"}


def test_message_store_exclusive_lease_and_reopen(tmp_path):
    path = tmp_path / "state.sqlite3"
    first = MessageStore(path)
    try:
        with pytest.raises(V2Error) as exc:
            MessageStore(path)
        assert exc.value.code == "message_state_in_use"
    finally:
        first.close()
    second = MessageStore(path)
    second.close()


def test_same_message_id_with_different_indices_is_delivered_but_not_ambiguously_quoted(config, state):
    first = observed(config)
    state.observe(first)
    state.mark_delivered(first)
    payload = chat_payload()
    payload["id"] = "different-envelope"
    payload["d"]["message_scene"]["ext"] = ["msg_idx=REFIDX_second"]
    second = convert_chat(first.identity, RawEnvelope(payload, NOW))
    state.observe(second)
    assert not state.delivered(second)
    state.mark_delivered(second)
    assert state.delivered(second)
    assert state.reference(second.route, "REFIDX_second") == "REFIDX_second"
    with pytest.raises(V2Error) as exc:
        state.reference(second.route, "msg-one")
    assert exc.value.code == "reference_not_observed"
    assert state.reserve(first.route, first.source, "one", "first")["seq"] == 1
    assert state.reserve(second.route, second.source, "two", "second")["seq"] == 2


def test_identity_observation_time_is_not_the_older_message_time(config, state):
    chat = observed(config, timestamp=NOW - 120)
    state.observe(chat)
    record = state.lookup(chat.route.robot, "member_openid", "group:group-one", "user-one")
    assert record["first_seen"] == record["last_seen"] == NOW
    assert chat.source.sent_at == NOW - 120 and chat.source.expires == NOW + 180
