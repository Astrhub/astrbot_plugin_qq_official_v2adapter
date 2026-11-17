"""Local message history is isolated from delivery ledgers and backs get_msg."""
import json
import sqlite3
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import Reply

from test_messaging_delivery import accept, receiver as receiver
from test_messaging_state import NOW, chat_payload, observed

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.history import HistoryStore, apply_reply_time, resolve_reply_time
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, RobotKey, SessionRoute
from v2.protocol import RawEnvelope
from v2.web_api import ControlAPI


def history_store(tmp_path, clock=None):
    ticks = clock or [NOW]
    store = HistoryStore(tmp_path / "history.sqlite3", clock=lambda: ticks[0])
    return store, ticks


def recorded(config, store, **kwargs):
    chat = observed(config, **kwargs)
    assert store.record(chat) is True
    return chat


def owner_key(chat):
    return chat.identity.settings_key


def reply_of(chat):
    return next(part for part in chat.message.message if isinstance(part, Reply))


def quoted_chat(config, *, message_id="quote-one", ref="REFIDX_prior", event="GROUP_AT_MESSAGE_CREATE", **kwargs):
    payload = chat_payload(event, message_id=message_id, **kwargs)
    if event in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"}:
        payload["d"]["message_type"] = 103
        payload["d"]["message_scene"]["ext"].append(f"ref_msg_idx={ref}")
        payload["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": ref, "content": "prior"}]
    else:
        payload["d"]["message_reference"] = {"message_id": ref}
    return convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))


def test_history_default_unlimited_and_restart(config, tmp_path):
    store, ticks = history_store(tmp_path)
    chat = recorded(config, store)
    path = tmp_path / "history.sqlite3"
    assert path.stat().st_mode & 0o777 == 0o600
    wal = tmp_path / "history.sqlite3-wal"
    if wal.exists():
        assert wal.stat().st_mode & 0o777 == 0o600
    event = store.get_msg(owner_key(chat), "msg-one")
    assert event["message_id"] == "msg-one"
    assert event["group_id"] == "group-one"
    assert event["_qq"]["scene"] == "group"
    assert store.policy(owner_key(chat))["mode"] == "unlimited"
    ticks[0] = NOW + 365 * 86400
    recorded(config, store, message_id="later")
    assert store.get_msg(owner_key(chat), "msg-one")["message_id"] == "msg-one"
    store.close()
    restored = HistoryStore(tmp_path / "history.sqlite3", clock=lambda: NOW)
    again = restored.get_msg(owner_key(chat), "msg-one")
    assert again["raw_message"] == chat.message.message_str
    restored.close()


def test_history_is_not_the_delivery_ledger(config, tmp_path):
    messages = MessageStore(tmp_path / "messaging.sqlite3", clock=lambda: NOW)
    history, _ = history_store(tmp_path)
    chat = recorded(config, history)
    messages.observe(chat)
    messages.mark_delivered(chat)
    messages.reserve(chat.route, None, "digest", "unknown-op")
    messages.mark_in_flight(chat.route.robot, "unknown-op")
    messages.finish(chat.route.robot, "unknown-op", "unknown")
    names = {row[0] for row in history.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "operations" not in names and "deliveries" not in names
    assert messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
    assert messages.db.execute("SELECT count(*) FROM operations WHERE state='unknown'").fetchone()[0] == 1
    with pytest.raises(V2Error) as unfiltered:
        history.delete_range(owner_key(chat))
    assert unfiltered.value.code == "invalid_history_range"
    with pytest.raises(V2Error) as empty:
        history.delete_range(owner_key(chat), scene="", target="")
    assert empty.value.code == "invalid_history_range"
    assert history.get_msg(owner_key(chat), "msg-one")["message_id"] == "msg-one"
    history.delete_range(owner_key(chat), target="group-one")
    assert messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
    assert messages.db.execute("SELECT count(*) FROM operations WHERE state='unknown'").fetchone()[0] == 1
    recorded(config, history, message_id="again")
    history.purge(owner_key(chat))
    assert messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
    assert messages.db.execute("SELECT count(*) FROM operations WHERE state='unknown'").fetchone()[0] == 1
    history.close()
    messages.close()


def test_policy_switch_evicts_only_history(config, tmp_path):
    store, _ = history_store(tmp_path)
    first = recorded(config, store, message_id="old", timestamp=NOW)
    recorded(config, store, message_id="new", timestamp=NOW + 10)
    store.save_policy(owner_key(first), {"enabled": True, "mode": "max_count", "max_count": 1})
    with pytest.raises(V2Error) as expired:
        store.get_msg(owner_key(first), "old")
    assert expired.value.code == "message_expired"
    assert store.get_msg(owner_key(first), "new")["message_id"] == "new"
    store.close()


def test_policy_max_age_evicts(config, tmp_path):
    store, ticks = history_store(tmp_path)
    first = recorded(config, store, message_id="old", timestamp=NOW)
    ticks[0] = NOW + 10
    recorded(config, store, message_id="mid", timestamp=NOW + 10)
    ticks[0] = NOW + 50
    recorded(config, store, message_id="new", timestamp=NOW + 50)
    store.save_policy(owner_key(first), {"enabled": True, "mode": "max_age", "max_age_seconds": 20})
    with pytest.raises(V2Error) as expired:
        store.get_msg(owner_key(first), "old")
    assert expired.value.code == "message_expired"
    with pytest.raises(V2Error) as expired_mid:
        store.get_msg(owner_key(first), "mid")
    assert expired_mid.value.code == "message_expired"
    assert store.get_msg(owner_key(first), "new")["message_id"] == "new"
    store.close()


def test_disk_error_does_not_delete_history(config, tmp_path, monkeypatch):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store)
    original = store.db

    class Boom:
        def execute(self, sql, params=()):
            if isinstance(sql, str) and sql.startswith("INSERT INTO messages"):
                raise sqlite3.OperationalError("database or disk is full")
            return original.execute(sql, params)

        def __enter__(self):
            return original.__enter__()

        def __exit__(self, *args):
            return original.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original, name)

    store.db = Boom()
    later = observed(config, message_id="second")
    assert store.record(later) is False
    assert store.last_error == "history_storage_unavailable"
    store.db = original
    assert store.get_msg(owner_key(chat), "msg-one")["message_id"] == "msg-one"
    store.close()


def test_cross_bot_and_bound_route(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store)
    other = InstanceKey("other", RobotKey("other-app")).settings_key
    with pytest.raises(V2Error) as missing:
        store.get_msg(other, "msg-one")
    assert missing.value.code == "message_not_found"
    with pytest.raises(V2Error) as routed:
        store.get_msg(owner_key(chat), "msg-one", scene="group", target="other-group")
    assert routed.value.code == "message_route_unavailable"
    store.close()


def test_same_appid_different_platform_is_isolated(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store)
    sibling = dict(config)
    sibling["id"] = "other-v2"
    other = observed(sibling)
    assert other.identity.robot.appid == chat.identity.robot.appid
    assert owner_key(other) != owner_key(chat)
    row = store.db.execute("SELECT owner FROM messages").fetchone()
    assert row["owner"] == owner_key(chat)
    with pytest.raises(V2Error) as missing:
        store.get_msg(owner_key(other), "msg-one")
    assert missing.value.code == "message_not_found"
    sandbox = InstanceKey(chat.identity.platform_id, RobotKey(chat.identity.robot.appid, "sandbox")).settings_key
    assert sandbox != owner_key(chat)
    with pytest.raises(V2Error) as missing_env:
        store.get_msg(sandbox, "msg-one")
    assert missing_env.value.code == "message_not_found"
    store.close()


def test_get_msg_and_forward_nodes(config, tmp_path):
    store, _ = history_store(tmp_path)
    payload = chat_payload(text="outer")
    payload["d"]["message_type"] = 102
    payload["d"]["msg_elements"] = [
        {"content": "node-one", "author": {"member_openid": "forward-user", "username": "Fwd"}},
        {"message_type": 102, "msg_idx": "nested-fwd", "msg_elements": [
            {"content": "nested", "attachments": [{"content_type": "image/png", "url": "https://fixture.invalid/n.png"}]},
        ]},
    ]
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))
    assert store.record(chat) is True
    event = store.get_msg(owner_key(chat), "msg-one")
    assert any(part["type"] == "forward" for part in event["message"])
    assert isinstance(event["_qq"]["media"], list)
    nested = store.get_forward(owner_key(chat), "nested-fwd")
    assert nested["messages"][0]["content"][0]["data"]["text"] == "nested"
    assert nested["messages"][0]["content"][1]["type"] == "image"
    quoted = chat_payload(message_id="only-ref", text="hi")
    quoted["d"]["message_type"] = 103
    quoted["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_prior")
    quoted["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_prior", "content": "prior"}]
    ref_chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(quoted, NOW))
    assert store.record(ref_chat) is True
    with pytest.raises(V2Error) as no_nodes:
        store.get_forward(owner_key(ref_chat), "only-ref")
    assert no_nodes.value.code == "forward_nodes_unavailable"
    with pytest.raises(V2Error) as unknown:
        store.get_forward(owner_key(chat), "missing-fwd")
    assert unknown.value.code == "forward_not_found"
    store.delete_range(owner_key(chat), target="group-one")
    with pytest.raises(V2Error) as deleted:
        store.get_msg(owner_key(chat), "msg-one")
    assert deleted.value.code == "message_deleted"
    store.close()


def test_sanitize_official_strips_secrets(config, tmp_path):
    store, _ = history_store(tmp_path)
    payload = chat_payload()
    payload["d"]["access_token"] = "leak-token"
    payload["d"]["nested"] = {"authorization": "Bearer x", "ok": 1}
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))
    assert store.record(chat) is True
    official = store.get_msg(owner_key(chat), "msg-one")["_qq"]["official"]
    blob = json.dumps(official)
    assert "access_token" not in blob
    assert "leak-token" not in blob
    assert "authorization" not in blob
    assert "Bearer x" not in blob
    assert "auth_token" not in blob
    assert "fixture-not-for-identity-db" not in blob
    assert official["d"]["nested"]["ok"] == 1
    assert any(isinstance(item, str) and item.startswith("msg_idx=") for item in official["d"]["message_scene"]["ext"])
    store.close()


@pytest.mark.asyncio
async def test_call_action_get_msg_bound_route_and_forward(config, tmp_path):
    store, _ = history_store(tmp_path)
    payload = chat_payload(text="outer")
    payload["d"]["message_type"] = 102
    payload["d"]["msg_elements"] = [
        {"content": "node-one", "author": {"member_openid": "forward-user", "username": "Fwd"}},
        {"message_type": 102, "msg_idx": "nested-fwd", "msg_elements": [
            {"content": "nested", "attachments": [{"content_type": "image/png", "url": "https://fixture.invalid/n.png"}]},
        ]},
    ]
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))
    assert store.record(chat) is True
    identity = InstanceKey(chat.identity.platform_id, chat.identity.robot, generation=chat.identity.generation)
    client = V2Client(identity, state=ClientState(identity))
    client._state.history = store
    event = await client.call_action("get_msg", message_id="msg-one")
    assert event["message_id"] == "msg-one"
    assert any(part["type"] == "forward" for part in event["message"])
    bound = client.bind(SessionRoute(identity.robot, "group", "group-one"))
    got = await bound.call_action("get_msg", message_id="msg-one")
    assert got["group_id"] == "group-one"
    other = client.bind(SessionRoute(identity.robot, "group", "other-group"))
    with pytest.raises(V2Error) as routed:
        await other.call_action("get_msg", message_id="msg-one")
    assert routed.value.code == "message_route_unavailable"
    nested = await client.call_action("get_forward_msg", id="nested-fwd")
    assert nested["messages"][0]["content"][0]["data"]["text"] == "nested"
    assert nested["messages"][0]["content"][1]["type"] == "image"
    quoted = recorded(config, store, message_id="only-ref")
    with pytest.raises(V2Error) as no_nodes:
        await client.call_action("get_forward_msg", message_id=quoted.source.message_id)
    assert no_nodes.value.code == "forward_nodes_unavailable"
    via_api = await client.api.call_action("get_msg", message_id="msg-one")
    assert via_api["message_id"] == "msg-one"
    assert client.api is client
    assert client.capabilities()["actions"]["get_msg"]["support"] == "conditional"
    store.close()
    assert client.capabilities()["actions"]["get_msg"]["support"] == "unsupported"
    assert client.capabilities()["actions"]["get_forward_msg"]["reason"] == "history_not_ready"


@pytest.mark.asyncio
async def test_inbound_write_failure_does_not_block_delivery(receiver, monkeypatch):
    owner, instance = receiver

    def boom(chat):
        raise RuntimeError("history write exploded")

    monkeypatch.setattr(owner.history, "record", boom)
    accept(owner, instance)
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert event.message_obj.message_id == "msg-one"
    assert owner.messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
    event.cleanup_temporary_local_files()


@pytest.mark.asyncio
async def test_inbound_history_write_and_client_lookup(receiver):
    owner, instance = receiver
    accept(owner, instance, text="hello history")
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    got = await instance.client.call_action("get_msg", message_id="msg-one")
    assert got["message_id"] == "msg-one"
    assert "hello history" in got["raw_message"]
    event.cleanup_temporary_local_files()


@pytest.mark.asyncio
async def test_terminate_closes_history(receiver):
    owner, instance = receiver
    assert owner.history is not None and not owner.history.closed
    await owner.terminate()
    assert owner.history.closed


def test_control_history_api_requires_admin_and_isolates_ledger(config, tmp_path, monkeypatch):
    from v2 import web_api as web_api_mod
    store, _ = history_store(tmp_path)
    chat = recorded(config, store)
    identity = InstanceKey.from_config(config)
    owner = SimpleNamespace(
        stopping=False,
        config={"webui_enabled": True, "remote_menu_sync": False},
        context=SimpleNamespace(get_config=lambda: {"platform": [config]}),
        history=store,
        messages=SimpleNamespace(db=SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: (1,)))),
    )
    api = ControlAPI(owner)
    monkeypatch.setattr(web_api_mod, "require_admin", lambda ctx: "admin")
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="GET", query={"platform_id": config["id"]}))
    status = api.dispatch_history("history_status", "admin", {}, identity, "fp")
    assert status["policy"]["mode"] == "unlimited"
    assert status["message_count"] == 1
    rows = api.dispatch_history("history_query", "admin", {}, identity, "fp")
    assert rows["entries"][0]["message_id"] == "msg-one"
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="POST", query={}))
    integrity = api.dispatch_history("history_integrity", "admin", {"fingerprint": "fp"}, identity, "fp")
    assert integrity["ok"] is True
    checkpoint = api.dispatch_history("history_checkpoint", "admin", {"fingerprint": "fp", "confirm": True}, identity, "fp")
    assert "size_bytes" in checkpoint
    exported = api.dispatch_history(
        "history_export", "admin",
        {"fingerprint": "fp", "confirm": True, "limit": 1000},
        identity, "fp",
    )
    assert exported["messages"][0]["message_id"] == "msg-one"
    assert exported["truncated"] is False
    policy = api.dispatch_history(
        "history_policy", "admin",
        {"fingerprint": "fp", "confirm": True, "policy": {"enabled": True, "mode": "unlimited"}},
        identity, "fp",
    )
    assert policy["policy"]["mode"] == "unlimited"
    with pytest.raises(V2Error) as missing_delete:
        api.dispatch_history("history_delete", "admin", {"fingerprint": "fp", "confirm": True}, identity, "fp")
    assert missing_delete.value.code == "confirmation_required"
    with pytest.raises(V2Error) as unfiltered:
        api.dispatch_history("history_delete", "admin",
                             {"fingerprint": "fp", "confirm": True, "confirm_delete": True}, identity, "fp")
    assert unfiltered.value.code == "invalid_history_range"
    deleted = api.dispatch_history(
        "history_delete", "admin",
        {"fingerprint": "fp", "confirm": True, "confirm_delete": True, "target": "group-one"},
        identity, "fp",
    )
    assert deleted["deleted"] == 1
    assert owner.messages.db.execute().fetchone()[0] == 1
    with pytest.raises(V2Error) as confirm:
        api.dispatch_history("history_purge", "admin", {"fingerprint": "fp", "confirm": True, "confirm_text": "nope"}, identity, "fp")
    assert confirm.value.code == "confirmation_required"
    recorded(config, store, message_id="keep-ledger")
    purged = api.dispatch_history(
        "history_purge", "admin",
        {"fingerprint": "fp", "confirm": True, "confirm_text": "DELETE_MESSAGE_HISTORY"},
        identity, "fp",
    )
    assert purged["deleted"] == 1
    assert chat.route.robot.appid == "test-app"
    store.close()


@pytest.mark.asyncio
async def test_history_handle_requires_admin_session(monkeypatch):
    from v2 import web_api as web_api_mod
    from test_web_api import parse
    owner = SimpleNamespace(stopping=False, config={"webui_enabled": True}, context=object(), history=None)
    api = ControlAPI(owner)
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="GET", query={"platform_id": "x"}))
    response = await api.handle("history_status")
    payload = parse(response)
    assert response.status_code == 403 and payload["code"] == "management_auth_required"


def test_control_logs_history_errors_without_bodies(monkeypatch, caplog):
    from v2 import web_api as web_api_mod
    owner = SimpleNamespace(stopping=False, config={"webui_enabled": True}, context=object(), history=None)
    api = ControlAPI(owner)
    monkeypatch.setattr(web_api_mod, "require_admin", lambda ctx: "admin")
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="GET", query={"platform_id": "x"}))
    with caplog.at_level("WARNING"):
        with pytest.raises(V2Error) as exc:
            api._history_store()
    assert exc.value.code == "history_not_ready"
    assert "fixture-app-secret" not in caplog.text


def test_convert_leaves_reply_time_unset(config):
    chat = quoted_chat(config)
    assert reply_of(chat).time is None
    assert reply_of(chat).id == "REFIDX_prior"


@pytest.mark.parametrize("event,ref", [
    ("GROUP_AT_MESSAGE_CREATE", "REFIDX_prior"),
    ("C2C_MESSAGE_CREATE", "REFIDX_prior"),
])
def test_reply_time_unique_hit_uses_quoted_sent_at(config, tmp_path, event, ref):
    store, _ = history_store(tmp_path)
    prior = recorded(config, store, event=event, message_id="prior", timestamp=NOW)
    chat = quoted_chat(config, event=event, ref=ref, timestamp=NOW + 10)
    assert reply_of(chat).time is None
    apply_reply_time(store, chat)
    stamp = reply_of(chat).time
    assert type(stamp) is int
    assert stamp == int(prior.source.sent_at) == int(NOW)
    assert stamp != int(chat.source.sent_at)
    assert reply_of(chat).id == ref
    assert reply_of(chat).message_str == "prior"


def test_reply_time_missing_quoted_message_stays_none(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = quoted_chat(config)
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None
    assert resolve_reply_time(store, chat) is None


def test_reply_time_ambiguous_ref_idx_stays_none(config, tmp_path):
    store, _ = history_store(tmp_path)
    for message_id in ("a", "b"):
        payload = chat_payload(message_id=message_id)
        payload["d"]["message_scene"]["ext"] = ["msg_idx=REFIDX_same", "auth_token=fixture-not-for-identity-db"]
        assert store.record(convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))) is True
    chat = quoted_chat(config, ref="REFIDX_same")
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None


def test_reply_time_is_isolated_by_target_instance_and_environment(config, tmp_path):
    store, _ = history_store(tmp_path)
    prior = recorded(config, store, message_id="prior")
    other_group = quoted_chat(config, ref="REFIDX_prior", target="other-group")
    apply_reply_time(store, other_group)
    assert reply_of(other_group).time is None
    sibling = dict(config)
    sibling["id"] = "other-v2"
    other_instance = quoted_chat(sibling, ref="REFIDX_prior")
    apply_reply_time(store, other_instance)
    assert reply_of(other_instance).time is None
    assert store.quoted_sent_at(
        InstanceKey(prior.identity.platform_id, RobotKey(prior.identity.robot.appid, "sandbox")).settings_key,
        "group", "group-one", "REFIDX_prior") is None
    hit = quoted_chat(config, ref="REFIDX_prior")
    apply_reply_time(store, hit)
    assert reply_of(hit).time == int(prior.source.sent_at)


def test_group_matches_ref_idx_not_message_id(config, tmp_path):
    store, _ = history_store(tmp_path)
    recorded(config, store, message_id="prior")
    assert store.quoted_sent_at(owner_key(observed(config)), "group", "group-one", "prior") is None
    assert store.quoted_sent_at(owner_key(observed(config)), "group", "group-one", "REFIDX_prior") == int(NOW)
    chat = quoted_chat(config, ref="prior")
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None


@pytest.mark.parametrize("event", ["AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
def test_channel_and_dm_match_message_id(config, tmp_path, event):
    store, _ = history_store(tmp_path)
    prior = recorded(config, store, event=event, message_id="prior", target="channel-one")
    assert prior.source.ref_idx is None
    assert store.quoted_sent_at(owner_key(prior), prior.route.scene, prior.route.target, "REFIDX_prior") is None
    assert store.quoted_sent_at(owner_key(prior), prior.route.scene, prior.route.target, "prior") == int(NOW)
    chat = quoted_chat(config, event=event, ref="prior", target="channel-one")
    apply_reply_time(store, chat)
    assert reply_of(chat).time == int(prior.source.sent_at)
    assert reply_of(chat).id == "prior"


def test_reply_time_disabled_or_closed_history_stays_none(config, tmp_path):
    store, _ = history_store(tmp_path)
    prior = recorded(config, store, message_id="prior")
    store.save_policy(owner_key(prior), {"enabled": False, "mode": "unlimited"})
    chat = quoted_chat(config, ref="REFIDX_prior")
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None
    store.save_policy(owner_key(prior), {"enabled": True, "mode": "unlimited"})
    store.close()
    later = quoted_chat(config, ref="REFIDX_prior")
    apply_reply_time(store, later)
    assert reply_of(later).time is None
    assert resolve_reply_time(None, later) is None


def test_self_quote_does_not_hit_current_message_before_record(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = quoted_chat(config, message_id="self", ref="REFIDX_self")
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None
    assert store.record(chat) is True
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None
    again = quoted_chat(config, message_id="self-again", ref="REFIDX_self")
    apply_reply_time(store, again)
    assert reply_of(again).time == int(chat.source.sent_at)


def test_reply_time_lookup_error_does_not_raise(config, tmp_path):
    store, _ = history_store(tmp_path)
    recorded(config, store, message_id="prior")
    chat = quoted_chat(config, ref="REFIDX_prior")

    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    store.quoted_sent_at = boom
    apply_reply_time(store, chat)
    assert reply_of(chat).time is None
    assert store.last_error == "history_storage_unavailable"


def test_inbound_paths_apply_reply_time_before_record():
    from pathlib import Path
    root = Path(__file__).parents[1]
    for rel in ("v2/messaging/delivery.py", "v2/sdk/ingress.py"):
        text = (root / rel).read_text(encoding="utf-8")
        apply_at = text.index("apply_reply_time(")
        record_at = text.index("record_chat(")
        assert apply_at < record_at


@pytest.mark.asyncio
async def test_delivery_fills_reply_time_from_history(receiver):
    owner, instance = receiver
    accept(owner, instance, message_id="prior", text="old")
    assert instance.consumer.step()
    instance._event_queue.get_nowait().cleanup_temporary_local_files()
    payload = chat_payload(message_id="quote-one", text="new", timestamp=NOW + 10)
    payload["d"]["message_type"] = 103
    payload["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_prior")
    payload["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_prior", "content": "old"}]
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    reply = next(part for part in event.get_messages() if isinstance(part, Reply))
    assert reply.id == "REFIDX_prior"
    assert reply.time == int(NOW)
    assert reply.message_str == "old"
    event.cleanup_temporary_local_files()


@pytest.mark.asyncio
async def test_delivery_missing_and_self_quote_keep_reply_time_none(receiver):
    owner, instance = receiver
    missing = chat_payload(message_id="quote-missing", text="new")
    missing["d"]["message_type"] = 103
    missing["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_absent")
    missing["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_absent", "content": "gone"}]
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(missing, NOW))
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert next(part for part in event.get_messages() if isinstance(part, Reply)).time is None
    event.cleanup_temporary_local_files()
    self_quote = chat_payload(message_id="self", text="loop")
    self_quote["d"]["message_type"] = 103
    self_quote["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_self")
    self_quote["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_self", "content": "loop"}]
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(self_quote, NOW))
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert next(part for part in event.get_messages() if isinstance(part, Reply)).time is None
    event.cleanup_temporary_local_files()


@pytest.mark.asyncio
async def test_ingress_then_delivery_fills_prior_and_ignores_self_quote(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    gen = instance.identity.generation
    prior = chat_payload(message_id="prior", text="old")
    owner.inbox.accept(key, RawEnvelope(prior, NOW, "websocket", (0, 1), "session", gen))
    assert instance.core.step() and instance.consumer.step()
    instance._event_queue.get_nowait().cleanup_temporary_local_files()
    quoted = chat_payload(message_id="quote-one", text="new", timestamp=NOW + 10)
    quoted["d"]["message_type"] = 103
    quoted["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_prior")
    quoted["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_prior", "content": "old"}]
    owner.inbox.accept(key, RawEnvelope(quoted, NOW, "websocket", (0, 1), "session", gen))
    assert instance.core.step() and instance.consumer.step()
    event = instance._event_queue.get_nowait()
    reply = next(part for part in event.get_messages() if isinstance(part, Reply))
    assert reply.id == "REFIDX_prior" and reply.time == int(NOW)
    event.cleanup_temporary_local_files()
    self_quote = chat_payload(message_id="self", text="loop")
    self_quote["d"]["message_type"] = 103
    self_quote["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_self")
    self_quote["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_self", "content": "loop"}]
    owner.inbox.accept(key, RawEnvelope(self_quote, NOW, "websocket", (0, 1), "session", gen))
    assert instance.core.step() and instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert next(part for part in event.get_messages() if isinstance(part, Reply)).time is None
    event.cleanup_temporary_local_files()
