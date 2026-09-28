"""SDK core progress is distinct from host delivery and historical observation."""

import asyncio
import sqlite3

import pytest
from test_messaging_state import NOW, chat_payload

from v2.protocol import RawEnvelope
from v2.transport.inbox import RawInbox

pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


async def test_member_state_and_observer_progress_while_host_queue_full(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    seen = []
    async def observe(event):
        seen.append(event)
    sub = instance.client.qq.events.subscribe({"GROUP_MEMBER_REMOVE", "GROUP_MEMBER_ADD"}, callback=observe, owner=object())
    for _ in range(owner.delivery_slots.queue_limit):
        instance._event_queue.put_nowait(None)
    chat = chat_payload()
    owner.inbox.accept(key, RawEnvelope(chat, NOW, "websocket", (0, 1), "session", instance.identity.generation))
    assert instance.core.step()
    chat_receipt = owner.inbox.pending(key, core_ready=True)[0]["receipt"]
    assert instance.client.qq.events.progress(chat_receipt)["host_state"] == "pending"
    assert not instance.consumer.step()
    for index, name in enumerate(("GROUP_MEMBER_REMOVE", "GROUP_MEMBER_ADD"), start=1):
        payload = {"op": 0, "id": f"membership-{index}", "t": name, "s": 10 + index,
                   "d": {"group_openid": "group-one", "member_openid": "user-one", "timestamp": int(NOW) + index}}
        if index == 2:
            payload["d"]["user_openid"] = "external-001"
        owner.inbox.accept(key, RawEnvelope(payload, NOW + index, "websocket", (0, 1), "session", instance.identity.generation))
        assert instance.core.step()
    await asyncio.sleep(0)
    assert len(seen) == 2 and seen[0].context.shard == (0, 1)
    assert owner.profiles.get_member(instance.identity.robot, "group", "group-one", "user-one")["membership"] == "present"
    assert owner.profiles.get_member(instance.identity.robot, "group", "group-one", "user-one")["fields"]["user_openid"]["value"] == "external-001"
    assert owner.inbox.count(key) == 1  # Only the chat waits for host queue admission.
    assert instance.client.qq.events.progress(seen[-1].context.receipt)["host_state"] == "not_applicable"
    for _ in range(owner.delivery_slots.queue_limit):
        instance._event_queue.get_nowait()
    assert instance.consumer.step()
    assert instance.client.qq.events.progress(chat_receipt)["host_state"] == "delivered_to_host"
    host = instance._event_queue.get_nowait()
    host.cleanup_temporary_local_files()
    await sub.close()


async def test_recovered_core_reentry_does_not_renew_source_or_publish_by_default(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    old = chat_payload()
    receipt = owner.inbox.accept(key, RawEnvelope(old, NOW))
    assert receipt
    # A newly constructed consumer treats existing raw receipts as recovered.
    from v2.sdk.ingress import CoreConsumer
    core = CoreConsumer(instance)
    observed = instance.client.qq.events.subscribe({"GROUP_AT_MESSAGE_CREATE"}, owner=object())
    assert core.step()
    assert observed.queue.empty()
    assert owner.inbox.core_pending(key) == []
    assert owner.inbox.pending(key, core_ready=True)[0]["payload"] == old
    await core.close()
    await observed.close()

async def test_ready_precedes_restored_chat_core_conversion(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    owner.inbox.accept(key, RawEnvelope({"op": 0, "t": "READY", "id": "boot", "d": {
        "session_id": "session", "user": {"id": "real-bot"}}}, NOW))
    chat = chat_payload(message_id="after-ready")
    owner.inbox.accept(key, RawEnvelope(chat, NOW))
    from v2.sdk.ingress import CoreConsumer
    recovered = CoreConsumer(instance)
    assert recovered.step() and instance.bot_id == "real-bot"
    assert recovered.step()
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert event.message_obj.self_id == "real-bot"
    event.cleanup_temporary_local_files()
    await recovered.close()

async def test_completed_ready_recovery_restores_bot_id_before_pending_chat(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    ready = {"op": 0, "t": "READY", "id": "ready-crash", "d": {
        "session_id": "session", "user": {"id": "bot-after-restart"}}}
    owner.inbox.accept(key, RawEnvelope(ready, NOW))
    receipt = owner.inbox.pending(key)[0]["receipt"]
    owner.inbox.core_done(key, receipt)  # Simulate crash between core marker and inbox ACK.
    owner.inbox.accept(key, RawEnvelope(chat_payload(message_id="crash-gap"), NOW))
    assert instance.bot_id == ""
    assert instance.core.step() and instance.bot_id == "bot-after-restart"
    assert instance.core.step()
    assert instance.consumer.step()
    host = instance._event_queue.get_nowait()
    assert host.message_obj.self_id == "bot-after-restart"
    host.cleanup_temporary_local_files()
    assert not owner.inbox.pending(key)


async def test_completed_membership_ack_recovery_does_not_reapply_event(receiver, monkeypatch):
    owner, instance = receiver
    key = instance.identity.settings_key
    notice = {"op": 0, "t": "GROUP_MEMBER_ADD", "id": "member-crash", "s": 5,
              "d": {"group_openid": "g", "member_openid": "u", "timestamp": int(NOW)}}
    owner.inbox.accept(key, RawEnvelope(notice, NOW))
    receipt = owner.inbox.pending(key)[0]["receipt"]
    original_ack = owner.inbox.acknowledge
    def failed_ack(*args, **kwargs):
        raise sqlite3.OperationalError("transient fixture")
    monkeypatch.setattr(owner.inbox, "acknowledge", failed_ack)
    with pytest.raises(sqlite3.Error):
        instance.core.step()
    assert owner.inbox.db.execute("SELECT core_state FROM inbox WHERE row_id=?", (receipt,)).fetchone()[0] == "done"
    revision = owner.profiles.revision(instance.identity.robot, "group", "g")
    assert revision == 1
    monkeypatch.setattr(owner.inbox, "acknowledge", original_ack)
    assert instance.core.step()
    assert owner.profiles.revision(instance.identity.robot, "group", "g") == revision
    assert owner.inbox.count(key) == 0 and not instance.core.published


async def test_ready_profile_storage_failure_keeps_connection_fact_and_chat(receiver, monkeypatch):
    owner, instance = receiver
    key = instance.identity.settings_key
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("profile fixture")
    monkeypatch.setattr(owner.profiles, "mark_gap", fail)
    owner.inbox.accept(key, RawEnvelope({"op": 0, "t": "READY", "id": "ready-profile-fail", "d": {
        "session_id": "session", "user": {"id": "bot"}}}, NOW))
    owner.inbox.accept(key, RawEnvelope(chat_payload(message_id="after-profile-fail"), NOW))
    assert instance.core.step() and instance.bot_id == "bot"
    assert owner.profiles.last_error == "profile_storage_unavailable"
    assert instance.core.step() and instance.consumer.step()
    host = instance._event_queue.get_nowait()
    assert host.message_obj.self_id == "bot"
    host.cleanup_temporary_local_files()



async def test_live_interaction_preempts_completed_notice_cleanup(receiver, monkeypatch):
    owner, instance = receiver
    key = instance.identity.settings_key
    owner.inbox.accept(key, RawEnvelope({"op": 0, "id": "old-resumed", "t": "RESUMED", "d": ""}, NOW))
    completed = owner.inbox.pending(key)[0]["receipt"]
    owner.inbox.core_done(key, completed)
    priority = {"op": 0, "id": "priority", "t": "INTERACTION_CREATE", "d": {}}
    owner.inbox.accept(key, RawEnvelope(priority, NOW))
    calls = []
    monkeypatch.setattr(instance.extensions, "accept", lambda payload, received: calls.append(payload["t"]) or True)
    assert instance.core.step() and calls == ["INTERACTION_CREATE"]
    assert owner.inbox.completed_nonchat(key)["receipt"] == completed
    assert instance.core.step() and owner.inbox.count(key) == 0


async def test_profile_capacity_degrades_core_without_blocking_host_chat(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    robot = instance.identity.robot
    owner.profiles.set_continuity(robot, "group", "group-one", True)
    assert owner.profiles.record_roster(robot, "group", "group-one", [], started_revision=0, started_at=NOW)
    assert owner.profiles.roster_status(robot, "group", "group-one")["continuous"]
    owner.profiles.max_profiles = 0
    owner.inbox.accept(key, RawEnvelope(chat_payload(message_id="cache-full"), NOW))
    assert instance.core.step()
    row = owner.inbox.db.execute("SELECT core_state,core_error FROM inbox WHERE owner=? AND body IS NOT NULL", (key,)).fetchone()
    assert tuple(row) == ("degraded", "cache_capacity")
    assert owner.profiles.roster_status(robot, "group", "group-one")["continuous"] is False
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert event.message_obj.message_id == "cache-full"
    event.cleanup_temporary_local_files()
    assert owner.messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1


async def test_nested_history_only_fills_profile_without_reviving_member(receiver):
    owner, instance = receiver
    robot = instance.identity.robot
    owner.profiles.member_event(robot, "group", "group-one", "former", "left", int(NOW) - 10)
    payload = chat_payload(message_id="history")
    payload["d"]["message_type"] = 103
    payload["d"]["message_scene"]["ext"].append("ref_msg_idx=ref-history")
    payload["d"]["msg_elements"] = [{"author": {"member_openid": "former", "username": "Old Nick"},
                                   "timestamp": "2020-01-01T00:00:00Z", "msg_idx": "ref-history", "content": "old body"}]
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    assert instance.core.step()
    member = owner.profiles.get_member(robot, "group", "group-one", "former")
    assert member["membership"] == "left"
    assert member["fields"]["nickname"]["value"] == "Old Nick"
    assert {row[0] for row in owner.messages.db.execute("SELECT message_id FROM sources WHERE scene='group' AND target='group-one'")} == {"history"}

async def test_forwarded_and_parallel_authors_do_not_enter_group_profiles(receiver):
    owner, instance = receiver
    payload = chat_payload(message_id="untrusted-history")
    payload["d"]["message_type"] = 103
    payload["d"]["message_scene"]["ext"].append("ref_msg_idx=ref-bound")
    payload["d"]["msg_elements"] = [
        {"message_type": 102, "msg_idx": "ref-bound", "content": "forward",
         "author": {"member_openid": "foreign", "username": "Other group"},
         "msg_elements": [{"author": {"member_openid": "nested", "username": "Nested foreign"}}]},
        {"message_type": 103, "msg_idx": "other", "content": "parallel",
         "author": {"member_openid": "parallel", "username": "Unrelated"}},
        {"message_type": 103, "msg_idx": "ref-bound", "content": "quoted",
         "timestamp": "2020-01-01T00:00:00Z",
         "author": {"member_openid": "trusted", "username": "Trusted quote"}},
    ]
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    assert instance.core.step()
    known = owner.profiles.list_known_members(instance.identity.robot, "group", "group-one")["members"]
    assert {row["user_id"] for row in known} == {"user-one", "trusted"}
    quoted = owner.profiles.get_member(instance.identity.robot, "group", "group-one", "trusted")
    assert quoted["fields"]["nickname"]["source"] == "chat_history"
    assert quoted["membership"] == "unknown"


async def test_no_listeners_skip_native_event_construction(receiver, monkeypatch):
    import sys
    owner, instance = receiver
    module = sys.modules[instance.core.__class__.__module__]
    def fail(*args, **kwargs):
        raise AssertionError("No SDK event allocation without a subscriber")
    monkeypatch.setattr(module, "NativeEvent", fail)
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(chat_payload(message_id="no-subscriber"), NOW))
    assert instance.core.step() and instance.consumer.step()
    host = instance._event_queue.get_nowait()
    host.cleanup_temporary_local_files()


async def test_degraded_event_context_is_pending_snapshot(receiver):
    owner, instance = receiver
    owner.profiles.max_profiles = 0
    sub = instance.client.qq.events.subscribe({"GROUP_AT_MESSAGE_CREATE"}, owner=object())
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(chat_payload(message_id="degraded-context"), NOW))
    try:
        assert instance.core.step()
        event = sub.queue.get_nowait()[0]
        assert event.context.core_state == "pending"
        assert instance.client.qq.events.progress(event.context.receipt)["core_state"] == "degraded"
    finally:
        await sub.close()




async def test_invalid_known_dispatch_is_retained_and_raw_observers_see_diagnostic(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    raw = instance.client.qq.events.subscribe({"*"}, owner=object())
    typed = instance.client.qq.events.subscribe({"GROUP_MEMBER_ADD"}, owner=object())
    payload = {"op": 0, "id": "invalid-member", "t": "GROUP_MEMBER_ADD", "d": []}
    owner.inbox.accept(key, RawEnvelope(payload, NOW))
    assert instance.core.step()
    assert owner.inbox.retained(key)[0]["reason"] == "invalid_event_data"
    assert typed.queue.empty() and raw.queue.qsize() == 1
    stream = raw.__aiter__()
    event = await anext(stream)
    await stream.aclose()
    assert event.diagnostic == "invalid_event_data" and not event.known
    await raw.close()
    await typed.close()


async def test_known_but_incomplete_shape_is_raw_only_without_inventing_a_guild(receiver):
    owner, instance = receiver
    raw = instance.client.qq.events.subscribe({"*"}, owner=object())
    typed = instance.client.qq.events.subscribe({"GUILD_CREATE"}, owner=object())
    payload = {"op": 0, "id": "guild-incomplete", "t": "GUILD_CREATE", "d": {"name": "unscoped"}}
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    try:
        assert instance.core.step()
        assert typed.queue.empty()
        event = raw.queue.get_nowait()[0]
        assert event.known and event.d["name"] == "unscoped"
        assert event.schema_missing == ("id",) and event.typed is None
        assert instance.client.qq.events.progress(event.context.receipt)["core_state"] == "done"
    finally:
        await raw.close()
        await typed.close()

async def test_oversized_sdk_structure_is_quarantined_without_poisoning_chat(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    nested = {}
    for _ in range(35):
        nested = {"child": nested}
    owner.inbox.accept(key, RawEnvelope({"op": 0, "id": "too-deep", "t": "FUTURE_EVENT", "d": nested}, NOW))
    raw = instance.client.qq.events.subscribe({"*"}, owner=object())
    owner.inbox.accept(key, RawEnvelope(chat_payload(message_id="valid-after-invalid"), NOW))
    assert instance.core.step() and owner.inbox.retained(key)[0]["reason"] == "invalid_sdk_event"
    observer = raw.__aiter__()
    diagnostic = await anext(observer)
    assert diagnostic.diagnostic == "invalid_sdk_event" and diagnostic.d["receipt"] > 0
    await observer.aclose()
    assert instance.core.step() and instance.consumer.step()
    host = instance._event_queue.get_nowait()
    assert host.message_obj.message_id == "valid-after-invalid"
    host.cleanup_temporary_local_files()


async def test_call_view_is_scoped_without_mutating_shared_client(receiver, monkeypatch):
    owner, instance = receiver
    calls = []
    async def fake_send(route, message, **options):
        calls.append((route, message, options))
        return {"state": "fixture"}
    monkeypatch.setattr(instance.client, "send", fake_send)
    view = instance.client.qq.with_options(operation_id="one", owner=object())
    await view.send("group", "g", "hello")
    assert calls[0][2]["operation_id"] == "one"
    assert instance.client.qq._options.operation_id is None
    with pytest.raises(RuntimeError) as exc:
        await view.send("group", "g", "different", operation_id="two")
    assert exc.value.code == "operation_conflict" and len(calls) == 1


async def test_plugin_owner_unload_and_platform_revoke_close_subscriptions(receiver):
    from types import SimpleNamespace
    owner, instance = receiver
    plugin = object()
    ours = instance.client.qq.events.subscribe({"GROUP_MEMBER_ADD"}, owner=plugin)
    other = instance.client.qq.events.subscribe({"GROUP_MEMBER_ADD"}, owner=object())
    owner_view = instance.client.qq.with_options(owner=plugin)
    await owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
    assert ours.closed and not other.closed
    with pytest.raises(RuntimeError) as stale:
        await owner_view.post_group_message("g", content="never")
    assert stale.value.code == "stale_owner"
    instance.revoke()
    assert other.closed and instance.client._state.events.closed
    await ours.close()
    await other.close()


def test_schema3_raw_inbox_migrates_context_without_erasing_pending(tmp_path):
    path = tmp_path / "transport.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE inbox (row_id INTEGER PRIMARY KEY,owner TEXT NOT NULL,event_id TEXT,body TEXT,received REAL NOT NULL,delivered REAL,size INTEGER NOT NULL DEFAULT 0,disposition TEXT NOT NULL DEFAULT 'pending',reason TEXT,UNIQUE(owner,event_id))")
    db.execute("INSERT INTO inbox(owner,event_id,body,received,size) VALUES(?,?,?,?,?)", ("owner", "old", '{"op":0,"t":"RESUMED","d":""}', 1, 29))
    db.execute("PRAGMA user_version=3")
    db.commit()
    db.close()
    inbox = RawInbox(path)
    try:
        assert inbox.db.execute("PRAGMA user_version").fetchone()[0] == 4
        pending = inbox.core_pending("owner")
        assert pending[0]["payload"]["d"] == "" and pending[0]["transport"] is None
        inbox.core_done("owner", pending[0]["receipt"])
        assert inbox.pending("owner", core_ready=True) == []
        assert inbox.completed_nonchat("owner")["payload"]["t"] == "RESUMED"
    finally:
        inbox.close()
