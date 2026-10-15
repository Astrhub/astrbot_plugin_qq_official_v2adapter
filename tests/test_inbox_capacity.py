"""Live raw events and bounded delivered deduplication records have separate budgets."""
import sqlite3

import pytest
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW, chat_payload

from v2.errors import V2Error
from v2.protocol import RawEnvelope
from v2.transport.inbox import RawInbox


def envelope(event_id):
    return RawEnvelope({"op": 0, "id": event_id, "t": "FUTURE_EVENT", "d": {"text": "fixture"}}, NOW)


def receipt(inbox, owner, event_id):
    return inbox.db.execute("SELECT row_id FROM inbox WHERE owner=? AND event_id=?", (owner, event_id)).fetchone()[0]


def accept_and_ack(inbox, owner, event_id):
    assert inbox.accept(owner, envelope(event_id))
    inbox.acknowledge(owner, receipt(inbox, owner, event_id))


async def test_consumed_stream_exceeds_live_capacity_without_backpressure(receiver):
    owner, instance = receiver
    inbox = owner.inbox
    inbox.clock = lambda: NOW
    key = instance.identity.settings_key
    for number in range(1030):
        payload = chat_payload(message_id=f"throughput-{number}")
        assert inbox.accept(key, RawEnvelope(payload, NOW))
        assert instance.consumer.step()
        event = instance._event_queue.get_nowait()
        event.cleanup_temporary_local_files()
        assert inbox.count(key) == 0
    assert inbox.db.execute("SELECT count(*) FROM inbox WHERE body IS NULL").fetchone()[0] == 1030
    assert not inbox.accept(key, RawEnvelope(chat_payload(message_id="throughput-0"), NOW))
    assert not owner.delivery_slots.events


def test_tombstone_eviction_never_removes_pending_or_retained_payloads(tmp_path):
    inbox = RawInbox(tmp_path / "raw.sqlite3", max_rows=4, max_tombstones=3, clock=lambda: NOW)
    try:
        for name in ("pending", "invalid", "extension"):
            assert inbox.accept("live", envelope(name))
            if name != "pending":
                inbox.retain("live", receipt(inbox, "live", name), "fixture", invalid=name == "invalid")
        for number in range(10):
            accept_and_ack(inbox, "history", f"done-{number}")
        assert inbox.diagnostics("live") == {"pending": 1, "invalid": 1, "extension": 1}
        assert inbox.pending("live")[0]["payload"] == envelope("pending").payload
        assert {r["payload"]["id"] for r in inbox.retained("live")} == {"invalid", "extension"}
        tombstones = inbox.db.execute("SELECT event_id FROM inbox WHERE body IS NULL ORDER BY row_id").fetchall()
        assert [row[0] for row in tombstones] == ["done-7", "done-8", "done-9"]
        assert not inbox.accept("history", envelope("done-9"))
        assert inbox.accept("history", envelope("done-0"))  # An evicted key is no longer deduplicated here.
        with pytest.raises(V2Error) as exc:
            inbox.accept("another", envelope("full"))
        assert exc.value.code == "inbox_full" and inbox.count("live") == 3
    finally:
        inbox.close()


def test_delivered_ttl_and_owner_isolation_survive_restart(tmp_path):
    path = tmp_path / "raw.sqlite3"
    clock = [NOW]
    inbox = RawInbox(path, max_rows=1, max_tombstones=4, clock=lambda: clock[0])
    accept_and_ack(inbox, "first", "same-id")
    inbox.close()
    inbox = RawInbox(path, max_rows=1, max_tombstones=4, clock=lambda: clock[0])
    try:
        assert inbox.accept("second", envelope("same-id"))
        clock[0] += 299
        assert not inbox.accept("first", envelope("same-id"))
        inbox.acknowledge("second", receipt(inbox, "second", "same-id"))
        clock[0] += 1
        assert inbox.accept("first", envelope("same-id"))
        assert inbox.count("first") == 1 and inbox.count("second") == 0
    finally:
        inbox.close()


def test_startup_prunes_only_delivered_overflow(tmp_path):
    path = tmp_path / "raw.sqlite3"
    inbox = RawInbox(path, max_tombstones=8, clock=lambda: NOW)
    assert inbox.accept("live", envelope("kept"))
    inbox.retain("live", receipt(inbox, "live", "kept"), "extension")
    for number in range(6):
        accept_and_ack(inbox, "history", str(number))
    inbox.close()
    inbox = RawInbox(path, max_tombstones=2, clock=lambda: NOW)
    try:
        assert inbox.count("live") == 1 and inbox.retained("live")[0]["payload"] == envelope("kept").payload
        assert inbox.db.execute("SELECT count(*) FROM inbox WHERE body IS NULL").fetchone()[0] == 2
        assert not inbox.accept("history", envelope("5"))
        assert inbox.accept("history", envelope("0"))
    finally:
        inbox.close()


def test_tombstone_prune_failure_rolls_back_acknowledgement(tmp_path):
    inbox = RawInbox(tmp_path / "raw.sqlite3", max_tombstones=1, clock=lambda: NOW)
    try:
        accept_and_ack(inbox, "owner", "old")
        assert inbox.accept("owner", envelope("new"))
        new_receipt = receipt(inbox, "owner", "new")
        inbox.db.execute("CREATE TRIGGER fail_prune BEFORE DELETE ON inbox BEGIN SELECT RAISE(ABORT, 'fixture prune failure'); END")
        inbox.db.commit()
        with pytest.raises(sqlite3.Error):
            inbox.acknowledge("owner", new_receipt)
        assert inbox.pending("owner")[0]["payload"] == envelope("new").payload
        assert inbox.db.execute("SELECT count(*) FROM inbox WHERE body IS NULL").fetchone()[0] == 1
        inbox.db.execute("DROP TRIGGER fail_prune")
        inbox.db.commit()
        inbox.acknowledge("owner", new_receipt)
        assert inbox.count("owner") == 0
        assert [row[0] for row in inbox.db.execute("SELECT event_id FROM inbox")] == ["new"]
    finally:
        inbox.close()


def test_per_owner_limit_still_counts_all_unprocessed_dispositions(tmp_path):
    inbox = RawInbox(tmp_path / "raw.sqlite3", clock=lambda: NOW)
    try:
        for number in range(256):
            assert inbox.accept("busy", envelope(str(number)))
            if number % 2:
                inbox.retain("busy", receipt(inbox, "busy", str(number)), "extension")
        with pytest.raises(V2Error) as exc:
            inbox.accept("busy", envelope("overflow"))
        assert exc.value.code == "inbox_full"
        assert inbox.accept("other", envelope("allowed"))
    finally:
        inbox.close()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_tombstone_capacity_must_be_a_positive_integer(tmp_path, limit):
    with pytest.raises(ValueError):
        RawInbox(tmp_path / "raw.sqlite3", max_tombstones=limit)



pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


def _retain_item(inbox, owner, number, invalid=False):
    payload = {"op": 0, "t": "CUSTOM_EVENT", "id": f"event-{number}", "d": {"secret": "private-body-marker"}}
    assert inbox.accept(owner, RawEnvelope(payload, NOW))
    rec = next(row["receipt"] for row in inbox.pending(owner) if row["payload"] == payload)
    inbox.retain(owner, rec, "fixture_reason", invalid=invalid)
    return rec


async def test_connection_notices_bad_ready_and_retained_discard_recovery(receiver, tmp_path):
    owner, instance = receiver
    key = instance.identity.settings_key
    for n in range(100):
        ev = "READY" if n % 2 == 0 else "RESUMED"
        data = {"session_id": "fixture-session", "user": {"id": "real-bot"}} if ev == "READY" else {}
        assert owner.inbox.accept(key, RawEnvelope({"op": 0, "t": ev, "d": data, "id": f"connection-{n}"}, NOW))
        assert instance.consumer.step()
    assert owner.inbox.count(key) == 0 and instance.bot_id == "real-bot"

    for number, ev_obj in enumerate([{"op": 0, "t": "READY", "d": {}}, {"op": 7, "t": "READY", "d": {}}, {"op": 0, "t": "INTERACTION_CREATE", "d": {}}]):
        owner.inbox.accept(key, RawEnvelope({**ev_obj, "id": f"bad-{number}"}, NOW))
        assert instance.consumer.step()
    assert len(owner.inbox.retained_summary(key)) == 3

    import json
    inbox = RawInbox(tmp_path / "recovery.db", clock=lambda: NOW)
    try:
        for n in range(255):
            _retain_item(inbox, "owner", n, invalid=n % 2 == 0)
        assert inbox.accept("owner", RawEnvelope(chat_payload(), NOW))
        with pytest.raises(V2Error) as exc:
            inbox.accept("owner", RawEnvelope(chat_payload(message_id="later"), NOW))
        assert exc.value.code == "inbox_full"
        rows = inbox.retained_summary("owner")
        assert len(rows) == 255 and "private-body-marker" not in json.dumps(rows)
        selected = [{k: row[k] for k in ("receipt", "version")} for row in rows]
        assert inbox.discard_retained("owner", selected) == 255
        assert inbox.count("owner") == 1

        rec1 = _retain_item(inbox, "one", 1)
        row1 = inbox.retained_summary("one")[0]
        chosen = [{"receipt": rec1, "version": row1["version"]}]
        for bad_owner, sel in [("two", chosen), ("one", chosen * 2), ("one", [{"receipt": True, "version": row1["version"]}])]:
            with pytest.raises(V2Error):
                inbox.discard_retained(bad_owner, sel)
        inbox.retain("one", rec1, "changed_reason")
        with pytest.raises(V2Error) as exc:
            inbox.discard_retained("one", chosen)
        assert exc.value.code == "inbox_snapshot_changed"
    finally:
        inbox.close()
