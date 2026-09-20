import asyncio
import copy
import sqlite3

import pytest

from test_messaging_help_panels import enable, panel_env as panel_env
from test_messaging_send import sending as sending
from test_messaging_state import NOW, chat_payload
from test_settings_commands import collect_catalog
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.models import InstanceKey
from v2.panels import PanelService
from v2.protocol import RawEnvelope
from v2.settings import DEFAULTS


async def test_generation_rotation_after_upstream_receives_write_keeps_unknown(sending):
    s = sending
    chat, client = s.observe()
    s.modes.append("wait")
    task = asyncio.create_task(client.qq.send("group", "group-one", "in flight", operation_id="rotation"))
    await s.entered.wait()
    def stale():
        raise V2Error("stale_generation", "fixture rotation", status=409)
    s.http.guard = stale
    s.release.set()
    with pytest.raises(V2Error) as exc:
        await task
    assert exc.value.phase == "result_unknown" and exc.value.code == "stale_generation"
    assert s.store.operation(chat.route.robot, "rotation")["state"] == "unknown"
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1


async def test_expiry_while_waiting_for_http_slot_cannot_become_active(sending):
    s = sending
    chat, client = s.observe()
    s.http._slots = asyncio.Semaphore(0)
    original = s.store.reserve
    reserved = asyncio.Event()
    def reserve(*args):
        result = original(*args)
        reserved.set()
        return result
    s.store.reserve = reserve
    task = asyncio.create_task(client.send(chat.route, "waiting"))
    await reserved.wait()
    s.clock[0] += 301
    s.http._slots.release()
    with pytest.raises(V2Error) as exc:
        await task
    assert exc.value.code == "reply_expired" and not s.calls
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0


async def test_storage_failure_after_real_id_blocks_new_writes_and_preserves_inflight(sending, monkeypatch):
    s = sending
    chat, client = s.observe()
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("fixture disk unavailable")
    monkeypatch.setattr(s.store, "finish", fail)
    with pytest.raises(V2Error) as exc:
        await client.qq.send("group", "group-one", "sent but not recorded", operation_id="disk")
    assert exc.value.phase == "result_unknown" and exc.value.operation_id == "disk"
    assert s.store.operation(chat.route.robot, "disk")["state"] == "in_flight"
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "must not write")
    assert exc.value.code == "send_storage_unavailable" and len(s.calls) == 1


async def test_c2c_quota_four_channel_ws_requirement_and_chain_preflight(sending):
    s = sending
    chat, client = s.observe("C2C_MESSAGE_CREATE")
    results = await asyncio.gather(*(client.send_private_msg(user_id="user-one", message=str(n)) for n in range(6)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == len(s.calls) == 4
    chat, client = s.observe("AT_MESSAGE_CREATE")
    s.core.ws_online = lambda: False
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "needs WS")
    assert exc.value.code == "channel_ws_required" and len(s.calls) == 4
    chat, client = s.observe()
    for message in ('<qqbot-cmd-input text="a">', '<qqbot-cmd-input text="a" text="b" />'):
        with pytest.raises(V2Error):
            await client.qq.send("group", "group-one", message, markdown=True)
    assert len(s.calls) == 4


async def test_panel_unknown_missing_resource_stays_unknown_across_service_restart(panel_env):
    e = panel_env
    e.modes.append("unknown")
    with pytest.raises(V2Error):
        await enable(e)
    e.records.clear()
    await e.service.close()
    service = PanelService(e.owner, clock=lambda: e.clock[0], stability_seconds=0, catalog_provider=e.service.catalog_provider)
    try:
        with pytest.raises(V2Error) as exc:
            await service.sync(e.instance, "group")
        assert exc.value.code == "panel_result_unknown"
        assert service.state(e.instance, "group")["pending"]
        assert sum(c[0] == "POST" for c in e.calls) == 1
    finally:
        await service.close()


async def test_panel_stop_during_write_is_not_undone_by_late_result(panel_env):
    e = panel_env
    original = e.instance.http.request
    entered, release = asyncio.Event(), asyncio.Event()
    async def request(spec, **kwargs):
        response = await original(spec, **kwargs)
        if spec.method == "POST":
            entered.set()
            await release.wait()
        return response
    e.instance.http.request = request
    task = asyncio.create_task(enable(e))
    try:
        await entered.wait()
        e.service.disable(e.instance, "group", confirm=True)
        release.set()
        result = await task
        assert result["state"] == "synced" and not result["enabled"]
        assert not e.service.state(e.instance, "group")["enabled"] and len(e.records) == 1
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_rejected_panel_update_retains_previous_and_worker_does_not_repeat_same_error(panel_env):
    e = panel_env
    previous = await enable(e)
    e.handlers[0].desc = "new text"
    e.modes.append("reject")
    with pytest.raises(V2Error):
        await e.service.sync(e.instance, "group")
    state = e.service.state(e.instance, "group")
    assert state["previous"] == previous["previous"] and state["pending"] is None
    before = len(e.calls)
    paused, resume = asyncio.Queue(), asyncio.Queue()
    async def sleep(delay):
        paused.put_nowait(True)
        await resume.get()
    e.service.sleep = sleep
    e.service.start()
    await paused.get()
    e.clock[0] += 120
    resume.put_nowait(True)
    await asyncio.wait_for(paused.get(), 2)
    assert len(e.calls) == before


async def test_selected_handler_bindings_are_atomic_and_never_reassigned_by_name(panel_env, monkeypatch):
    e = panel_env
    def catalog(config, scene):
        return collect_catalog(config, scene, handlers=e.handlers, plugins=e.plugins)
    monkeypatch.setattr("v2.panels.collect_catalog", catalog)
    node = next(n for n in catalog({"wake_prefix": ["/"]}, "group")["nodes"] if n["name"] == "plugin")
    settings = copy.deepcopy(DEFAULTS)
    settings["panels"]["group"] = {"mode": "custom", "selected": [node["id"]]}
    key = e.instance.identity.settings_key
    saved = e.settings.mutate(key, 0, "fixture", operation="save", patch=settings)
    with pytest.raises(V2Error) as exc:
        e.service.capture_bindings(e.instance, saved["draft"])
    assert exc.value.code == "command_binding_confirmation"
    bindings = e.service.capture_bindings(e.instance, saved["draft"], confirm=True)
    e.settings.mutate(key, 1, "fixture", operation="apply", bindings=bindings)
    assert not e.service.plan(e.instance, "group")["issues"]
    async def replacement(self, event):
        raise AssertionError("Metadata must not execute replacement code")
    next(h for h in e.handlers if h.handler_full_name == node["id"]).handler = replacement
    assert "command_binding_confirmation" in e.service.plan(e.instance, "group")["issues"]
    assert e.settings.get(key)["applied"]["panels"]["group"]["selected"] == [node["id"]]
    assert not e.calls


async def test_specific_scope_uses_only_observed_current_robot_targets(panel_env):
    e = panel_env
    with pytest.raises(V2Error) as exc:
        e.service.plan(e.instance, "group", target_type="specific", targets=["not-observed"])
    assert exc.value.code == "identity_not_observed" and not e.calls
    chat = convert_chat(e.instance.identity, RawEnvelope(chat_payload(), NOW))
    e.owner.messages.observe(chat)
    value = await enable(e, target_type="specific", targets=["group-one"])
    assert e.records[value["panel_id"]]["group_openids"] == ["group-one"]
    with pytest.raises(V2Error) as exc:
        await enable(e)
    assert exc.value.code == "panel_scope_locked"


@pytest.mark.parametrize("author", [None, [], "wrong"])
def test_malformed_nested_author_is_a_diagnosable_bad_chat(config, author):
    payload = chat_payload()
    payload["d"]["msg_elements"] = [{"message_type": 102, "msg_idx": "REFIDX_bad", "author": author}]
    with pytest.raises(V2Error) as exc:
        convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))
    assert exc.value.code == "invalid_chat"


def test_p2_raw_inbox_migration_preserves_pending_and_acknowledged_records(tmp_path):
    import json
    from v2.transport.inbox import RawInbox
    path = tmp_path / "transport.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE inbox(row_id INTEGER PRIMARY KEY, owner TEXT NOT NULL, event_id TEXT, body TEXT, received REAL NOT NULL, delivered REAL, size INTEGER NOT NULL DEFAULT 0, UNIQUE(owner,event_id))")
    db.execute("PRAGMA user_version=2")
    body = json.dumps(chat_payload())
    db.execute("INSERT INTO inbox VALUES(1,'owner','pending',?,?,NULL,?)", (body, NOW, len(body.encode())))
    db.execute("INSERT INTO inbox VALUES(2,'owner','acknowledged',NULL,?,?,0)", (NOW, NOW))
    db.commit()
    db.close()
    inbox = RawInbox(path, clock=lambda: NOW)
    try:
        assert inbox.pending("owner")[0]["payload"] == chat_payload()
        assert inbox.diagnostics("owner") == {"pending": 1}
        assert inbox.db.execute("SELECT delivered FROM inbox WHERE row_id=2").fetchone()[0] == NOW
        assert inbox.db.execute("PRAGMA user_version").fetchone()[0] == 3
    finally:
        inbox.close()


def test_p1_settings_migration_preserves_saved_applied_and_history(tmp_path):
    from v2.settings import SettingsStore
    path = tmp_path / "settings.sqlite3"
    settings = SettingsStore(path)
    settings.mutate("key", 0, "fixture", operation="save", patch={"title": "kept title"})
    expected = settings.mutate("key", 1, "fixture", operation="apply")
    versions = settings.versions("key")
    settings.db.execute("DROP TABLE command_bindings")
    settings.db.execute("PRAGMA user_version=1")
    settings.db.commit()
    settings.close()
    restored = SettingsStore(path)
    try:
        assert restored.get("key") == expected and restored.versions("key") == versions
        assert restored.db.execute("SELECT count(*) FROM command_bindings").fetchone()[0] == 0
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 2
    finally:
        restored.close()


async def test_panel_scan_recovers_from_transient_storage_failure(panel_env):
    e = panel_env
    await enable(e)
    previous_calls = len(e.calls)
    real_db = e.service.db

    class FailScanOnce:
        failed = False

        def execute(self, sql, *args):
            if sql == "SELECT robot,scene,body FROM panels" and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("fixture transient database lock")
            return real_db.execute(sql, *args)

    paused, advance = asyncio.Queue(), asyncio.Queue()

    async def scheduler(delay):
        paused.put_nowait(None)
        await advance.get()

    e.service.db = FailScanOnce()
    e.service.sleep = scheduler
    e.service.start()
    await asyncio.wait_for(paused.get(), 1)
    advance.put_nowait(None)
    await asyncio.sleep(0)
    assert not e.service.worker.done(), "A transient scan failure must not kill automatic synchronization"
    assert e.service.last_error == "panel_storage_unavailable"
    assert len(e.calls) == previous_calls
    await asyncio.wait_for(paused.get(), 1)
    e.handlers[0].desc = "after recovery"
    e.clock[0] += 120
    advance.put_nowait(None)
    await asyncio.wait_for(paused.get(), 2)
    assert not e.service.worker.done() and e.service.last_error is None
    assert e.calls[-1][0] == "PUT"
    assert sum(method == "POST" for method, _, _ in e.calls) == 1
