import asyncio
import copy
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_messaging_state import NOW
from test_settings_commands import fixtures
from test_transport_http import MappedSession, upstream

from v2.commands import collect_catalog
from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.help import node_token, render_help, text_link
from v2.messaging.store import MessageStore, robot_key
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
    assert "%22%3C%26" in link and "<&" not in link and "cmd-input" in link
    assert text_link("中" * 12, "x", scene="c2c") is None
    with pytest.raises(V2Error):
        text_link("/x", "x", scene="group", enter=True)
    handlers, plugins, _, _, _ = fixtures()
    handlers[0].desc = '<qqbot-cmd-enter text="evil" />'
    settings = copy.deepcopy(DEFAULTS)
    settings["title"] = '<qqbot-cmd-enter text="evil" />'
    catalog = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    result = render_help(catalog, settings, "system", markdown=True)
    assert "<qqbot-cmd-enter" not in result["text"] and "&lt;qqbot" in result["text"]


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
        body = await request.json() if request.method in {"POST", "PUT"} else dict(request.query) if request.method == "GET" else None
        calls.append((request.method, request.path, body))
        if request.method == "GET" and request.path == "/v2/panels":
            assert set(body) <= {"scope", "limit", "cursor"} and body["limit"] == "50"
            return web.json_response({"records": [r for r in records.values() if r["scope"] == body["scope"]], "is_end": True, "next_cursor": ""})
        if request.method == "GET":
            return web.json_response(records[request.path.rsplit("/", 1)[-1]])
        mode = modes.pop(0) if modes else "ok"
        if request.method == "POST":
            panel_id = "owned-" + str(sum(method == "POST" and path == "/v2/panels" for method, path, _ in calls) - 1)
            records[panel_id] = {**copy.deepcopy(body), "panel_id": panel_id, "version": 1}
            if mode == "unknown":
                return web.Response(status=500, text="unknown")
            return web.json_response({"panel_id": panel_id})
        panel_id = request.path.split("/")[-2] if request.path.endswith("/target") else request.path.rsplit("/", 1)[-1]
        if mode == "reject":
            return web.json_response({"code": 40030020})
        if request.method == "PUT" and request.path.endswith("/target"):
            field = "group_openids" if "group_openids" in body else "user_openids"
            previous = records[panel_id].get(field, [])
            records[panel_id][field] = sorted(set(previous) | set(body[field])) if body["op"] == "add" else sorted(set(previous) - set(body[field]))
            return web.Response(status=500, text="unknown") if mode == "unknown" else web.json_response({})
        if request.method == "DELETE":
            records.pop(panel_id, None)
            return web.Response(status=500, text="unknown") if mode == "unknown" else web.json_response({})
        records[panel_id]["panel"] = copy.deepcopy(body["panel"])
        records[panel_id]["version"] += 1
        if mode == "unknown":
            return web.Response(status=500, text="unknown")
        return web.json_response({"version": records[panel_id]["version"]})
    async with upstream(handle) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        instance = SimpleNamespace(identity=identity, http=http, check_generation=lambda: None)
        extension = ExtensionStore(store)
        owner = SimpleNamespace(messages=store, store=settings, stopping=False, instances={identity.platform_id: instance},
            config={"remote_menu_sync": False}, catalog_ready=True, extension_state=extension,
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
            await extension.close()
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


async def test_panel_unknown_create_stays_locked_and_never_replayed(panel_env):
    e = panel_env
    e.modes.append("unknown")
    with pytest.raises(V2Error) as exc:
        await enable(e)
    assert exc.value.phase == "result_unknown" and len(e.records) == 1
    # The ledger retains the write as unknown: sync stays locked and never replays or adopts remotely.
    with pytest.raises(V2Error) as locked:
        await e.service.sync(e.instance, "group")
    assert locked.value.code == "panel_result_unknown"
    state = e.service.state(e.instance, "group")
    assert state["state"] == "unknown" and state["pending"]["op_id"]
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


async def test_manual_panel_write_pauses_only_owned_scope_and_serializes_sync(panel_env):
    e = panel_env
    managed = await enable(e)
    owned = managed["panel_id"]
    client = SimpleNamespace(identity=e.instance.identity, check=lambda: None)
    entered, finish = asyncio.Event(), asyncio.Event()
    async def manual():
        entered.set()
        await finish.wait()
        return {"version": 2}
    write = asyncio.create_task(e.service.manual_write(client, owned, manual))
    await entered.wait()
    current = e.service.state(e.instance, "group")
    assert current["state"] == "paused" and current["enabled"] is False
    sync = asyncio.create_task(e.service.sync(e.instance, "group"))
    await asyncio.sleep(0)
    assert not sync.done()
    finish.set()
    assert await write == {"version": 2}
    with pytest.raises(V2Error, match="Confirm management"):
        await sync
    assert sum(method in ("POST", "PUT") for method, _, _ in e.calls) == 1
    # Unmanaged panel IDs are never copied into the ownership table.
    assert await e.service.manual_write(client, "someone-else", lambda: asyncio.sleep(0, result={})) == {}
    assert e.service.state(e.instance, "group")["panel_id"] == owned


async def test_manual_panel_write_refuses_unresolved_managed_result(panel_env):
    e = panel_env
    value = await enable(e)
    current = e.service.state(e.instance, "group")
    current["pending"] = {"kind": "update"}
    e.service._save(robot_key(e.instance.identity.robot), "group", current)
    client = SimpleNamespace(identity=e.instance.identity, check=lambda: None)
    with pytest.raises(V2Error) as pending:
        await e.service.manual_write(client, value["panel_id"], lambda: asyncio.sleep(0))
    assert pending.value.code == "panel_result_unknown"
    assert e.service.state(e.instance, "group")["enabled"] is True



async def test_c2c_all_panel_is_distinct_from_global_custom_menu(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "c2c")
    assert not e.calls and not plan["issues"]
    assert plan["payload"]["scope"] == "c2c" and plan["payload"]["target_type"] == "all"
    assert "user_openids" not in plan["payload"]
    with pytest.raises(V2Error) as exc:
        await e.service.enable(e.instance, "c2c", plan["fingerprint"], confirm=True)
    assert exc.value.code == "remote_sync_disabled" and not e.calls
    e.owner.config["remote_menu_sync"] = True
    with pytest.raises(V2Error) as exc:
        await e.service.enable(e.instance, "c2c", plan["fingerprint"], confirm=False)
    assert exc.value.code == "confirmation_required" and not e.calls
    result = await e.service.enable(e.instance, "c2c", plan["fingerprint"], confirm=True)
    assert result["state"] == "synced"
    writes = [(method, path, body) for method, path, body in e.calls if method != "GET"]
    assert len(writes) == 1 and writes[0][:2] == ("POST", "/v2/panels")
    assert writes[0][2]["scope"] == "c2c" and writes[0][2]["target_type"] == "all"
    e.handlers[0].desc = "updated"
    await e.service.sync(e.instance, "c2c")
    assert e.calls[-1][0:2] == ("PUT", "/v2/panels/" + result["panel_id"])
    assert all(path != "/v2/menu" for _, path, _ in e.calls)


@pytest.fixture
def routed_panels(panel_env, monkeypatch):
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.platform.message_type import MessageType
    from astrbot.core.star.context import Context
    from astrbot.core.umop_config_router import UmopConfigRouter
    from test_messaging_state import NOW, chat_payload

    from v2.messaging.convert import convert_chat
    from v2.protocol import RawEnvelope

    e = panel_env
    default = {"wake_prefix": ["/"], "plugin_set": ["*"]}
    router = UmopConfigRouter(None)
    manager = AstrBotConfigManager(default, router, None)
    manager.abconf_data = {}
    manager.confs["unrelated"] = {"wake_prefix": ["?"], "plugin_set": []}
    context = SimpleNamespace(_config=default, astrbot_config_mgr=manager)
    lookups = []

    def get_config(umo=None):
        if umo is not None:
            lookups.append(umo)
        return Context.get_config(context, umo)

    context.get_config = get_config
    e.owner.context = context
    e.service.catalog_provider = e.service._catalog
    monkeypatch.setattr("v2.panels.collect_catalog", lambda config, scene, **kwargs: collect_catalog(
        config, scene, handlers=e.handlers, plugins=e.plugins,
    ))

    def bind(scene, target, profile):
        event = "GROUP_AT_MESSAGE_CREATE" if scene == "group" else "C2C_MESSAGE_CREATE"
        profile_id = "profile-" + str(len(manager.confs))
        payload = chat_payload(event, message_id=profile_id, target=target,
                               sender=target if scene == "c2c" else "member-one")
        e.owner.messages.observe(convert_chat(e.instance.identity, RawEnvelope(payload, NOW)))
        message_type = MessageType.GROUP_MESSAGE if scene == "group" else MessageType.FRIEND_MESSAGE
        umo = f"{e.instance.identity.platform_id}:{message_type.value}:{target}"
        manager.confs[profile_id] = copy.deepcopy(profile)
        manager.abconf_data[profile_id] = {"name": profile_id, "path": profile_id + ".json"}
        router.umop_to_conf_id[umo] = profile_id
        return umo

    e.bind = bind
    e.lookups = lookups
    return e


@pytest.mark.parametrize("scene", ["group", "c2c"])
async def test_routed_panel_target_prefix_plugin_set_and_conflict_checks(routed_panels, scene):
    from v2 import PLUGIN_NAME
    e = routed_panels
    target = "target/one:openid"
    umo = e.bind(scene, target, {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME], "disable_builtin_commands": True})
    plan = e.service.plan(e.instance, scene, target_type="specific", targets=[target])
    assert plan["issues"] == []
    assert [item["name"] for item in plan["payload"]["panel"]["items"]] == ["!v2menu"]
    assert e.lookups == [umo] and not e.calls

    all_plan = e.service.plan(e.instance, scene)
    assert "scope_config_conflict" in all_plan["issues"]

    e.lookups.clear()
    targets = ["target-a", "target-b"]
    profile = {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME]}
    umos = [e.bind(scene, t_id, profile) for t_id in targets]
    shared_plan = e.service.plan(e.instance, scene, target_type="specific", targets=targets)
    assert shared_plan["issues"] == [] and e.lookups == umos
    e.owner.config["remote_menu_sync"] = True
    result = await e.service.enable(e.instance, scene, shared_plan["fingerprint"], confirm=True,
                                    target_type="specific", targets=targets)
    record = e.records[result["panel_id"]]
    assert record["scope"] == scene and record["target_type"] == "specific"
    assert record["group_openids" if scene == "group" else "user_openids"] == targets

    for difference in ("prefix", "plugin_set"):
        base_prof = {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME, "data.plugins.sample.main"]}
        other = copy.deepcopy(base_prof)
        other["wake_prefix" if difference == "prefix" else "plugin_set"] = ["#"] if difference == "prefix" else [PLUGIN_NAME]
        e.bind(scene, f"conf-a-{difference}", base_prof)
        e.bind(scene, f"conf-b-{difference}", other)
        cat = collect_catalog(base_prof, scene, handlers=e.handlers, plugins=e.plugins)
        shortcut = next(node for node in cat["nodes"] if node["name"] == "plugin")
        key = e.instance.identity.settings_key
        cur_rev = e.settings.get(key)["revision"]
        draft = e.settings.mutate(key, cur_rev, "fixture", operation="save",
                                  patch={"panels": {scene: {"mode": "custom", "selected": [shortcut["id"]]}}})
        bindings = e.service.capture_bindings(e.instance, draft["draft"], confirm=True)
        e.settings.mutate(key, draft["revision"], "fixture", operation="apply", bindings=bindings)
        c_plan = e.service.plan(e.instance, scene, target_type="specific", targets=[f"conf-a-{difference}", f"conf-b-{difference}"])
        assert "scope_config_conflict" in c_plan["issues"]


    from v2.panels import routed_configs
    mgr = e.owner.context.astrbot_config_mgr
    mgr.confs["default"]["plugin_set"] = []
    mgr.confs["default"]["disable_builtin_commands"] = True
    mgr.confs["routed-plat"] = {"wake_prefix": ["#"], "plugin_set": ["*"], "disable_builtin_commands": False}
    mgr.abconf_data["routed-plat"] = {"name": "routed-plat", "path": "routed-plat.json"}
    mgr.ucr.umop_to_conf_id = {f"{e.instance.identity.platform_id}:*:*": "routed-plat", "other-plat:*:*": "unrelated"}
    resolved = routed_configs(e.owner.context, e.instance.identity, scene)
    assert resolved == [mgr.confs["routed-plat"]]
