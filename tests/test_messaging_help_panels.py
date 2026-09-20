import copy
import json
from types import SimpleNamespace

import pytest
from aiohttp import web

from test_messaging_state import NOW
from test_settings_commands import fixtures
from test_transport_http import MappedSession, upstream
from v2.commands import collect_catalog
from v2.errors import V2Error
from v2.help import node_token, render_help, text_link
from v2.messaging.store import MessageStore
from v2.models import InstanceKey
from v2.panels import PanelService
from v2.settings import DEFAULTS, SettingsStore
from v2.transport.http import HTTPTransport


def test_help_110_commands_pagination_permissions_and_no_execution():
    handlers, plugins, add, child_md, child = fixtures()
    for n in range(110):
        add(f"test{n}", module="data.plugins.many.main")
    catalog = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    first = render_help(catalog, DEFAULTS, "search 0 test")
    items = []
    for page in range(first["pages"]):
        value = render_help(catalog, DEFAULTS, f"search {page} test")
        items.extend(item["id"] for item in value["items"])
        assert len(value["text"]) <= 4096 and "真实@" in value["text"]
    assert len(items) == len(set(items)) == 110
    node = next(n for n in catalog["nodes"] if n["id"] == child_md.handler_full_name)
    with pytest.raises(V2Error):
        render_help(catalog, DEFAULTS, "detail " + node_token(node))
    detail = render_help(catalog, DEFAULTS, "detail " + node_token(node), admin=True, markdown=True)
    assert "do-not-leak" not in detail["text"] and "cmd-enter" not in detail["text"]
    assert "required_optional" in detail["text"] and "参数只作提示" in detail["text"]
    child.command_name = "renamed"
    child._cmpl_cmd_names = None
    renamed = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    assert "renamed" in render_help(renamed, DEFAULTS, "detail " + node_token(node), admin=True)["text"]
    child.handler_params["new-required"] = str
    changed = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    with pytest.raises(V2Error) as exc:
        render_help(changed, DEFAULTS, "detail " + node_token(node), admin=True)
    assert exc.value.code == "menu_node_expired"


def test_text_chain_encoding_injection_and_limits():
    link = text_link('/x "<& 中文', '显示"<&', scene="group")
    assert "%22%3C%26" in link and '<&' not in link and 'cmd-input' in link
    assert text_link("中" * 12, "x", scene="c2c") is None
    with pytest.raises(V2Error):
        text_link("/x", "x", scene="group", enter=True)
    handlers, plugins, _, _, _ = fixtures()
    handlers[0].desc = '<qqbot-cmd-enter text="evil" />'
    settings = copy.deepcopy(DEFAULTS)
    settings["title"] = '<qqbot-cmd-enter text="evil" />'
    catalog = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    result = render_help(catalog, settings, "system", markdown=True)
    assert '<qqbot-cmd-enter' not in result["text"] and '&lt;qqbot' in result["text"]


@pytest.fixture
async def panel_env(config, tmp_path):
    clock = [NOW]
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    settings = SettingsStore(tmp_path / "settings")
    identity = InstanceKey.from_config(config)
    handlers, plugins, add, child_md, child = fixtures()
    calls, records, modes = [], {}, []
    async def handle(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "panel-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot panel-fixture"
        body = await request.json() if request.method != "GET" else dict(request.query)
        calls.append((request.method, request.path, body))
        if request.method == "GET" and request.path == "/v2/panels":
            assert set(body) <= {"scope", "limit", "cursor"} and body["limit"] == "50"
            return web.json_response({"records": [r for r in records.values() if r["scope"] == body["scope"]], "is_end": True, "next_cursor": ""})
        if request.method == "GET":
            return web.json_response(records[request.path.rsplit("/", 1)[-1]])
        mode = modes.pop(0) if modes else "ok"
        if request.method == "POST":
            panel_id = "owned-" + str(len(records))
            records[panel_id] = {**copy.deepcopy(body), "panel_id": panel_id, "version": 1}
            if mode == "unknown":
                return web.Response(status=500, text="unknown")
            return web.json_response({"panel_id": panel_id})
        panel_id = request.path.rsplit("/", 1)[-1]
        if mode == "reject":
            return web.json_response({"code": 40030020})
        records[panel_id]["panel"] = copy.deepcopy(body["panel"])
        records[panel_id]["version"] += 1
        return web.json_response({"version": records[panel_id]["version"]})
    async with upstream(handle) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        instance = SimpleNamespace(identity=identity, http=http, check_generation=lambda: None)
        owner = SimpleNamespace(messages=store, store=settings, stopping=False, instances={identity.platform_id: instance},
            config={"remote_menu_sync": False}, catalog_ready=True,
            context=SimpleNamespace(get_config=lambda umo=None: {"wake_prefix": ["/"]}))
        # PanelService takes actual instance objects, as the plugin does.
        owner.instances = [instance]
        def catalog(instance, scene, target_type, targets):
            return collect_catalog({"wake_prefix": ["/"]}, scene, handlers=handlers, plugins=plugins)
        service = PanelService(owner, clock=lambda: clock[0], stability_seconds=0, catalog_provider=catalog)
        try:
            yield SimpleNamespace(clock=clock, owner=owner, instance=instance, service=service, records=records,
                calls=calls, modes=modes, handlers=handlers, plugins=plugins, add=add, child=child, settings=settings)
        finally:
            await service.close()
            await http.close()
            settings.close()
            store.close()


async def enable(env, **options):
    env.owner.config["remote_menu_sync"] = True
    plan = env.service.plan(env.instance, "group", **options)
    return await env.service.enable(env.instance, "group", plan["fingerprint"], confirm=True, **options)


async def test_panel_preview_zero_network_and_independent_gate(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "group")
    assert plan["payload"]["scope"] == "group" and not e.calls
    with pytest.raises(V2Error) as exc:
        await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=True)
    assert exc.value.code == "remote_sync_disabled" and not e.calls
    e.owner.config["remote_menu_sync"] = True
    with pytest.raises(V2Error):
        await e.service.enable(e.instance, "group", plan["fingerprint"], confirm=False)
    assert not e.calls


async def test_panel_owned_diff_rename_drift_and_disabled_gate(panel_env):
    e = panel_env
    result = await enable(e)
    panel_id = result["panel_id"]
    assert result["state"] == "synced" and len(e.records) == 1
    assert len([c for c in e.calls if c[0] == "POST"]) == 1
    await e.service.sync(e.instance, "group")
    assert len([c for c in e.calls if c[0] != "GET"]) == 1
    e.handlers[0].desc = "updated description"
    await e.service.sync(e.instance, "group")
    assert e.calls[-1][0] == "PUT" and e.calls[-1][1] == "/v2/panels/" + panel_id
    e.records[panel_id]["panel"]["remark"] = "operator changed it"
    with pytest.raises(V2Error) as exc:
        await e.service.sync(e.instance, "group")
    assert exc.value.code == "panel_drift"
    assert e.records[panel_id]["panel"]["remark"] == "operator changed it"
    e.owner.config["remote_menu_sync"] = False
    count = len(e.calls)
    with pytest.raises(V2Error):
        await e.service.sync(e.instance, "group")
    assert len(e.calls) == count


async def test_panel_unknown_create_reconciles_not_recreated(panel_env):
    e = panel_env
    e.modes.append("unknown")
    with pytest.raises(V2Error) as exc:
        await enable(e)
    assert exc.value.phase == "result_unknown" and len(e.records) == 1
    result = await e.service.sync(e.instance, "group")
    assert result["state"] == "synced"
    assert sum(method == "POST" for method, _, _ in e.calls) == 1


async def test_external_panels_count_toward_twenty_and_never_adopted(panel_env):
    e = panel_env
    for n in range(20):
        e.records[str(n)] = {"panel_id": str(n), "scope": "c2c", "target_type": "all", "panel": {"items": [], "remark": "astrbot v2"}}
    with pytest.raises(V2Error) as exc:
        await enable(e)
    assert exc.value.code == "panel_limit"
    assert len(e.records) == 20 and all(method == "GET" for method, _, _ in e.calls)


async def test_capacity_no_truncation_and_new_binding_requires_confirmation(panel_env):
    e = panel_env
    for n in range(25):
        e.add("system" + str(n))
    with pytest.raises(V2Error) as exc:
        await enable(e)
    assert exc.value.code == "panel_invalid" and not e.calls
    # Explicitly selecting menu-only is not silent truncation.
    result = await enable(e, menu_only=True)
    assert len(e.records[result["panel_id"]]["panel"]["items"]) == 1
