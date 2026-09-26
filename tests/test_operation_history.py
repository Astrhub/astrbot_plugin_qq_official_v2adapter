import json
import sqlite3

import pytest
from test_messaging_send import sending as sending
from test_messaging_state import NOW, observed

from v2.errors import V2Error
from v2.messaging.store import MessageStore, robot_key


@pytest.fixture
def ledger(tmp_path):
    clock = [NOW]
    store = MessageStore(tmp_path / "operations.db", clock=lambda: clock[0], operation_capacity=2)
    yield store, clock
    store.close()


def finish_send(store, chat, op_id, outcome="sent"):
    store.reserve(chat.route, None, "digest", op_id)
    if outcome != "not_sent":
        store.mark_in_flight(chat.route.robot, op_id)
    store.finish(chat.route.robot, op_id, outcome,
                 result={"message_id": "result-" + op_id} if outcome == "sent" else None)


@pytest.mark.parametrize("outcome", ["sent", "rejected", "not_sent"])
def test_terminal_history_does_not_fill_pending_budget(ledger, config, outcome):
    store, clock = ledger
    chat = observed(config)
    store.observe(chat)
    for index in range(12):
        finish_send(store, chat, f"op-{index}", outcome)
        clock[0] += 61
    assert store.db.execute("SELECT count(*) FROM operations WHERE state IN ('sent','rejected','not_sent')").fetchone()[0] == 2
    assert store.operation(chat.route.robot, "op-11")["state"] == outcome
    assert store.db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
    with pytest.raises(V2Error) as exc:
        store.operation(chat.route.robot, "op-0")
    assert exc.value.code == ("operation_history_evicted" if outcome == "sent" else "operation_not_found")


async def test_offline_failures_release_capacity_for_recovery(sending):
    s = sending
    s.store.operation_capacity = 2
    chat, _ = s.observe()
    s.core.is_online = lambda: False
    for index in range(8):
        with pytest.raises(V2Error) as exc:
            await s.core.send(chat.route, "offline", operation_id=f"offline-{index}")
        assert exc.value.code == "transport_not_ready" and exc.value.phase == "not_sent"
    assert not s.calls and not s.core.storage_failed
    assert s.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 2
    s.core.is_online = lambda: True
    result = await s.core.send(chat.route, "recovered", operation_id="recovered")
    assert result["state"] == "sent" and len(s.calls) == 1


async def test_compacted_success_cannot_replay_or_change_binding(sending):
    s = sending
    s.store.operation_capacity = 1
    chat, _ = s.observe()
    await s.core.send(chat.route, "once", source=chat.source, operation_id="original")
    await s.core.send(chat.route, "second", source=chat.source, operation_id="new")
    for content, code in [("once", "operation_history_evicted"), ("changed", "operation_conflict")]:
        with pytest.raises(V2Error) as exc:
            await s.core.send(chat.route, content, source=chat.source, operation_id="original")
        assert exc.value.code == code
    s.clock[0] += 61
    s.store.prune()
    with pytest.raises(V2Error) as exc:
        await s.core.send(chat.route, "once", source=chat.source, operation_id="original")
    assert exc.value.code == "operation_history_evicted"
    assert len(s.calls) == 2 and not s.core.storage_failed
    assert tuple(s.store.db.execute("SELECT seq,used FROM sources").fetchone()) == (2, 2)


def test_history_compaction_keeps_replay_fences_without_local_quota(ledger, config):
    store, clock = ledger
    chat = observed(config)
    other = observed({**config, "appid": "second-app"})
    store.observe(chat)
    store.observe(other)
    for index in range(20):
        finish_send(store, chat, f"op-{index}")
        clock[0] += 2
    assert store.db.execute("SELECT count(*) FROM charges").fetchone()[0] == 0
    robot = robot_key(chat.route.robot)
    with store.transaction():
        store.db.executemany("INSERT INTO charges VALUES(?,?,?,?,?)",
            ((robot, f"op-{index}", "active_target", "group:group-one", clock[0] + 60) for index in range(20)))
    assert store.db.execute("SELECT count(*) FROM charges").fetchone()[0] == 20
    finish_send(store, chat, "over-quota")
    finish_send(store, other, "op-0")
    assert store.operation(other.route.robot, "op-0")["state"] == "sent"
    with pytest.raises(V2Error) as exc:
        store.reserve(chat.route, None, "digest", "op-0")
    assert exc.value.code == "operation_history_evicted"


def test_nonterminal_budget_and_restart_keep_unknown_and_compacted_fences(ledger, config, tmp_path):
    store, clock = ledger
    chat = observed(config)
    store.observe(chat)
    for index in range(5):
        finish_send(store, chat, f"done-{index}")
        clock[0] += 61
    store.reserve(chat.route, None, "digest", "unknown")
    store.mark_in_flight(chat.route.robot, "unknown")
    store.finish(chat.route.robot, "unknown", "unknown")
    store.reserve(chat.route, None, "digest", "reserved")
    with pytest.raises(V2Error) as exc:
        store.reserve(chat.route, None, "digest", "overflow")
    assert exc.value.code == "message_state_full"
    store.close()
    restored = MessageStore(tmp_path / "operations.db", clock=lambda: clock[0], operation_capacity=2)
    try:
        assert restored.operation(chat.route.robot, "reserved")["state"] == "not_sent"
        assert restored.operation(chat.route.robot, "unknown")["state"] == "unknown"
        with pytest.raises(V2Error) as exc:
            restored.reserve(chat.route, None, "digest", "done-0")
        assert exc.value.code == "operation_history_evicted"
        clock[0] += 86401
        restored.prune()
        assert restored.operation(chat.route.robot, "unknown")["state"] == "unknown"
        assert restored.db.execute("SELECT count(*) FROM operations WHERE state='history_evicted'").fetchone()[0] == 0
    finally:
        restored.close()


def test_finishing_transaction_rolls_back_compaction_on_failure(ledger, config):
    store, _ = ledger
    chat = observed(config)
    store.observe(chat)
    finish_send(store, chat, "first")
    finish_send(store, chat, "second")
    store.reserve(chat.route, None, "digest", "third")
    before = [tuple(row) for row in store.db.execute("SELECT * FROM operations ORDER BY rowid")]
    store.db.execute("CREATE TRIGGER fail_finish BEFORE UPDATE ON operations WHEN NEW.op_id='third' AND NEW.state='sent' BEGIN SELECT RAISE(ABORT, 'fixture finish failure'); END")
    with pytest.raises(sqlite3.Error):
        store.finish(chat.route.robot, "third", "sent", result={"message_id": "third-result"})
    assert [tuple(row) for row in store.db.execute("SELECT * FROM operations ORDER BY rowid")] == before


@pytest.mark.parametrize("outcome", ["sent", "not_sent"])
def test_default_32768_result_limit_does_not_reject_next_send(ledger, config, outcome):
    store, _ = ledger
    store.operation_capacity = 32768
    chat = observed(config)
    store.observe(chat)
    robot = robot_key(chat.route.robot)
    with store.transaction():
        store.db.executemany("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ((robot, f"old-{i}", "group", chat.route.target, None, "old-digest", None, outcome, NOW - 600, NOW - 600,
              json.dumps({"message_id": f"historical-{i}"}) if outcome == "sent" else None, None) for i in range(32768)))
    finish_send(store, chat, "next")
    assert store.operation(chat.route.robot, "next")["state"] == "sent"
    assert store.db.execute("SELECT count(*) FROM operations WHERE state IN ('sent','rejected','not_sent')").fetchone()[0] == 32768
    assert store.db.execute("SELECT count(*) FROM operations WHERE state='history_evicted'").fetchone()[0] == (1 if outcome == "sent" else 0)
