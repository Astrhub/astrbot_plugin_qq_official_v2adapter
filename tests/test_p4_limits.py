import hashlib
from datetime import UTC, datetime

import pytest
from test_extension_dispatch import dispatch as dispatch
from test_extension_dispatch import settle
from test_interactions import interaction
from test_lifecycle import plugin_module as plugin_module
from test_management import management as management
from test_messaging_delivery import receiver as receiver

from v2.errors import V2Error
from v2.extensions.state import ExtensionStore, digest
from v2.messaging.store import MessageStore, robot_key
from v2.models import RobotKey


@pytest.mark.parametrize("kind", ["stream_frame", "interaction_ack"])
def test_default_extension_history_does_not_block_another_robot_or_ack(tmp_path, kind):
    path = tmp_path / "messages"
    clock = [100.0]
    store = MessageStore(path, clock=lambda: clock[0])
    robot, other = RobotKey("original", "production"), RobotKey("other", "production")
    try:
        state = ExtensionStore(store)
        state.begin(robot, "unknown", "media_put", "original-binding")
        state.attempt(robot, "unknown")
        state.finish(robot, "unknown", "unknown")
        with store.transaction():
            store.db.executemany(
                "INSERT INTO extension_ops VALUES(?,?,?,?,?,?,NULL,NULL,NULL)",
                ((robot_key(robot), f"past-{n}", "media_put", str(n), "history_evicted", clock[0]) for n in range(32768)),
            )
        fresh, _ = state.begin(other, "fresh", kind, "fresh-binding")
        assert fresh
        assert state.operation(robot, "unknown")["state"] == "unknown"
        assert store.db.execute("SELECT count(*) FROM extension_ops WHERE state='history_evicted'").fetchone()[0] == 32768
        with pytest.raises(V2Error) as error:
            state.begin(robot, "past-0", "media_put", "0")
        assert error.value.code == "operation_already_attempted"
        assert store.db.execute("PRAGMA max_page_count").fetchone()[0] * store.db.execute("PRAGMA page_size").fetchone()[0] == 128 * 1024 * 1024
        store.close()
        clock[0] = 50
        store = MessageStore(path, clock=lambda: clock[0])
        state = ExtensionStore(store)
        assert state.operation(robot, "unknown")["state"] == "unknown"
        with pytest.raises(V2Error):
            state.begin(robot, "past-0", "media_put", "0")
        clock[0] = 86501
        state.begin(other, "after-expiry", kind, "new-binding")
        assert store.db.execute("SELECT count(*) FROM extension_ops WHERE state='history_evicted'").fetchone()[0] == 0
        assert state.operation(robot, "unknown")["state"] == "unknown"
    finally:
        store.close()


def test_extension_pending_and_unknown_budgets_remain_independent(tmp_path):
    store = MessageStore(tmp_path / "messages", clock=lambda: 100)
    robot = RobotKey("fixture", "production")
    try:
        state = ExtensionStore(store, capacity=1)
        state.begin(robot, "unknown", "media_put", "binding")
        state.attempt(robot, "unknown")
        state.finish(robot, "unknown", "unknown")
        with pytest.raises(V2Error) as error:
            state.begin(robot, "another", "media_put", "other")
        assert error.value.code == "extension_state_full"
        for n in range(128):
            op = f"ack-{n}"
            assert state.begin(robot, op, "interaction_ack", digest(n))[0]
            state.attempt(robot, op)
            state.finish(robot, op, "unknown")
        with pytest.raises(V2Error) as error:
            state.begin(robot, "ack-overflow", "interaction_ack", "overflow")
        assert error.value.code == "extension_state_full"
        assert store.db.execute("SELECT count(*) FROM extension_ops WHERE state='unknown'").fetchone()[0] == 129
    finally:
        store.close()


def application(now, request="new-request"):
    return {"member_openid": "new-member", "join_request_id": request,
            "apply_at": datetime.fromtimestamp(now, UTC).isoformat(), "apply_source": "self_apply"}


@pytest.mark.parametrize("terminal", ["pending", "succeeded", "retryable", "observed_approved"])
async def test_expired_known_approval_records_release_default_capacity(management, terminal):
    m = management
    robot = robot_key(m.http.identity.robot)
    old = m.clock[0] - 86401
    m.state.begin(m.http.identity.robot, "uncertain-approval", "approve_request", "binding")
    m.state.attempt(m.http.identity.robot, "uncertain-approval")
    m.state.finish(m.http.identity.robot, "uncertain-approval", "unknown")
    with m.store.transaction():
        m.store.db.executemany(
            "INSERT INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)",
            ((robot, "g", f"member-{n}", f"request-{n}", f"hash-{n}", old + 300, old, terminal, None) for n in range(4094)),
        )
        m.store.db.execute("INSERT INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)",
                           (robot, "g", "unknown-member", "unknown-request", "unknown-hash", old + 300, old, "attempted", "uncertain-approval"))
        m.store.db.execute("INSERT INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)",
                           (robot, "g", "live-member", "live-request", "live-hash", m.clock[0] + 300, m.clock[0], "pending", None))
    flag = m.service.observe_request("g", application(m.clock[0]), fresh_read=True)
    assert isinstance(flag, str) and flag
    remaining = {(row[0], row[1]) for row in m.store.db.execute("SELECT member,state FROM join_flags")}
    assert remaining == {("unknown-member", "attempted"), ("live-member", "pending"), ("new-member", "pending")}
    assert m.state.operation(m.http.identity.robot, "uncertain-approval")["state"] == "unknown"
    assert m.store.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 0
    assert not m.calls


async def test_live_or_uncertain_approval_capacity_still_fails_closed(management):
    m = management
    robot, old = robot_key(m.http.identity.robot), m.clock[0] - 86401
    with m.store.transaction():
        m.store.db.executemany(
            "INSERT INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)",
            ((robot, "g", f"member-{n}", f"request-{n}", f"hash-{n}",
              old + 300 if n % 2 else m.clock[0] + 300, old if n % 2 else m.clock[0],
              "attempted" if n % 2 else "pending", f"op-{n}" if n % 2 else None) for n in range(4096)),
        )
    with pytest.raises(V2Error) as error:
        m.service.observe_request("g", application(m.clock[0]), fresh_read=True)
    assert error.value.code == "request_flag_capacity"
    assert m.store.db.execute("SELECT count(*) FROM join_flags").fetchone()[0] == 4096
    assert not m.calls


async def test_ack_uses_official_50_qps_robot_budget_and_window(dispatch):
    s = dispatch
    clock = [s.owner.messages.now()]
    s.owner.messages.clock = lambda: clock[0]
    robot = s.service.robot
    other = robot_key(RobotKey("another-app", "production"))
    with s.owner.messages.transaction():
        s.owner.messages.db.executemany("INSERT INTO extension_rates VALUES(?,?,?)",
            [(robot, "interaction_ack", clock[0])] * 49 + [(other, "interaction_ack", clock[0])] * 50)
    for number in (50, 51):
        payload = interaction(s.config, kind=12, interaction_id=f"ack-{number}", event_id=f"event-{number}")
        assert s.service.accept(payload, clock[0])
        await settle(s)
    assert s.calls == [("PUT", "/interactions/ack-50", {"code": 0})]
    record = next(row for row in s.service.records() if row["metadata"]["interaction_id"] == "ack-51")
    assert record["ack"] == "not_sent" and record["business"] == "not_executed"
    assert record["error"]["code"] == "extension_rate_limited"
    clock[0] += 1.01
    assert s.service.accept(interaction(s.config, kind=12, interaction_id="next-window", event_id="next-event"), clock[0])
    await settle(s)
    assert s.calls[-1] == ("PUT", "/interactions/next-window", {"code": 0}) and len(s.calls) == 2


@pytest.mark.parametrize("terminal", ["succeeded", "observed_approved"])
async def test_recent_completed_requests_keep_fences_without_occupying_live_budget(management, terminal):
    m = management
    robot, old = robot_key(m.http.identity.robot), m.clock[0] - 301
    with m.store.transaction():
        m.store.db.executemany("INSERT INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)",
            ((robot, "g", f"member-{n}", f"request-{n}", f"hash-{n}", old + 300, old, terminal, None) for n in range(4096)))
    assert m.service.observe_request("g", application(m.clock[0]), fresh_read=True)
    assert m.store.db.execute("SELECT count(*) FROM join_flags").fetchone()[0] == 4097
    original = {**application(old, "request-0"), "member_openid": "member-0"}
    assert m.service.observe_request("g", original, fresh_read=True) is None
    assert m.store.db.execute("SELECT state FROM join_flags WHERE request_id='request-0'").fetchone()[0] == terminal
    assert not m.calls


@pytest.mark.parametrize("state", ["pending", "retryable"])
async def test_expired_unattempted_flags_do_not_wait_another_day(management, state):
    m = management
    robot, old = robot_key(m.http.identity.robot), m.clock[0] - 301
    with m.store.transaction():
        m.store.db.executemany("INSERT INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)",
            ((robot, "g", f"member-{n}", f"request-{n}", hashlib.sha256(f"expired-token-{n}".encode()).hexdigest(), old + 300, old, state, None) for n in range(4096)))
    assert m.service.observe_request("g", application(m.clock[0]), fresh_read=True)
    assert m.store.db.execute("SELECT count(*) FROM join_flags").fetchone()[0] == 1
    with pytest.raises(V2Error) as error:
        await m.service.approve("expired-token-0", approve=True)
    assert error.value.code == "request_flag_invalid" and not m.calls
