"""One authority for network secrets plus layered 1/2/10/50/100 instance checks."""
import asyncio
import copy
import json
import threading
from contextlib import AsyncExitStack
from pathlib import Path

import aiohttp
import pytest
from test_lifecycle import context
from test_lifecycle import plugin_module as plugin_module
from test_onebot_network import TOKEN, free_port
from test_onboarding import owner as owner

from v2.errors import V2Error
from v2.network_config import DEFAULT_NETWORK, network_config


@pytest.mark.parametrize("patch", [
    {"enable": 1}, {"writes": "false"}, {"host": "localhost"}, {"host": "https://127.0.0.1"},
    {"host": "::1%lo"}, {"port": True}, {"port": 0}, {"port": 65536}, {"port": "5700"},
    {"enable": True}, {"token": "short"}, {"token": "*" * 32}, {"token": "[REDACTED]"},
    {"token": "a" * 16 + "\n"}, {"token": "中" * 16}, {"token": "x" * 513}, {"self_id": "foreign"},
])
def test_invalid_network_settings_fail_closed(patch):
    with pytest.raises(V2Error):
        network_config(patch)


def test_network_defaults_and_explicit_remote_address():
    assert network_config() == DEFAULT_NETWORK
    assert not DEFAULT_NETWORK["enable"] and not DEFAULT_NETWORK["writes"]
    assert network_config({"host": "0.0.0.0"})["host"] == "0.0.0.0"
    assert network_config({"host": "::1"})["host"] == "::1"


async def test_network_config_single_authority_mask_keep_replace_clear_and_conflict(owner, config):
    conn = owner.connections
    def view():
        value = conn.view(config["id"])
        assert TOKEN not in json.dumps(value) and config["secret"] not in json.dumps(value)
        return value
    original = copy.deepcopy(owner.context.get_config())
    initial = view()
    assert "onebot" not in owner.context.get_config()["platform"][0] and not initial["network_token_configured"]
    with pytest.raises(V2Error):
        await conn.save(config["id"], initial["fingerprint"], {"onebot": {"enable": True}}, confirm=True)
    assert owner.context.get_config() == original
    with pytest.raises(V2Error):
        await conn.save(config["id"], initial["fingerprint"], {"onebot": {"token": TOKEN}}, confirm=True)
    with pytest.raises(V2Error) as exc:
        await conn.save(config["id"], initial["fingerprint"], {"onebot": {"enable": True, "writes": True}}, confirm=True,
                        network_token_action="replace", network_token=TOKEN, confirm_network_token=True)
    assert exc.value.code == "network_write_confirmation_required"
    saved = await conn.save(config["id"], initial["fingerprint"], {"onebot": {"enable": True, "writes": True, "port": 5788}}, confirm=True,
                            network_token_action="replace", network_token=TOKEN, confirm_network_token=True, confirm_network_writes=True)
    assert saved == view() and saved["network_token_configured"] and not owner.context.platform_manager.calls
    disk = json.loads(Path(owner.context.get_config().config_path).read_text())
    assert disk["platform"][0]["onebot"]["token"] == TOKEN and disk["platform"][0]["secret"] == config["secret"]
    with pytest.raises(V2Error) as exc:
        await conn.save(config["id"], initial["fingerprint"], {}, confirm=True)
    assert exc.value.code == "config_conflict"
    retained = await conn.save(config["id"], saved["fingerprint"], {"onebot": {"writes": False}}, confirm=True)
    assert owner.context.get_config()["platform"][0]["onebot"]["token"] == TOKEN and not retained["fields"]["onebot"]["writes"]
    with pytest.raises(V2Error):
        await conn.save(config["id"], retained["fingerprint"], {}, confirm=True, network_token_action="clear", confirm_network_token=True)
    cleared = await conn.save(config["id"], retained["fingerprint"], {"onebot": {"enable": False}}, confirm=True,
                              network_token_action="clear", confirm_network_token=True)
    assert not cleared["network_token_configured"] and owner.context.get_config()["platform"][0]["onebot"]["token"] == ""


@pytest.mark.parametrize("options", [
    {"network_token": TOKEN}, {"network_token_action": "replace", "network_token": TOKEN},
    {"network_token_action": "clear"}, {"network_token_action": "unknown"},
    {"network_token_action": "replace", "network_token": "", "confirm_network_token": True},
])
async def test_network_secret_operations_require_explicit_valid_confirmation(owner, config, options):
    before = copy.deepcopy(owner.context.get_config())
    with pytest.raises(V2Error):
        await owner.connections.save(config["id"], owner.connections.view(config["id"])["fingerprint"], {}, confirm=True, **options)
    assert owner.context.get_config() == before


async def test_network_token_cannot_reuse_qq_secret_or_be_rolled_back_silently(owner, config):
    conn = owner.connections
    fingerprint = conn.view(config["id"])["fingerprint"]
    with pytest.raises(V2Error) as exc:
        await conn.save(config["id"], fingerprint, {}, confirm=True, network_token_action="replace", network_token=config["secret"], confirm_network_token=True)
    assert exc.value.code == "invalid_network_token"
    before = copy.deepcopy(owner.context.get_config())
    owner.context.get_config().fail = True
    with pytest.raises(V2Error) as exc:
        await conn.save(config["id"], fingerprint, {}, confirm=True, network_token_action="replace", network_token=TOKEN, confirm_network_token=True)
    assert exc.value.code == "config_save_failed" and owner.context.get_config() == before


@pytest.mark.parametrize("count", [1, 2, 10, 50, 100])
async def test_layered_multi_instance_state_and_loopback_cleanup(plugin_module, config, monkeypatch, tmp_path, count):
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path)
    ctx = context()
    configs = [{**config, "id": f"multi-{i}", "appid": f"synthetic-app-{i}", "environment": "production" if i % 2 == 0 else "sandbox",
                "onebot": {"enable": i < 10, "port": free_port() if i < 10 else 5700, "token": TOKEN + str(i)}} for i in range(count)]
    # Retain the distinct allocated ports before starting; avoid a test free-port collision.
    for i, cfg in enumerate(configs[:10]):
        while cfg["onebot"]["port"] in {c["onebot"]["port"] for c in configs[:i]}:
            cfg["onebot"]["port"] = free_port()
    ctx.get_config()["platform"] = copy.deepcopy(configs)
    owner = plugin_module.QQOfficialV2(ctx, {"onebot_network_enabled": True})
    await owner.initialize()
    baseline = set(asyncio.all_tasks())
    descriptors = len(list(Path("/proc/self/fd").iterdir()))
    threads = threading.active_count()
    instances = []
    try:
        for cfg in configs:
            instances.append(owner.adapter_class(cfg, {}, asyncio.Queue()))
        assert len(owner.instances) == count and len({i.identity.generation for i in instances}) == count
        assert len({i.identity.robot for i in instances}) == count
        for instance in instances:
            assert instance.network.runner is None and instance.http.session is None and instance.ack_http.session is None
            await instance.network.start()
        active = instances[:min(count, 10)]
        assert sum(i.network.listening for i in instances) == len(active)
        async with aiohttp.ClientSession() as http, AsyncExitStack() as stack:
            for index, instance in enumerate(active):
                base = f"http://127.0.0.1:{instance.network.config['port']}"
                headers = {"Authorization": "Bearer " + TOKEN + str(index)}
                async with http.get(base + "/get_status", headers=headers) as r:
                    value = (await r.json())["data"]
                    assert value["platform_id"] == instance.identity.platform_id and not value["online"]
                ws = await stack.enter_async_context(http.ws_connect(base + "/api", headers=headers))
                await ws.send_json({"action": "get_version_info", "echo": index})
                assert (await ws.receive_json(timeout=2))["echo"] == index
                if index:
                    async with http.get(base + "/get_status", headers={"Authorization": "Bearer " + TOKEN + "0"}) as r:
                        assert r.status == 403
            assert all(i.http.session is None and not i.gateway.online and i.ingress.worker is None and i.consumer.task is None for i in instances)
        for instance in instances:
            await instance.terminate()
        assert not owner.instances and all(not i.network.requests and not i.network.peers and not i.network.cleanups and not i.network.queued_bytes for i in instances)
        assert not (set(asyncio.all_tasks()) - baseline)
        assert len(list(Path("/proc/self/fd").iterdir())) <= descriptors and threading.active_count() == threads
        print(f"P5_MULTI count={count} constructed={count} loopback_listeners={len(active)} qq_connections=0 leftover_tasks=0 leftover_listeners=0 leftover_subscriptions=0")
    finally:
        await owner.terminate()


async def test_ten_network_generation_reloads_return_to_resource_baseline(plugin_module, config, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path)
    ctx = context()
    cfg = {**config, "onebot": {"enable": True, "port": free_port(), "token": TOKEN}}
    ctx.get_config()["platform"] = [cfg]
    owner = plugin_module.QQOfficialV2(ctx, {"onebot_network_enabled": True})
    await owner.initialize()
    baseline = set(asyncio.all_tasks())
    descriptors = len(list(Path("/proc/self/fd").iterdir()))
    generations = set()
    try:
        for _ in range(10):
            instance = owner.adapter_class(cfg, {}, asyncio.Queue())
            generations.add(instance.identity.generation)
            await instance.network.start()
            async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as http:
                async with http.ws_connect(f"http://127.0.0.1:{cfg['onebot']['port']}/event") as ws:
                    assert (await ws.receive_json(timeout=2))["_qq"]["generation"] == instance.identity.generation
                    await instance.terminate()
            assert not owner.instances and not (set(asyncio.all_tasks()) - baseline)
            assert len(list(Path("/proc/self/fd").iterdir())) <= descriptors
        assert len(generations) == 10
        print("P5_RELOAD iterations=10 generations=10 leftover_tasks=0 fd_growth=0 qq_connections=0")
    finally:
        await owner.terminate()


async def test_saved_network_configuration_and_unknown_ledger_survive_owner_restart(plugin_module, config, monkeypatch, tmp_path):
    from test_onboarding import HostConfig
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path / "plugin-data")
    disk_path = tmp_path / "host.json"
    saved = HostConfig(disk_path, config)
    ctx = context()
    ctx.get_config = lambda: saved
    owner = plugin_module.QQOfficialV2(ctx, {"onebot_network_enabled": True})
    await owner.initialize()
    try:
        before = owner.connections.view(config["id"])
        await owner.connections.save(config["id"], before["fingerprint"], {"onebot": {"enable": True, "port": free_port()}},
            confirm=True, network_token_action="replace", network_token=TOKEN, confirm_network_token=True)
        assert not owner.instances  # Saving still did not start a receiver or listener.
        instance = owner.adapter_class(saved["platform"][0], {}, asyncio.Queue())
        old_generation = instance.identity.generation
        owner.extension_state.begin(instance.identity.robot, "keep-network-unknown", "group_ban", "synthetic-binding")
        owner.extension_state.attempt(instance.identity.robot, "keep-network-unknown")
        owner.extension_state.finish(instance.identity.robot, "keep-network-unknown", "unknown", error={"code": "synthetic_result_unknown"})
    finally:
        await owner.terminate()
    restored = json.loads(disk_path.read_text())
    fresh_context = context()
    fresh_context.get_config = lambda: restored
    replacement = plugin_module.QQOfficialV2(fresh_context, {"onebot_network_enabled": True})
    await replacement.initialize()
    try:
        instance = replacement.adapter_class(restored["platform"][0], {}, asyncio.Queue())
        assert instance.identity.generation != old_generation and not instance.network.config["writes"]
        await instance.network.start()
        async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as http:
            async with http.get(f"http://127.0.0.1:{instance.network.config['port']}/_qq_get_extension_status", params={"operation_id": "keep-network-unknown"}) as response:
                assert response.status == 200
                result = (await response.json())["data"]
                assert result["state"] == "unknown" and result["error"]["code"] == "synthetic_result_unknown"
        assert instance.http.session is None and not instance.gateway.online
        print("P5_RESTART persisted_config=restored token=retained generation=changed unknown_ledger=retained qq_connections=0")
    finally:
        await replacement.terminate()
