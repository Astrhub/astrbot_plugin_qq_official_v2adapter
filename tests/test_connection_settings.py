"""Dual owned adapters preserve host form, configuration and routing contracts."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.core.platform.register import platform_cls_map, platform_registry, register_platform_adapter, unregister_platform_adapters_by_module
from astrbot.dashboard.services.config_service import ConfigDisplayService
from test_lifecycle import context, plugin_module as plugin_module
from test_onboarding import HostConfig, owner as owner

from v2 import PLATFORM_TYPE, PLATFORM_TYPES, WEBHOOK_TYPE
from v2.connection_config import normalize_connection
from v2.errors import V2Error
from v2.models import InstanceKey


@pytest.mark.parametrize("fields,transport,environment,mode", [
    ({}, "websocket", "production", "auto"),
    ({"type": WEBHOOK_TYPE}, "webhook", "production", "auto"),
    ({"environment": "sandbox", "transport": "webhook", "shard": [0, 1]}, "webhook", "sandbox", "manual"),
    ({"shard": [1, 2]}, "websocket", "production", "manual"),
    ({"is_sandbox": True, "shard_mode": "auto"}, "websocket", "sandbox", "auto"),
])
def test_legacy_and_new_configuration_keep_identity_and_topology(fields, transport, environment, mode):
    config = {"id": "官机", "appid": "app", **fields}
    snapshot = copy.deepcopy(config)
    identity = InstanceKey.from_config(config)
    assert (identity.transport, identity.robot.environment, identity.shard_mode) == (transport, environment, mode)
    assert json.loads(identity.settings_key) == ["官机", "app", environment]
    assert config == snapshot


@pytest.mark.parametrize("fields", [
    {"environment": "sandbox", "is_sandbox": False}, {"is_sandbox": 1},
    {"type": WEBHOOK_TYPE, "transport": "websocket"}, {"shard_mode": "invalid"},
    {"type": WEBHOOK_TYPE, "shard": [1, 2]}, {"shard_mode": "auto", "shard": [1, 2]},
    {"shard": [False, 1]}, {"intents": True}, {"intents": 2**32},
])
def test_conflicting_aliases_and_invalid_topology_reject_without_side_effects(fields):
    with pytest.raises(V2Error):
        normalize_connection(fields)


async def test_both_registered_forms_leave_shared_host_management_metadata_unchanged(plugin_module):
    service = ConfigDisplayService(SimpleNamespace(astrbot_config={}))
    before = (await service.get_astrbot_config())["metadata"]["platform_group"]["metadata"]["platform"]["items"]
    before = copy.deepcopy(before)
    owner = plugin_module.QQOfficialV2(context(), {})
    await owner.initialize()
    try:
        form = (await service.get_astrbot_config())["metadata"]["platform_group"]["metadata"]["platform"]
        for key in ("id", "enable", "unified_webhook_mode", "webhook_uuid", "is_sandbox"):
            assert form["items"].get(key) == before.get(key)
        for kind, title in [(PLATFORM_TYPE, "QQ 官方 V2（WebSocket）"), (WEBHOOK_TYPE, "QQ 官方 V2（Webhook）")]:
            meta = next(m for m in platform_registry if m.name == kind)
            template = form["config_template"][kind]
            visible = {k for k in template if not form["items"].get(k, {}).get("invisible")}
            assert visible == {"id", "enable", "appid", "secret", "is_sandbox"}
            assert meta.adapter_display_name == title and meta.logo_path == "assets/qq.png"
            assert template["is_sandbox"] is False and template["shard_mode"] == "auto"
            assert form["items"]["secret"]["secret"] is True
            assert "unified_webhook_mode" not in template and "transport" not in template and "environment" not in template
        for kind in PLATFORM_TYPES:
            event = SimpleNamespace(get_platform_name=lambda: kind, raw_data={"t": "GROUP_AT_MESSAGE_CREATE"})
            assert plugin_module.V2Only().filter(event, {}) and plugin_module.V2Addressed().filter(event, {})
        other = plugin_module.QQOfficialV2(context(), {})
        with pytest.raises(RuntimeError) as exc:
            await other.initialize()
        assert exc.value.code == "message_state_in_use"
        assert all(platform_cls_map[k] is owner.adapter_classes[k] for k in PLATFORM_TYPES)
    finally:
        await owner.terminate()
    assert not any(k in platform_cls_map for k in PLATFORM_TYPES)


async def test_second_registration_collision_rolls_back_only_owned_class(plugin_module):
    class Other:
        pass
    register_platform_adapter(WEBHOOK_TYPE, "another plugin")(Other)
    owner = plugin_module.QQOfficialV2(context(), {})
    try:
        with pytest.raises(ValueError):
            await owner.initialize()
        assert PLATFORM_TYPE not in platform_cls_map and platform_cls_map[WEBHOOK_TYPE] is Other
        assert owner.store.closed and owner.inbox.closed and owner.messages.closed
    finally:
        unregister_platform_adapters_by_module(Other.__module__)


async def test_legacy_webhook_save_is_explicit_canonicalization_without_ledger_rekey(owner, config):
    host = owner.context.get_config()
    host["platform"][0].update(transport="webhook", environment="sandbox", webhook_uuid="4c4eb5e4c98340118deec38fed41bd75", unified_webhook_mode=True, logo_token="not-status")
    host.save_config()
    original = copy.deepcopy(dict(host))
    prior = InstanceKey.from_config(host["platform"][0]).settings_key
    view = owner.connections.view(config["id"])
    assert view["fields"]["type"] == WEBHOOK_TYPE and view["fields"]["is_sandbox"] is True
    assert view["fields"]["shard_mode"] == "manual" and "logo_token" not in json.dumps(view)
    assert dict(host) == original
    saved = await owner.connections.save(config["id"], view["fingerprint"], {"intents": 7}, confirm=True)
    persisted = host["platform"][0]
    assert InstanceKey.from_config(persisted).settings_key == prior
    assert persisted["type"] == WEBHOOK_TYPE and persisted["is_sandbox"] is True
    assert persisted["secret"] == config["secret"] and persisted["webhook_uuid"] == original["platform"][0]["webhook_uuid"]
    assert not ({"environment", "transport", "logo_token", "unified_webhook_mode"} & persisted.keys())
    assert not owner.context.platform_manager.calls
    assert saved["runtime"]["state"] == "configured"


@pytest.mark.parametrize("first,second,allowed", [
    ({"shard": [0, 2]}, {"shard": [1, 2], "shard_mode": "manual"}, True),
    ({"shard": [0, 2]}, {"shard": [0, 2], "shard_mode": "manual"}, False),
    ({"shard_mode": "auto"}, {"shard": [1, 2], "shard_mode": "manual"}, False),
    ({"shard": [0, 2]}, {"shard_mode": "auto"}, False),
    ({"shard_mode": "auto"}, {"shard_mode": "auto"}, False),
    ({"type": WEBHOOK_TYPE}, {"shard_mode": "auto"}, False),
    ({"shard": [0, 2]}, {"shard": [1, 3], "shard_mode": "manual"}, False),
])
async def test_save_and_runtime_enforce_same_receiver_exclusion(plugin_module, config, tmp_path, first, second, allowed):
    cfg = {**config, **first}
    host = HostConfig(tmp_path / "host.json", cfg)
    ctx = context()
    ctx.get_config = lambda *args: host
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        one = owner.adapter_classes[host["platform"][0]["type"]](host["platform"][0], {}, asyncio.Queue())
        view = owner.connections.view("second")
        patch = {"appid": config["appid"], "enable": True, **second}
        kwargs = {"secret_action": "replace", "secret": config["secret"], "confirm_secret": True, "confirm": True}
        if allowed:
            await owner.connections.save("second", view["fingerprint"], patch, **kwargs)
        else:
            with pytest.raises(RuntimeError) as exc:
                await owner.connections.save("second", view["fingerprint"], patch, **kwargs)
            assert exc.value.code == "duplicate_receiver"
        candidate = {**config, "id": "second", **second}
        if not allowed:
            with pytest.raises(RuntimeError) as exc:
                owner.adapter_class(candidate, {}, asyncio.Queue())
            assert exc.value.code == "duplicate_receiver"
        else:
            two = owner.adapter_class(host["platform"][-1], {}, asyncio.Queue())
            assert one.gateway.budget is two.gateway.budget
            assert one.gateway.ingress.owner != two.gateway.ingress.owner
        assert all(i.http.session is None for i in owner.instances)
    finally:
        await owner.terminate()


async def test_new_webhook_first_load_persists_uuid_without_legacy_switch(plugin_module, config, tmp_path):
    host = HostConfig(tmp_path / "host.json", {**config, "type": WEBHOOK_TYPE})
    ctx = context()
    ctx.get_config = lambda *args: host
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        first = host["platform"][0]["webhook_uuid"]
        candidate = {**config, "id": "new-after-plugin-start", "appid": "other", "type": WEBHOOK_TYPE}
        host["platform"].append(candidate)
        host.save_config()
        instance = owner.adapter_classes[WEBHOOK_TYPE](candidate, {}, asyncio.Queue())
        assert instance.unified_webhook() and instance.meta().name == WEBHOOK_TYPE
        assert instance.config["webhook_uuid"] == candidate["webhook_uuid"] != first
        assert json.loads(Path(host.config_path).read_text())["platform"][-1]["webhook_uuid"] == candidate["webhook_uuid"]
        assert "unified_webhook_mode" not in candidate and instance.gateway is None
        from astrbot.dashboard.services.platform_service import PlatformService
        service = object.__new__(PlatformService)
        service.platform_manager = SimpleNamespace(platform_insts=[instance])
        assert service.find_platform_by_uuid(candidate["webhook_uuid"]) is instance
    finally:
        await owner.terminate()
