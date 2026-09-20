import json
import sqlite3

import pytest
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW, chat_payload

from v2.errors import V2Error
from v2.protocol import RawEnvelope
from v2.transport.inbox import RawInbox


async def test_connection_notices_do_not_exhaust_ingress(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    for n in range(300):
        event = "READY" if n % 2 == 0 else "RESUMED"
        data = {"session_id": "fixture-session", "user": {"id": "real-bot"}} if event == "READY" else {}
        assert owner.inbox.accept(key, RawEnvelope({"op": 0, "t": event, "d": data, "id": f"connection-{n}"}, NOW))
        assert instance.consumer.step()
    assert owner.inbox.count(key) == 0
    assert instance.bot_id == "real-bot" and instance._event_queue.empty()
    assert owner.inbox.accept(key, RawEnvelope(chat_payload(), NOW))
    assert instance.consumer.step() and instance._event_queue.qsize() == 1
    instance._event_queue.get_nowait().cleanup_temporary_local_files()


@pytest.fixture
def inbox(tmp_path):
    value = RawInbox(tmp_path / "raw.db", clock=lambda: NOW)
    yield value
    value.close()


def retain(inbox, owner, number, invalid=False):
    payload = {"op": 0, "t": "CUSTOM_EVENT", "id": f"event-{number}", "d": {"secret": "private-body-marker"}}
    assert inbox.accept(owner, RawEnvelope(payload, NOW))
    receipt = next(row["receipt"] for row in inbox.pending(owner) if row["payload"] == payload)
    inbox.retain(owner, receipt, "fixture_reason", invalid=invalid)
    return receipt


def test_retained_discard_recovers_full_scope_without_dropping_pending(inbox):
    for n in range(255):
        retain(inbox, "owner", n, invalid=n % 2 == 0)
    assert inbox.accept("owner", RawEnvelope(chat_payload(), NOW))
    with pytest.raises(V2Error) as exc:
        inbox.accept("owner", RawEnvelope(chat_payload(message_id="later"), NOW))
    assert exc.value.code == "inbox_full"
    rows = inbox.retained_summary("owner")
    assert len(rows) == 255 and "private-body-marker" not in json.dumps(rows)
    assert {row["state"] for row in rows} == {"invalid", "extension"}
    selected = [{key: row[key] for key in ("receipt", "version")} for row in rows]
    assert inbox.discard_retained("owner", selected) == 255
    assert inbox.count("owner") == 1 and inbox.pending("owner")[0]["payload"] == chat_payload()
    assert inbox.accept("owner", RawEnvelope(chat_payload(message_id="later"), NOW))
    assert inbox.db.execute("SELECT count(*) FROM inbox WHERE disposition='discarded' AND reason='operator_discard' AND body IS NULL").fetchone()[0] == 255


def test_retained_discard_rejects_cross_owner_stale_pending_and_duplicates(inbox):
    receipt = retain(inbox, "one", 1)
    row = inbox.retained_summary("one")[0]
    chosen = [{"receipt": receipt, "version": row["version"]}]
    for owner, selected in [("two", chosen), ("one", chosen * 2), ("one", [{"receipt": True, "version": row["version"]}])]:
        with pytest.raises(V2Error):
            inbox.discard_retained(owner, selected)
    inbox.retain("one", receipt, "changed_reason")
    with pytest.raises(V2Error) as exc:
        inbox.discard_retained("one", chosen)
    assert exc.value.code == "inbox_snapshot_changed" and inbox.count("one") == 1
    inbox.db.execute("UPDATE inbox SET disposition='pending' WHERE row_id=?", (receipt,))
    inbox.db.commit()
    with pytest.raises(V2Error):
        inbox.discard_retained("one", chosen)
    assert inbox.pending("one")


def test_retained_discard_is_atomic_and_old_receipts_cannot_repeat(inbox):
    for n in range(2):
        retain(inbox, "owner", n)
    selected = [{k: row[k] for k in ("receipt", "version")} for row in inbox.retained_summary("owner")]
    inbox.db.execute("CREATE TRIGGER fail_discard BEFORE UPDATE OF body ON inbox WHEN OLD.row_id=2 BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
    with pytest.raises(sqlite3.Error):
        inbox.discard_retained("owner", selected)
    assert inbox.count("owner") == 2
    inbox.db.execute("DROP TRIGGER fail_discard")
    assert inbox.discard_retained("owner", selected) == 2
    with pytest.raises(V2Error):
        inbox.discard_retained("owner", selected)


async def test_bad_ready_and_unhandled_dispatch_remain_operator_visible(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    for number, event in enumerate([{"op": 0, "t": "READY", "d": {}}, {"op": 7, "t": "READY", "d": {}}, {"op": 0, "t": "INTERACTION_CREATE", "d": {}}]):
        owner.inbox.accept(key, RawEnvelope({**event, "id": f"bad-{number}"}, NOW))
        assert instance.consumer.step()
    assert owner.inbox.count(key) == 3 and instance._event_queue.empty()
    assert len(owner.inbox.retained_summary(key)) == 3
