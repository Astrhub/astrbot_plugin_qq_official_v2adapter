"""Upgrade and partial-shard behavior through the real host adapter lifecycle."""
import asyncio
import copy
import importlib
import time
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.manager import PlatformManager
from astrbot.core.platform.register import platform_registry
from test_gateway_shards import group_env as group_env
from test_lifecycle import context, plugin_module as plugin_module
from test_messaging_state import chat_payload
from test_onboarding import HostConfig


@pytest.mark.parametrize("environment,transport,shard", [
    ("production", "websocket", [0, 1]),
    ("production", "websocket", [1, 3]),
    ("sandbox", "webhook", [0, 1]),
])
async def test_legacy_host_form_merge_preserves_environment_and_manual_topology(
    plugin_module, config, tmp_path, environment, transport, shard
):
    legacy = {**config, "environment": environment, "transport": transport, "shard": shard}
    legacy.pop("is_sandbox", None)
    legacy.pop("shard_mode", None)
    host = HostConfig(tmp_path / "host.json", legacy)
    ctx = context()
    ctx.get_config = lambda *args: host
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        saved = host["platform"][0]
        metadata = next(item for item in platform_registry if item.name == saved["type"])
        # The host editor fills missing keys from its current template before saving.
        merged = {**copy.deepcopy(metadata.default_config_tmpl), **copy.deepcopy(saved)}
        models = importlib.import_module(plugin_module.__package__ + ".v2.models")
        identity = models.InstanceKey.from_config(merged)
        assert identity.shard_mode == "manual"
        assert identity.shard == tuple(shard)
        assert identity.robot.environment == environment
        assert identity.transport == transport
        merged["is_sandbox"] = environment != "sandbox"
        switched = models.InstanceKey.from_config(merged)
        assert switched.robot.environment == ("production" if environment == "sandbox" else "sandbox")
        assert switched.settings_key != identity.settings_key
        assert saved["id"] == config["id"] and saved["appid"] == config["appid"]
    finally:
        await owner.terminate()


async def test_healthy_shard_keeps_delivering_and_replying_during_peer_recovery(
    group_env, plugin_module, tmp_path, monkeypatch
):
    e = group_env
    e.suggestion[0] = 2
    cfg = {**e.config, "shard_mode": "auto"}
    host = HostConfig(tmp_path / "host.json", cfg)
    host["platform_settings"] = {}
    host.save_config()
    from astrbot.core.utils.metrics import Metric
    async def no_metrics(**kwargs):
        pass
    monkeypatch.setattr(Metric, "upload", no_metrics)
    ctx, queue = context(), asyncio.Queue()
    ctx.get_config = lambda *args: host
    ctx.platform_manager = PlatformManager(host, queue)
    http_module = importlib.import_module(plugin_module.__package__ + ".v2.transport.http")
    ws_module = importlib.import_module(plugin_module.__package__ + ".v2.transport.websocket")
    monkeypatch.setattr(http_module.HTTPTransport, "_make_session", lambda self: e.http._factory())
    original = ws_module.Gateway.__init__
    recovering, release = asyncio.Event(), asyncio.Event()

    async def blocked_backoff(delay):
        if delay < 15:
            recovering.set()
            await release.wait()
        else:
            await asyncio.Event().wait()

    def gateway_init(self, *args, **kwargs):
        original(self, *args, **kwargs, sleep=blocked_backoff, jitter=lambda: 0)

    monkeypatch.setattr(ws_module.Gateway, "__init__", gateway_init)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        await ctx.platform_manager.reload(host["platform"][0])
        instance = next(iter(owner.instances))
        for _ in range(2):
            await asyncio.wait_for(e.handshakes.get(), 2)
            await asyncio.wait_for(e.beats.get(), 2)
        await e.sockets[1].close(code=4009)
        await asyncio.wait_for(recovering.wait(), 2)
        assert not instance.runtime_status()["online"]
        assert instance.runtime_status()["state"] == "degraded"
        await e.sockets[0].send_json(chat_payload(
            message_id="healthy-peer-during-recovery", timestamp=time.time(), text="hello"
        ))
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.message_obj.message_id == "healthy-peer-during-recovery"
        instance.sender.connected(SimpleNamespace(scene="group"))
        instance.sender.connected(SimpleNamespace(scene="channel"))
        event.delivery_finished()
        group_event = event
        assert instance.runtime_status()["message_ready"] and instance.runtime_status()["ws_available"]
        await group_event.send(MessageChain([Plain("healthy group reply")]))
        await e.sockets[0].send_json(chat_payload("AT_MESSAGE_CREATE", target="channel-one",
            message_id="healthy-channel-during-recovery", timestamp=time.time(), text="hello"))
        channel_event = await asyncio.wait_for(queue.get(), 1)
        await channel_event.send(MessageChain([Plain("healthy channel reply")]))
        channel_event.delivery_finished()
        assert e.messages == [
            ("/v2/groups/group-one/messages", {"content": "healthy group reply", "msg_type": 0, "msg_id": "healthy-peer-during-recovery", "msg_seq": 1}),
            ("/channels/channel-one/messages", {"content": "healthy channel reply", "msg_id": "healthy-channel-during-recovery"}),
        ]
        healthy = instance.gateway.gateways[0]
        previous_clock = healthy.clock
        healthy.clock = lambda: healthy.last_ack + 2 * healthy.interval + 1
        assert not instance.runtime_status()["ws_available"] and not instance.runtime_status()["message_ready"]
        with pytest.raises(RuntimeError) as exc:
            await channel_event.send(MessageChain([Plain("expired heartbeat")]))
        assert exc.value.code == "channel_ws_required" and len(e.messages) == 2
        healthy.clock = previous_clock
        assert instance.runtime_status()["message_ready"] and not instance.runtime_status()["online"]
        recovering.clear()
        await e.sockets[0].close(code=4009)
        await asyncio.wait_for(recovering.wait(), 2)
        assert not instance.runtime_status()["ws_available"] and not instance.runtime_status()["message_ready"]
        for old_event, code in ((group_event, "transport_not_ready"), (channel_event, "channel_ws_required")):
            with pytest.raises(RuntimeError) as exc:
                await old_event.send(MessageChain([Plain("all shards offline")]))
            assert exc.value.code == code
        assert len(e.messages) == 2
        release.set()
        for _ in range(2):
            assert (await asyncio.wait_for(e.handshakes.get(), 2))["op"] == 6
            await asyncio.wait_for(e.beats.get(), 2)
        assert instance.runtime_status()["online"] and instance.runtime_status()["state"] == "online"
        assert instance.runtime_status()["ws_available"] and instance.runtime_status()["message_ready"]
        host["platform"][0]["intents"] ^= 1
        host.save_config()
        assert not instance.runtime_status()["ws_available"] and not instance.runtime_status()["message_ready"]
        with pytest.raises(RuntimeError) as exc:
            await group_event.send(MessageChain([Plain("revoked generation")]))
        assert exc.value.code == "stale_generation" and len(e.messages) == 2
    finally:
        release.set()
        await owner.terminate()
    assert not instance.runtime_status()["ws_available"] and not instance.runtime_status()["message_ready"]


async def test_initialization_normalizes_one_batch_preserves_foreign_and_is_idempotent(plugin_module, config, tmp_path):
    foreign = {**config, "type": "qq_official", "id": "native", "environment": "untouched", "logo_token": "native-logo", "secret": "native-fixture"}
    legacy = {**config, "id": "legacy-manual", "environment": "production", "transport": "websocket",
              "shard": [1, 3], "logo_token": "old-display-token"}
    hook = {**config, "type": "qq_official_v2", "id": "legacy-hook", "appid": "hook-app", "environment": "sandbox",
            "transport": "webhook", "unified_webhook_mode": True, "webhook_uuid": "4c4eb5e4c98340118deec38fed41bd75"}
    host = HostConfig(tmp_path / "host.json", legacy)
    host["platform"].extend([hook, foreign])
    host.save_config()
    saves = host.saves
    ctx = context()
    ctx.get_config = lambda *args: host
    first = plugin_module.QQOfficialV2(ctx, {})
    await first.initialize()
    try:
        assert host.saves == saves + 1 and not first.instances
        ws, wh, other = host["platform"]
        assert other is foreign and other == {**config, "type": "qq_official", "id": "native", "environment": "untouched", "logo_token": "native-logo", "secret": "native-fixture"}
        assert ws["type"] == "qq_official_v2" and ws["is_sandbox"] is False and ws["shard_mode"] == "manual" and ws["shard"] == [1, 3]
        assert wh["type"] == "qq_official_v2_webhook" and wh["is_sandbox"] is True and wh["shard_mode"] == "manual"
        assert wh["webhook_uuid"] == hook["webhook_uuid"]
        models = importlib.import_module(plugin_module.__package__ + ".v2.models")
        assert json.loads(models.InstanceKey.from_config(ws).settings_key) == ["legacy-manual", config["appid"], "production"]
        assert json.loads(models.InstanceKey.from_config(wh).settings_key) == ["legacy-hook", "hook-app", "sandbox"]
        for value, old in ((ws, legacy), (wh, hook)):
            assert all(value[k] == old[k] for k in ("id", "appid", "secret", "intents", "shard", "enable"))
            assert not ({"environment", "transport", "unified_webhook_mode", "logo_token"} & value.keys())
        assert json.loads(Path(host.config_path).read_text()) == dict(host)
        frozen = Path(host.config_path).read_bytes()
        first.connections.prepare_webhooks()
        assert host.saves == saves + 1 and Path(host.config_path).read_bytes() == frozen
    finally:
        await first.terminate()
    second = plugin_module.QQOfficialV2(ctx, {})
    await second.initialize()
    try:
        assert host.saves == saves + 1 and Path(host.config_path).read_bytes() == frozen
        assert not second.instances
    finally:
        await second.terminate()


@pytest.mark.parametrize("failure", ["save", "environment", "transport", "shard", "uuid"])
async def test_initialization_batch_failure_restores_all_records_without_partial_writes(plugin_module, config, tmp_path, failure):
    host = HostConfig(tmp_path / "host.json", config)
    bad = {**config, "id": "second", "transport": "webhook"}
    if failure == "environment":
        bad.update(is_sandbox=False, environment="sandbox")
    elif failure == "transport":
        bad.update(type="qq_official_v2_webhook", transport="websocket")
    elif failure == "shard":
        bad.update(shard_mode="auto", shard=[1, 3])
    elif failure == "uuid":
        bad["webhook_uuid"] = "invalid-existing-uuid"
    host["platform"].append(bad)
    host.save_config()
    host.fail = failure == "save"
    before, records = copy.deepcopy(dict(host)), list(host["platform"])
    disk, saves = Path(host.config_path).read_bytes(), host.saves
    ctx = context()
    ctx.get_config = lambda *args: host
    owner = plugin_module.QQOfficialV2(ctx, {})
    with pytest.raises(RuntimeError) as exc:
        await owner.initialize()
    assert exc.value.code == {"save": "config_save_failed", "environment": "config_conflict",
        "transport": "config_conflict", "shard": "invalid_shard", "uuid": "invalid_webhook_uuid"}[failure]
    assert dict(host) == before and Path(host.config_path).read_bytes() == disk and host.saves == saves
    assert all(a is b for a, b in zip(host["platform"], records))
    assert not owner.instances and owner.messages.closed and owner.inbox.closed
    assert not ctx.registered_web_apis
