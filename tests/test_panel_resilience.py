import asyncio
import copy
from types import SimpleNamespace

import pytest
from test_messaging_help_panels import enable
from test_messaging_help_panels import panel_env as panel_env
from test_messaging_state import NOW, chat_payload

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.extensions.management import Management
from v2.extensions.state import ExtensionStore
from v2.messaging.convert import convert_chat
from v2.messaging.store import robot_key
from v2.models import InstanceKey, RobotKey
from v2.panels import PanelService
from v2.protocol import RawEnvelope


def native_panel_client(env):
    extension = ExtensionStore(env.owner.messages)
    state = ClientState(env.instance.identity)
    state.http = env.instance.http
    state.sender = SimpleNamespace()
    state.guard = env.instance.check_generation
    state.extension_state = env.owner.extension_state = extension
    state.panels = env.owner.panels = env.service
    manager = Management(env.instance.identity, env.instance.http, extension, env.owner.messages,
                         settings=lambda: {"management_writes": True})
    state.management = manager
    return V2Client(env.instance.identity, state=state), manager, extension

async def worker_ticks(env, service=None):
    service = service or env.service
    paused, resume = asyncio.Queue(), asyncio.Queue()

    async def scheduler(delay):
        assert delay == 5
        paused.put_nowait(None)
        await resume.get()

    service.sleep = scheduler
    service.start()
    await asyncio.wait_for(paused.get(), 2)

    async def tick(seconds=120):
        env.clock[0] += seconds
        resume.put_nowait(None)
        await asyncio.wait_for(paused.get(), 2)

    return tick


async def test_legacy_panel_rate_rows_do_not_block_server_requests(panel_env):
    e = panel_env
    robot = robot_key(e.instance.identity.robot)
    with e.owner.messages.transaction():
        e.owner.messages.db.executemany("INSERT INTO panel_rates VALUES(?,?,?)",
            [(robot, "read", e.clock[0])] * 30 + [(robot, "write", e.clock[0])] * 10)
    await enable(e)
    assert e.service.state(e.instance, "group")["state"] == "synced"
    assert any(method == "POST" for method, *_ in e.calls)
    assert e.owner.messages.db.execute("SELECT count(*) FROM panel_rates").fetchone()[0] == 40


@pytest.mark.parametrize("stage", ["list", "detail"])
@pytest.mark.parametrize("code,status,http_status,phase", [
    ("network_failure", 503, None, "result_unknown"),
    ("connect_failed", 503, None, "not_sent"),
    ("request_deadline", 504, None, "result_unknown"),
    ("request_capacity", 429, None, "not_sent"),
    ("qq_rate_limited", 429, 429, "rejected"),
    ("qq_api_error", 503, 503, "result_unknown"),
    ("qq_api_error", 429, 429, "rejected"),
    ("token_refresh_failed", 503, 503, "rejected"),
    ("token_refresh_failed", 503, None, "rejected"),
])
async def test_panel_read_outage_recovers_automatically(panel_env, monkeypatch, stage, code, status, http_status, phase):
    e = panel_env
    if stage == "detail":
        await enable(e)
        e.handlers[0].desc = "updated while offline"
    original = e.instance.http.request

    async def failed(spec, **kwargs):
        assert spec.method == "GET"
        raise V2Error(code, "fixture exhausted read retries", status=status, http_status=http_status, phase=phase)

    monkeypatch.setattr(e.instance.http, "request", failed)
    with pytest.raises(V2Error):
        if stage == "list":
            await enable(e)
        else:
            await e.service.sync(e.instance, "group")
    monkeypatch.setattr(e.instance.http, "request", original)
    tick = await worker_ticks(e)
    await tick()
    assert e.service.state(e.instance, "group")["state"] == "synced"
    assert sum(c[0] == "POST" for c in e.calls) == 1
    assert sum(c[0] == "PUT" for c in e.calls) == (stage == "detail")


async def test_panel_retry_after_does_not_hot_loop(panel_env, monkeypatch):
    e = panel_env
    original = e.instance.http.request

    async def limited(spec, **kwargs):
        raise V2Error("qq_rate_limited", "fixture limit", status=429, business_code=50002, http_status=429, retry_after="90", phase="rejected")

    monkeypatch.setattr(e.instance.http, "request", limited)
    with pytest.raises(V2Error):
        await enable(e)
    monkeypatch.setattr(e.instance.http, "request", original)
    tick = await worker_ticks(e)
    await tick(50)
    assert not e.calls
    await tick(41)
    assert e.service.state(e.instance, "group")["state"] == "synced"
    assert sum(c[0] == "POST" for c in e.calls) == 1


@pytest.mark.parametrize("code,status,business_code", [("qq_api_error", 403, None), ("qq_api_error", 502, 40030020), ("invalid_panel_response", 502, None)])
async def test_deterministic_read_failure_is_not_retried_forever(panel_env, monkeypatch, code, status, business_code):
    e = panel_env
    attempts = []

    async def failed(spec, **kwargs):
        attempts.append(spec.method)
        raise V2Error(code, "fixture deterministic failure", status=status, http_status=status, business_code=business_code)

    monkeypatch.setattr(e.instance.http, "request", failed)
    with pytest.raises(V2Error):
        await enable(e)
    assert e.service.state(e.instance, "group").get("failed_fingerprint")
    tick = await worker_ticks(e)
    await tick()
    assert attempts == ["GET"]


@pytest.mark.parametrize("scene,event,target", [("group", "GROUP_AT_MESSAGE_CREATE", "group-one"), ("c2c", "C2C_MESSAGE_CREATE", "user-one")])
@pytest.mark.parametrize("restart", [False, True])
async def test_confirmed_quiet_targets_keep_sync_without_refreshing_identity(panel_env, scene, event, target, restart):
    e = panel_env
    chat = convert_chat(e.instance.identity, RawEnvelope(chat_payload(event), NOW))
    e.owner.messages.observe(chat)
    e.owner.config["remote_menu_sync"] = True
    options = {"target_type": "specific", "targets": [target]}
    plan = e.service.plan(e.instance, scene, **options)
    await e.service.enable(e.instance, scene, plan["fingerprint"], confirm=True, **options)
    e.clock[0] += 86401
    with pytest.raises(V2Error, match="observation"):
        e.owner.messages.target(chat.route)
    e.handlers[0].desc = "updated after quiet day"
    service = e.service
    if restart:
        await service.close()
        service = PanelService(e.owner, clock=lambda: e.clock[0], stability_seconds=0, catalog_provider=service.catalog_provider)
    try:
        tick = await worker_ticks(e, service)
        await tick()
        assert service.state(e.instance, scene)["state"] == "synced"
        assert sum(c[0] == "POST" for c in e.calls) == 1
        assert sum(c[0] == "PUT" for c in e.calls) == 1
        with pytest.raises(V2Error, match="observation"):
            e.owner.messages.target(chat.route)
        assert service.plan(e.instance, scene, **options)["payload"]["scope"] == scene
    finally:
        await service.close()


@pytest.mark.parametrize("changed", ["target", "platform", "appid", "environment"])
async def test_confirmed_scope_cannot_authorize_unobserved_alternatives(panel_env, config, changed):
    e = panel_env
    e.owner.messages.observe(convert_chat(e.instance.identity, RawEnvelope(chat_payload(), NOW)))
    await enable(e, target_type="specific", targets=["group-one"])
    e.clock[0] += 86401
    target, instance = "group-one", e.instance
    if changed == "target":
        target = "unobserved"
    elif changed == "environment":
        # The sandbox robot key stays an internal identity even though configs ignore sandbox fields.
        instance = SimpleNamespace(identity=InstanceKey(config["id"], RobotKey(config["appid"], "sandbox"),
                                                       "websocket", tuple(config["shard"]), config["intents"]))
    else:
        field = {"platform": "id", "appid": "appid"}[changed]
        instance = SimpleNamespace(identity=InstanceKey.from_config({**config, field: "other"}))
    with pytest.raises(V2Error) as exc:
        e.service.plan(instance, "group", target_type="specific", targets=[target])
    assert exc.value.code == "identity_not_observed"
    assert sum(c[0] == "POST" for c in e.calls) == 1


@pytest.mark.parametrize("action", ["update", "delete"])
async def test_native_managed_panel_override_recovers_only_after_operator_confirmation(panel_env, action):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    try:
        initial = await enable(e)
        old_id = initial["panel_id"]
        if action == "update":
            panel = copy.deepcopy(e.records[old_id]["panel"])
            panel["remark"] = "manual-baseline"
            assert (await client.qq.update_panel(old_id, panel, operation_id="override-once"))["version"] == 2
            assert e.records[old_id]["panel"]["remark"] == "manual-baseline"
        else:
            assert await client.qq.delete_panel(old_id, operation_id="delete-once") == {}
            assert old_id not in e.records
        paused = e.service.state(e.instance, "group")
        assert not paused["enabled"] and paused["state"] == "paused"
        before = len([row for row in e.calls if row[0] in {"POST", "PUT", "DELETE"}])
        tick = await worker_ticks(e)
        await tick()
        assert len([row for row in e.calls if row[0] in {"POST", "PUT", "DELETE"}]) == before
        await e.service.close()
        e.service = PanelService(e.owner, clock=lambda: e.clock[0], stability_seconds=0,
                                 catalog_provider=e.service.catalog_provider)
        e.owner.panels = e.service
        plan = e.service.plan(e.instance, "group")
        with pytest.raises(V2Error) as unconfirmed:
            await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=False)
        assert unconfirmed.value.code == "confirmation_required"
        assert len([row for row in e.calls if row[0] in {"POST", "PUT", "DELETE"}]) == before
        result = await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert result["state"] == "synced" and result["enabled"] is True
        if action == "update":
            assert result["panel_id"] == old_id and result["previous"]["panel"]["remark"] == "manual-baseline"
            assert len([row for row in e.calls if row[0] in {"POST", "PUT"}]) == 2
        else:
            assert result["panel_id"] != old_id
            assert len([row for row in e.calls if row[0] == "POST"]) == 2
            assert len(e.records) == 1
    finally:
        await manager.close()
        await extension.close()


async def test_unknown_manual_panel_write_never_unlocks_through_confirmed_enable(panel_env):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    try:
        owned = (await enable(e))["panel_id"]
        panel = copy.deepcopy(e.records[owned]["panel"])
        panel["remark"] = "unknown-remote"
        e.modes.append("unknown")
        with pytest.raises(V2Error) as unknown:
            await client.qq.update_panel(owned, panel, operation_id="override-uncertain")
        assert unknown.value.phase == "result_unknown"
        paused = e.service.state(e.instance, "group")
        assert paused["enabled"] is False
        writes = len([row for row in e.calls if row[0] in {"POST", "PUT"}])
        plan = e.service.plan(e.instance, "group")
        with pytest.raises(V2Error) as blocked:
            await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert blocked.value.code == "panel_result_unknown"
        assert len([row for row in e.calls if row[0] in {"POST", "PUT"}]) == writes
    finally:
        await manager.close()
        await extension.close()


@pytest.mark.parametrize("action", ["update", "delete", "target"])
@pytest.mark.parametrize("outcome", ["not_sent", "rejected"])
@pytest.mark.parametrize("previously_disabled", [False, True])
async def test_definite_failed_manual_panel_write_restores_only_unchanged_operator_intent(panel_env, action, outcome, previously_disabled):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    try:
        owned = (await enable(e))["panel_id"]
        if previously_disabled:
            e.service.disable(e.instance, "group", confirm=True)
        initial = e.service.state(e.instance, "group")
        before = len(e.calls)
        original = e.instance.http.request
        async def failed(spec, *, before_send=None):
            if outcome == "rejected":
                before_send()
            raise V2Error("qq_api_error" if outcome == "rejected" else "connect_failed",
                          "definite fixture failure", phase=outcome, http_status=400 if outcome == "rejected" else None)
        e.instance.http.request = failed
        try:
            with pytest.raises(V2Error) as exc:
                if action == "update":
                    await client.qq.update_panel(owned, copy.deepcopy(e.records[owned]["panel"]), operation_id="fail-update")
                elif action == "delete":
                    await client.qq.delete_panel(owned, operation_id="fail-delete")
                else:
                    await client.qq.set_panel_target(owned, "add", group_openids=["g"], operation_id="fail-target")
            assert exc.value.phase == outcome
        finally:
            e.instance.http.request = original
        current = e.service.state(e.instance, "group")
        assert current["enabled"] == initial["enabled"] and current["state"] == initial["state"]
        assert current["panel_id"] == owned and current.get("manual") is None
        assert len(e.calls) == before and owned in e.records
    finally:
        await manager.close()
        await extension.close()


async def test_operator_disable_during_unattempted_manual_write_wins(panel_env):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    release, entered = asyncio.Event(), asyncio.Event()
    try:
        owned = (await enable(e))["panel_id"]
        before = len(e.calls)
        original = e.instance.http.request
        async def failed(spec, *, before_send=None):
            entered.set()
            await release.wait()
            raise V2Error("connect_failed", "fixture never sent", phase="not_sent")
        e.instance.http.request = failed
        write = asyncio.create_task(client.qq.update_panel(owned, copy.deepcopy(e.records[owned]["panel"]), operation_id="late-failure"))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert e.service.state(e.instance, "group")["enabled"] is False
            disabled = e.service.disable(e.instance, "group", confirm=True)
            assert disabled["enabled"] is False
            release.set()
            with pytest.raises(V2Error) as exc:
                await write
            assert exc.value.phase == "not_sent"
            assert e.service.state(e.instance, "group")["enabled"] is False
            assert len(e.calls) == before and owned in e.records
        finally:
            release.set()
            e.instance.http.request = original
            if not write.done():
                write.cancel()
                await asyncio.gather(write, return_exceptions=True)
    finally:
        await manager.close()
        await extension.close()


async def test_managed_panel_recovery_refuses_wrong_ledger_pending_and_foreign_ownership(panel_env):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    try:
        owned = (await enable(e))["panel_id"]
        e.records["third-party"] = {"scope": "c2c", "target_type": "all", "panel": {"items": [], "remark": "other"},
                                    "panel_id": "third-party", "version": 1}
        await client.qq.update_panel("third-party", {"items": [], "remark": "changed"}, operation_id="third-party-update")
        current = e.service.state(e.instance, "group")
        assert current["enabled"] and current["panel_id"] == owned and "manual" not in current
        foreign = SimpleNamespace(identity=InstanceKey("foreign", e.instance.identity.robot), check=lambda: None)
        with pytest.raises(V2Error) as conflict:
            await e.service.manual_write(foreign, owned, lambda: asyncio.sleep(0))
        assert conflict.value.code == "panel_owner_conflict"
        current["pending"] = {"kind": "update", "payload": current["previous"]}
        e.service._save(robot_key(e.instance.identity.robot), "group", current)
        before = len(e.calls)
        with pytest.raises(V2Error) as pending:
            await client.qq.delete_panel(owned, operation_id="blocked-pending")
        assert pending.value.code == "panel_result_unknown" and len(e.calls) == before
        plan = e.service.plan(e.instance, "group")
        with pytest.raises(V2Error) as pending_enable:
            await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert pending_enable.value.code == "panel_result_unknown" and len(e.calls) == before
    finally:
        await manager.close()
        await extension.close()


async def test_managed_panel_recovery_does_not_adopt_success_from_colliding_operation(panel_env):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    try:
        owned = (await enable(e))["panel_id"]
        from v2.extensions.state import digest
        robot = e.instance.identity.robot
        collision = "foreign-op"
        binding = digest(["DELETE", "/v2/panels/unrelated", None, None])
        fresh, _ = extension.begin(robot, collision, "delete_panel", binding)
        assert fresh
        extension.attempt(robot, collision)
        extension.finish(robot, collision, "succeeded", result={"state": "succeeded"})
        before = len(e.calls)
        with pytest.raises(V2Error) as wrong:
            await client.qq.update_panel(owned, copy.deepcopy(e.records[owned]["panel"]), operation_id=collision)
        assert wrong.value.code == "operation_conflict" and len(e.calls) == before
        assert not e.service.state(e.instance, "group")["enabled"]
        plan = e.service.plan(e.instance, "group")
        with pytest.raises(V2Error) as disallowed:
            await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert disallowed.value.code == "operation_conflict" and len(e.calls) == before
        assert e.service.state(e.instance, "group")["panel_id"] == owned
    finally:
        await manager.close()
        await extension.close()


async def test_disable_during_manual_confirmation_cannot_be_overwritten(panel_env):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    entered, release = asyncio.Event(), asyncio.Event()
    original = e.instance.http.request
    task = None
    try:
        owned = (await enable(e))["panel_id"]
        panel = copy.deepcopy(e.records[owned]["panel"])
        panel["remark"] = "manual"
        await client.qq.update_panel(owned, panel, operation_id="manual-before-disable")
        assert not e.service.state(e.instance, "group")["enabled"]
        plan = e.service.plan(e.instance, "group")

        async def delayed(spec, **kwargs):
            if spec.method == "GET" and spec.path == f"/v2/panels/{owned}":
                entered.set()
                await release.wait()
            return await original(spec, **kwargs)

        e.instance.http.request = delayed
        task = asyncio.create_task(e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True))
        await asyncio.wait_for(entered.wait(), 2)
        e.service.disable(e.instance, "group", confirm=True)
        release.set()
        with pytest.raises(V2Error) as exc:
            await task
        assert exc.value.code == "config_conflict"
        state = e.service.state(e.instance, "group")
        assert not state["enabled"] and state["manual"]["op_id"] == "manual-before-disable"
        assert sum(c[0] == "PUT" for c in e.calls) == 1
    finally:
        release.set()
        e.instance.http.request = original
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await manager.close()
        await extension.close()


@pytest.mark.parametrize("phase", ["not_sent", "rejected"])
async def test_failed_target_after_disable_requires_confirmed_enable_with_original_scope(panel_env, phase):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    entered, release = asyncio.Event(), asyncio.Event()
    original = e.instance.http.request
    work = None
    try:
        owned = (await enable(e))["panel_id"]
        before = len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}])
        async def fail(spec, *, before_send=None):
            assert spec.method == "PUT" and spec.path == f"/v2/panels/{owned}/target"
            entered.set()
            await release.wait()
            if phase == "rejected":
                before_send()
            raise V2Error("qq_api_error" if phase == "rejected" else "connect_failed",
                          "fixture definite failure", phase=phase, http_status=400 if phase == "rejected" else None)
        e.instance.http.request = fail
        work = asyncio.create_task(client.qq.set_panel_target(owned, "add", group_openids=["g"], operation_id="target-override"))
        await asyncio.wait_for(entered.wait(), 2)
        e.service.disable(e.instance, "group", confirm=True)
        release.set()
        with pytest.raises(V2Error) as error:
            await work
        assert error.value.phase == phase
        assert extension.operation(e.instance.identity.robot, "target-override")["state"] == phase
        paused = e.service.state(e.instance, "group")
        assert not paused["enabled"] and paused["manual"]["kind"] == "set_panel_target"
        assert len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}]) == before
        e.instance.http.request = original
        plan = e.service.plan(e.instance, "group")
        result = await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert result["enabled"] and result["state"] == "synced" and result["panel_id"] == owned
        assert "manual" not in result and result["previous"] == e.service.state(e.instance, "group")["previous"]
        assert len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}]) == before
    finally:
        release.set()
        e.instance.http.request = original
        if work is not None and not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
        await manager.close()
        await extension.close()


@pytest.mark.parametrize("mode", ["ok", "unknown"])
async def test_successful_or_unknown_target_change_cannot_resume_original_managed_scope(panel_env, mode):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    try:
        owned = (await enable(e))["panel_id"]
        e.modes.append(mode)
        if mode == "unknown":
            with pytest.raises(V2Error) as error:
                await client.qq.set_panel_target(owned, "add", group_openids=["g"], operation_id="changed-target")
            assert error.value.phase == "result_unknown"
        else:
            assert await client.qq.set_panel_target(owned, "add", group_openids=["g"], operation_id="changed-target") == {}
        assert e.records[owned]["group_openids"] == ["g"]
        paused = e.service.state(e.instance, "group")
        assert not paused["enabled"] and paused["manual"]["kind"] == "set_panel_target"
        writes = len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}])
        plan = e.service.plan(e.instance, "group")
        with pytest.raises(V2Error) as blocked:
            await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert blocked.value.code == ("panel_result_unknown" if mode == "unknown" else "panel_scope_locked")
        assert not e.service.state(e.instance, "group")["enabled"]
        assert len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}]) == writes
    finally:
        await manager.close()
        await extension.close()


@pytest.mark.parametrize("ledger", ["missing", "different_binding", "partial", "remote_drift"])
async def test_failed_target_resume_requires_matching_terminal_ledger(panel_env, ledger):
    e = panel_env
    client, manager, extension = native_panel_client(e)
    original = e.instance.http.request
    try:
        owned = (await enable(e))["panel_id"]
        async def failed(spec, *, before_send=None):
            e.service.disable(e.instance, "group", confirm=True)
            raise V2Error("connect_failed", "fixture never sent", phase="not_sent")
        e.instance.http.request = failed
        with pytest.raises(V2Error) as error:
            await client.qq.set_panel_target(owned, "add", group_openids=["g"], operation_id="target-ledger")
        assert error.value.phase == "not_sent"
        e.instance.http.request = original
        assert e.service.state(e.instance, "group")["manual"]["op_id"] == "target-ledger"
        if ledger == "remote_drift":
            e.records[owned]["group_openids"] = ["external"]
        else:
            with e.owner.messages.transaction():
                if ledger == "missing":
                    extension.db.execute("DELETE FROM extension_ops WHERE op_id=?", ("target-ledger",))
                elif ledger == "different_binding":
                    extension.db.execute("UPDATE extension_ops SET binding=? WHERE op_id=?", ("another-request", "target-ledger"))
                else:
                    extension.db.execute("UPDATE extension_ops SET state='partial' WHERE op_id=?", ("target-ledger",))
        writes = len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}])
        plan = e.service.plan(e.instance, "group")
        with pytest.raises(V2Error) as blocked:
            await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
        assert blocked.value.code == ("operation_conflict" if ledger == "different_binding" else
                                      "panel_drift" if ledger == "remote_drift" else "panel_result_unknown")
        assert not e.service.state(e.instance, "group")["enabled"]
        assert len([call for call in e.calls if call[0] in {"POST", "PUT", "DELETE"}]) == writes
    finally:
        e.instance.http.request = original
        await manager.close()
        await extension.close()
