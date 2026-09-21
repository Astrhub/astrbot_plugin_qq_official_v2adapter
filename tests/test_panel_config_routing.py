"""Panel targets resolve through the real host context, config manager and UMO router."""
import copy
from types import SimpleNamespace

import pytest
from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.context import Context
from astrbot.core.umop_config_router import UmopConfigRouter
from test_messaging_help_panels import panel_env as panel_env
from test_messaging_state import NOW, chat_payload

from v2 import PLUGIN_NAME
from v2.commands import collect_catalog
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.models import SessionRoute
from v2.protocol import RawEnvelope


@pytest.fixture
def routed_panels(panel_env, monkeypatch):
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
        route = SessionRoute(e.instance.identity.robot, scene, target)
        message_type = MessageType.GROUP_MESSAGE if scene == "group" else MessageType.FRIEND_MESSAGE
        umo = str(MessageSession(e.instance.identity.platform_id, message_type, route.encode()))
        manager.confs[profile_id] = copy.deepcopy(profile)
        manager.abconf_data[profile_id] = {"name": profile_id, "path": profile_id + ".json"}
        router.umop_to_conf_id[umo] = profile_id
        return umo

    e.bind = bind
    e.lookups = lookups
    return e


@pytest.mark.parametrize("scene", ["group", "c2c"])
async def test_specific_target_uses_effective_prefix_and_plugin_set(routed_panels, scene):
    e = routed_panels
    target = "target/one:openid"
    umo = e.bind(scene, target, {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME], "disable_builtin_commands": True})
    plan = e.service.plan(e.instance, scene, target_type="specific", targets=[target])
    assert plan["issues"] == []
    assert [item["name"] for item in plan["payload"]["panel"]["items"]] == ["!v2menu"]
    assert e.lookups == [umo] and not e.calls


@pytest.mark.parametrize("scene", ["group", "c2c"])
async def test_specific_targets_ignore_unrelated_profiles_and_publish_shared_items(routed_panels, scene):
    e = routed_panels
    targets = ["target-a", "target-b"]
    profile = {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME]}
    umos = [e.bind(scene, target, profile) for target in targets]
    plan = e.service.plan(e.instance, scene, target_type="specific", targets=targets)
    assert plan["issues"] == [] and e.lookups == umos and not e.calls
    assert all(item["name"].startswith("!") for item in plan["payload"]["panel"]["items"])
    e.owner.config["remote_menu_sync"] = True
    result = await e.service.enable(e.instance, scene, plan["fingerprint"], confirm=True,
                                    target_type="specific", targets=targets)
    record = e.records[result["panel_id"]]
    assert record["scope"] == scene and record["target_type"] == "specific"
    assert record["group_openids" if scene == "group" else "user_openids"] == targets
    assert record["panel"]["items"] == plan["payload"]["panel"]["items"]
    assert sum(method == "POST" for method, _, _ in e.calls) == 1


@pytest.mark.parametrize("scene", ["group", "c2c"])
@pytest.mark.parametrize("difference", ["prefix", "plugin_set"])
async def test_specific_target_conflicts_block_writes(routed_panels, scene, difference):
    e = routed_panels
    profile = {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME, "data.plugins.sample.main"]}
    other = copy.deepcopy(profile)
    other["wake_prefix" if difference == "prefix" else "plugin_set"] = ["#"] if difference == "prefix" else [PLUGIN_NAME]
    e.bind(scene, "target-a", profile)
    e.bind(scene, "target-b", other)
    catalog = collect_catalog(profile, scene, handlers=e.handlers, plugins=e.plugins)
    shortcut = next(node for node in catalog["nodes"] if node["name"] == "plugin")
    key = e.instance.identity.settings_key
    draft = e.settings.mutate(key, 0, "fixture", operation="save",
                              patch={"panels": {scene: {"mode": "custom", "selected": [shortcut["id"]]}}})
    bindings = e.service.capture_bindings(e.instance, draft["draft"], confirm=True)
    e.settings.mutate(key, draft["revision"], "fixture", operation="apply", bindings=bindings)
    plan = e.service.plan(e.instance, scene, target_type="specific", targets=["target-a", "target-b"])
    assert "scope_config_conflict" in plan["issues"]
    e.owner.config["remote_menu_sync"] = True
    with pytest.raises(V2Error) as exc:
        await e.service.enable(e.instance, scene, plan["fingerprint"], confirm=True,
                               target_type="specific", targets=["target-a", "target-b"])
    assert exc.value.code == "panel_invalid" and not e.calls


@pytest.mark.parametrize("scene", ["group", "c2c"])
async def test_all_targets_keep_conservative_profile_conflict_check(routed_panels, scene):
    e = routed_panels
    e.bind(scene, "target-a", {"wake_prefix": ["!"], "plugin_set": [PLUGIN_NAME]})
    plan = e.service.plan(e.instance, scene)
    assert "scope_config_conflict" in plan["issues"]
    assert not e.lookups and not e.calls
