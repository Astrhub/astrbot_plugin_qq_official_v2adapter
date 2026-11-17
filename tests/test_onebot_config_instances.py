"""Leftover OneBot network config is ignored; no listener is started."""
import asyncio
import copy
import json
import socket
import threading
from pathlib import Path

import pytest
from test_lifecycle import context
from test_lifecycle import plugin_module as plugin_module
from test_onboarding import owner as owner


async def test_onebot_network_fields_are_ignored_and_stripped(owner, config):
    conn = owner.connections
    initial = conn.view(config["id"])
    assert "onebot" not in initial["fields"]
    assert "network_token_configured" not in initial
    assert "network_global_enabled" not in initial
    saved = await conn.save(config["id"], initial["fingerprint"],
                            {"onebot": {"enable": True, "port": 5700, "writes": True, "token": "x" * 16}},
                            confirm=True)
    assert "onebot" not in saved["fields"]
    disk = json.loads(Path(owner.context.get_config().config_path).read_text())
    assert "onebot" not in disk["platform"][0]
    with pytest.raises(TypeError):
        await conn.save(config["id"], saved["fingerprint"], {}, confirm=True, network_token_action="replace")


async def test_leftover_onebot_on_disk_is_stripped_on_save(owner, config):
    conn = owner.connections
    cfg = owner.context.get_config()
    cfg["platform"][0]["onebot"] = {"enable": True, "host": "127.0.0.1", "port": 18765,
                                    "token": "x" * 16, "writes": True}
    cfg.save_config()
    view = conn.view(config["id"])
    assert "onebot" not in view["fields"]
    saved = await conn.save(config["id"], view["fingerprint"], {"enable": False}, confirm=True)
    assert "onebot" not in saved["fields"]
    disk = json.loads(Path(cfg.config_path).read_text())
    assert "onebot" not in disk["platform"][0]


async def test_leftover_onebot_config_does_not_start_a_listener(plugin_module, config, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path)
    ctx = context()
    port = 18765
    cfg = {**config, "onebot": {"enable": True, "host": "127.0.0.1", "port": port, "token": "x" * 16, "writes": True}}
    ctx.get_config()["platform"] = [copy.deepcopy(cfg)]
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        instance = owner.adapter_class(cfg, {}, asyncio.Queue())
        assert not hasattr(instance, "network")
        assert "network" not in instance.runtime_status()
        assert instance.client.capabilities()["network_api"] == {"support": "removed", "state": "not_provided"}
        probe = socket.socket()
        probe.bind(("127.0.0.1", port))
        probe.close()
        await instance.terminate()
    finally:
        await owner.terminate()


@pytest.mark.parametrize("count", [1, 2, 10, 50])
async def test_layered_multi_instance_state_without_network_listener(plugin_module, config, monkeypatch, tmp_path, count):
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path)
    ctx = context()
    configs = [{**config, "id": f"multi-{i}", "appid": f"synthetic-app-{i}",
                "onebot": {"enable": True, "port": 18765, "token": "x" * 16}} for i in range(count)]
    ctx.get_config()["platform"] = copy.deepcopy(configs)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    baseline = set(asyncio.all_tasks())
    descriptors = len(list(Path("/proc/self/fd").iterdir()))
    threads = threading.active_count()
    instances = []
    try:
        for cfg in configs:
            instances.append(owner.adapter_class(cfg, {}, asyncio.Queue()))
        assert len(owner.instances) == count and len({i.identity.generation for i in instances}) == count
        assert all(not hasattr(i, "network") for i in instances)
        for instance in instances:
            await instance.terminate()
        assert not owner.instances
        assert not (set(asyncio.all_tasks()) - baseline)
        assert len(list(Path("/proc/self/fd").iterdir())) <= descriptors and threading.active_count() == threads
    finally:
        await owner.terminate()


async def test_unknown_ledger_survives_owner_restart_without_network(plugin_module, config, monkeypatch, tmp_path):
    from test_onboarding import HostConfig

    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path / "plugin-data")
    disk_path = tmp_path / "host.json"
    saved = HostConfig(disk_path, config)
    ctx = context()
    ctx.get_config = lambda: saved
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        instance = owner.adapter_class(saved["platform"][0], {}, asyncio.Queue())
        old_generation = instance.identity.generation
        owner.extension_state.begin(instance.identity.robot, "keep-network-unknown", "group_ban", "synthetic-binding")
        owner.extension_state.attempt(instance.identity.robot, "keep-network-unknown")
        owner.extension_state.finish(instance.identity.robot, "keep-network-unknown", "unknown",
                                     error={"code": "synthetic_result_unknown"})
        await instance.terminate()
    finally:
        await owner.terminate()
    restored = json.loads(disk_path.read_text())
    fresh_context = context()
    fresh_context.get_config = lambda: restored
    replacement = plugin_module.QQOfficialV2(fresh_context, {})
    await replacement.initialize()
    try:
        instance = replacement.adapter_class(restored["platform"][0], {}, asyncio.Queue())
        assert instance.identity.generation != old_generation
        assert not hasattr(instance, "network")
        result = await instance.client.call_action("_qq_get_extension_status", operation_id="keep-network-unknown")
        assert result["state"] == "unknown" and result["error"]["code"] == "synthetic_result_unknown"
    finally:
        await replacement.terminate()
