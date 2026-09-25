import asyncio
from types import SimpleNamespace

import pytest
from test_messaging_help_panels import enable
from test_messaging_help_panels import panel_env as panel_env
from test_messaging_state import NOW, chat_payload

from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.store import robot_key
from v2.models import InstanceKey
from v2.panels import PanelService
from v2.protocol import RawEnvelope


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
    else:
        field = {"platform": "id", "appid": "appid", "environment": "environment"}[changed]
        value = "sandbox" if changed == "environment" else "other"
        instance = SimpleNamespace(identity=InstanceKey.from_config({**config, field: value}))
    with pytest.raises(V2Error) as exc:
        e.service.plan(instance, "group", target_type="specific", targets=[target])
    assert exc.value.code == "identity_not_observed"
    assert sum(c[0] == "POST" for c in e.calls) == 1
