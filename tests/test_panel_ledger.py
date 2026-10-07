"""Managed panel writes share the extension_ops ledger; recovery is ledger-first."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from test_messaging_help_panels import enable
from test_messaging_help_panels import panel_env as panel_env
from test_panel_resilience import worker_ticks

from v2.errors import V2Error
from v2.extensions.state import digest as ledger_digest
from v2.messaging.store import robot_key
from v2.protocol import RequestSpec


def arm_pending(e, pending, *, panel_id=None, previous=None):
    """Persist an enabled panel row interrupted mid-write, as a crash would leave it."""
    e.owner.config["remote_menu_sync"] = True
    plan = e.service.plan(e.instance, "group")
    value = {"state": "unknown", "enabled": True, "platform_id": e.instance.identity.platform_id,
             "intent": {"target_type": "all", "targets": [], "menu_only": False},
             "panel_id": panel_id, "previous": previous, "pending": pending,
             "error": "interrupted", "checked": e.clock[0]}
    e.service._save(robot_key(e.instance.identity.robot), "group", value, control=True)
    return plan


def create_payload(plan):
    payload = copy.deepcopy(plan["payload"])
    payload["panel"]["remark"] = "astrbot-v2:fixture"
    return payload


def ledger_rows(e):
    return e.owner.extension_state.db.execute(
        "SELECT op_id,kind,binding,state,result,error,context FROM extension_ops ORDER BY rowid").fetchall()


def write_calls(e):
    return [call for call in e.calls if call[0] != "GET"]


async def test_sync_writes_enter_shared_extension_ledger(panel_env):
    e = panel_env
    in_flight = []
    original = e.instance.http.request

    async def capture(spec, **kwargs):
        if spec.method == "POST":
            in_flight.append(e.service.state(e.instance, "group"))
        return await original(spec, **kwargs)

    e.instance.http.request = capture
    try:
        result = await enable(e)
    finally:
        e.instance.http.request = original
    rows = ledger_rows(e)
    assert len(rows) == 1
    # The pending write is persisted with its op_id before the request is sent.
    assert in_flight[0]["state"] == "writing" and in_flight[0]["pending"]["op_id"] == rows[0]["op_id"]
    body = next(b for m, _, b in e.calls if m == "POST")
    assert rows[0]["kind"] == "panel_sync" and rows[0]["state"] == "succeeded"
    assert rows[0]["binding"] == ledger_digest(["POST", "/v2/panels", None, body])
    assert json.loads(rows[0]["result"]) == {"state": "succeeded", "panel_id": result["panel_id"]}
    assert json.loads(rows[0]["context"]) == {"scene": "group", "panel_id": None, "method": "POST", "path": "/v2/panels"}
    e.handlers[0].desc = "ledger update"
    await e.service.sync(e.instance, "group")
    rows = ledger_rows(e)
    assert len(rows) == 2 and rows[1]["kind"] == "panel_sync" and rows[1]["state"] == "succeeded"
    put = next(b for m, _, b in e.calls if m == "PUT")
    assert rows[1]["binding"] == ledger_digest(["PUT", "/v2/panels/" + result["panel_id"], None, put])
    assert json.loads(rows[1]["result"]) == {"state": "succeeded", "version": 2}
    assert json.loads(rows[1]["context"])["panel_id"] == result["panel_id"]


async def test_crash_after_successful_create_adopts_retained_panel_id(panel_env):
    e = panel_env
    created = (await enable(e))["panel_id"]
    row = ledger_rows(e)[0]
    body = next(b for m, _, b in e.calls if m == "POST")
    value = e.service.state(e.instance, "group")
    value.update(panel_id=None, previous=None, fingerprint=None, state="unknown", error="interrupted",
                 pending={"kind": "create", "payload": body, "baseline": [], "op_id": row["op_id"],
                          "fingerprint": value["fingerprint"]})
    e.service._save(robot_key(e.instance.identity.robot), "group", value)
    result = await e.service.sync(e.instance, "group")
    assert result["state"] == "synced" and result["panel_id"] == created
    assert sum(m == "POST" for m, _, _ in e.calls) == 1
    assert ("GET", "/v2/panels/" + created) in [(m, p) for m, p, _ in e.calls]


async def test_crash_before_send_or_rejected_write_resets_to_failed(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "group")
    payload = create_payload(plan)
    extension = e.owner.extension_state
    robot = e.instance.identity.robot
    binding = ledger_digest(["POST", "/v2/panels", None, payload])
    fresh, _ = extension.begin(robot, "interrupted-create", "panel_sync", binding)
    assert fresh
    extension.finish(robot, "interrupted-create", "not_sent", error={
        "code": "connect_failed", "message": "never sent", "status": 503, "retcode": 1400,
        "business_code": None, "trace_id": None, "retry_after": None, "http_status": None,
        "phase": "not_sent", "operation_id": "interrupted-create"})
    arm_pending(e, {"kind": "create", "payload": payload, "baseline": [], "op_id": "interrupted-create",
                    "fingerprint": plan["fingerprint"]})
    failed = await e.service.sync(e.instance, "group")
    assert failed["state"] == "failed" and failed["pending"] is None
    assert failed["error"]["code"] == "connect_failed" and failed["retry_at"] > e.clock[0]
    assert not write_calls(e)
    e.clock[0] += 10
    synced = await e.service.sync(e.instance, "group")
    assert synced["state"] == "synced" and len(e.records) == 1
    assert sum(m == "POST" for m, _, _ in e.calls) == 1


async def test_rejected_write_stays_failed_until_plan_changes(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "group")
    payload = create_payload(plan)
    extension = e.owner.extension_state
    robot = e.instance.identity.robot
    binding = ledger_digest(["POST", "/v2/panels", None, payload])
    fresh, _ = extension.begin(robot, "rejected-create", "panel_sync", binding)
    assert fresh
    extension.attempt(robot, "rejected-create")
    extension.finish(robot, "rejected-create", "rejected", error={
        "code": "qq_api_error", "message": "rejected", "status": 400, "retcode": 1400,
        "business_code": 40030020, "trace_id": None, "retry_after": None, "http_status": 400,
        "phase": "rejected", "operation_id": "rejected-create"})
    arm_pending(e, {"kind": "create", "payload": payload, "baseline": [], "op_id": "rejected-create",
                    "fingerprint": plan["fingerprint"]})
    failed = await e.service.sync(e.instance, "group")
    assert failed["state"] == "failed" and failed["pending"] is None
    assert failed["failed_fingerprint"] == plan["fingerprint"] and "retry_at" not in failed
    tick = await worker_ticks(e)
    await tick()
    assert not write_calls(e)
    assert e.service.state(e.instance, "group")["state"] == "failed"


async def test_unknown_ledger_write_stays_locked_without_replay(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "group")
    payload = create_payload(plan)
    extension = e.owner.extension_state
    robot = e.instance.identity.robot
    binding = ledger_digest(["POST", "/v2/panels", None, payload])
    fresh, _ = extension.begin(robot, "unknown-create", "panel_sync", binding)
    assert fresh
    extension.attempt(robot, "unknown-create")
    extension.finish(robot, "unknown-create", "unknown")
    # The remote side really has the panel, yet a retained unknown must not be adopted or replayed.
    e.records["maybe-created"] = {**copy.deepcopy(payload), "panel_id": "maybe-created", "version": 1}
    arm_pending(e, {"kind": "create", "payload": payload, "baseline": [], "op_id": "unknown-create",
                    "fingerprint": plan["fingerprint"]})
    with pytest.raises(V2Error) as exc:
        await e.service.sync(e.instance, "group")
    assert exc.value.code == "panel_result_unknown"
    state = e.service.state(e.instance, "group")
    assert state["state"] == "unknown" and state["pending"]["op_id"] == "unknown-create"
    assert not write_calls(e)


async def test_missing_ledger_row_falls_back_to_unique_remote_candidate(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "group")
    payload = create_payload(plan)
    e.records["orphan"] = {**copy.deepcopy(payload), "panel_id": "orphan", "version": 1}
    arm_pending(e, {"kind": "create", "payload": payload, "baseline": [], "op_id": "evicted-create",
                    "fingerprint": plan["fingerprint"]})
    result = await e.service.sync(e.instance, "group")
    assert result["state"] == "synced" and result["panel_id"] == "orphan"
    assert not write_calls(e)


@pytest.mark.parametrize("ledger", ["legacy", "missing"])
async def test_pending_without_usable_ledger_row_still_requires_unique_match(panel_env, ledger):
    e = panel_env
    plan = e.service.plan(e.instance, "group")
    payload = create_payload(plan)
    pending = {"kind": "create", "payload": payload, "baseline": [], "fingerprint": plan["fingerprint"]}
    if ledger == "missing":
        pending["op_id"] = "evicted-create"
    arm_pending(e, pending)
    with pytest.raises(V2Error) as exc:
        await e.service.sync(e.instance, "group")
    assert exc.value.code == "panel_result_unknown"
    assert not write_calls(e)
    e.records["orphan"] = {**copy.deepcopy(payload), "panel_id": "orphan", "version": 1}
    result = await e.service.sync(e.instance, "group")
    assert result["state"] == "synced" and result["panel_id"] == "orphan"
    assert not write_calls(e)


async def test_same_op_id_replays_cached_result_and_foreign_binding_conflicts(panel_env):
    e = panel_env
    http = e.instance.http
    spec = RequestSpec(e.instance.identity.robot.environment, "POST", "/v2/panels",
                       json_body={"scope": "group", "target_type": "all",
                                  "panel": {"items": [], "remark": "astrbot-v2:replay"}})

    def validate(data):
        return {"state": "succeeded", "panel_id": data["panel_id"]}

    result = await e.owner.extension_state.execute(http, spec, op_id="panel-op", kind="panel_sync", validate=validate)
    replayed = await e.owner.extension_state.execute(http, spec, op_id="panel-op", kind="panel_sync", validate=validate)
    assert replayed == result and sum(m == "POST" for m, _, _ in e.calls) == 1
    other = RequestSpec(e.instance.identity.robot.environment, "POST", "/v2/panels",
                        json_body={"scope": "c2c", "target_type": "all",
                                   "panel": {"items": [], "remark": "astrbot-v2:other"}})
    with pytest.raises(V2Error) as exc:
        await e.owner.extension_state.execute(http, other, op_id="panel-op", kind="panel_sync", validate=validate)
    assert exc.value.code == "operation_conflict"
    assert sum(m == "POST" for m, _, _ in e.calls) == 1


async def test_inflight_cancel_keeps_pending_and_locks_manual_writes(panel_env):
    e = panel_env
    owned = (await enable(e))["panel_id"]
    e.handlers[0].desc = "in-flight update"
    original = e.instance.http.request
    entered = asyncio.Event()

    async def hanging(spec, before_send=None, **kwargs):
        if spec.method == "PUT":
            before_send()
            entered.set()
            await asyncio.Event().wait()
        return await original(spec, before_send=before_send)

    e.instance.http.request = hanging
    task = asyncio.create_task(e.service.sync(e.instance, "group"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        e.instance.http.request = original
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    state = e.service.state(e.instance, "group")
    assert state["state"] == "unknown" and state["pending"]["op_id"]
    row = e.owner.extension_state.operation(e.instance.identity.robot, state["pending"]["op_id"])
    assert row["kind"] == "panel_sync" and row["state"] == "unknown"
    client = SimpleNamespace(identity=e.instance.identity, check=lambda: None)
    with pytest.raises(V2Error) as blocked:
        await e.service.manual_write(client, owned, lambda: None)
    assert blocked.value.code == "panel_result_unknown"
    with pytest.raises(V2Error) as locked:
        await e.service.sync(e.instance, "group")
    assert locked.value.code == "panel_result_unknown"
    assert not any(m == "PUT" for m, _, _ in e.calls)


async def test_sync_requires_extension_ledger(panel_env):
    e = panel_env
    e.owner.config["remote_menu_sync"] = True
    e.owner.extension_state = None
    with pytest.raises(V2Error) as exc:
        await e.service.sync(e.instance, "group")
    assert exc.value.code == "service_stopped"
    assert not e.calls
