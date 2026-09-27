"""Retrying shards stay local; shared fatal failures still close the group."""
import asyncio
import importlib
import time

import pytest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.manager import PlatformManager
from astrbot.core.utils.metrics import Metric
from test_gateway_shards import group_env as group_env
from test_lifecycle import context
from test_lifecycle import plugin_module as plugin_module
from test_messaging_state import chat_payload
from test_onboarding import HostConfig

from v2.errors import V2Error
from v2.transport.websocket import Gateway, GatewayClosed


class FaultSocket:
    def __init__(self, session, gate, broken):
        self.session, self.gate, self.broken = session, gate, broken
        self.calls = 0

    async def ws_connect(self, *args, **kwargs):
        await self.gate.wait()
        self.calls += 1
        if self.broken():
            raise OSError("synthetic shard connect failure")
        return await self.session.ws_connect(*args, **kwargs)


async def test_retrying_shard_preserves_host_delivery_and_recovers_without_reload(group_env, plugin_module, tmp_path, monkeypatch):
    e = group_env
    e.suggestion[0] = 2
    host = HostConfig(tmp_path / "host.json", {**e.config, "shard_mode": "auto"})
    host["platform_settings"] = {}
    host.save_config()
    ctx, queue = context(), asyncio.Queue()
    ctx.get_config = lambda *args: host
    ctx.platform_manager = PlatformManager(host, queue)
    http_module = importlib.import_module(plugin_module.__package__ + ".v2.transport.http")
    ws_module = importlib.import_module(plugin_module.__package__ + ".v2.transport.websocket")
    monkeypatch.setattr(http_module.HTTPTransport, "_make_session", lambda self: e.http._factory())
    async def no_metrics(**kwargs):
        pass
    monkeypatch.setattr(Metric, "upload", no_metrics)
    gate, parked, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    broken, faults = [True], []
    async def fault_sleep(delay):
        if release.is_set():
            await asyncio.sleep(delay)
        elif delay >= 16:
            parked.set()
            await release.wait()
        else:
            await asyncio.sleep(0)
    original_init = ws_module.Gateway.__init__
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs, sleep=fault_sleep if kwargs["shard"][0] == 1 else asyncio.sleep, jitter=lambda: 0)
        if self.shard[0] == 1:
            socket = FaultSocket(self.ws_session, gate, lambda: broken[0])
            faults.append(socket)
            self.ws_session = socket
    monkeypatch.setattr(ws_module.Gateway, "__init__", init)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        await ctx.platform_manager.reload(host["platform"][0])
        instance = next(iter(owner.instances))
        assert (await asyncio.wait_for(e.handshakes.get(), 2))["d"]["shard"] == [0, 2]
        await asyncio.wait_for(e.beats.get(), 2)
        gate.set()
        await asyncio.wait_for(parked.wait(), 2)
        assert faults[0].calls == 5 and not instance._run_task.done()
        await e.sockets[0].send_json(chat_payload(message_id="healthy-while-peer-retrying", timestamp=time.time(), text="hello"))
        event = await asyncio.wait_for(queue.get(), 1)
        assert event.message_obj.message_id == "healthy-while-peer-retrying"
        await event.send(MessageChain([Plain("still receiving")]))
        event.cleanup_temporary_local_files()
        assert e.messages == [("/v2/groups/group-one/messages", {"content": "still receiving", "msg_type": 0,
            "msg_id": "healthy-while-peer-retrying", "msg_seq": 1})]
        assert instance.http.session.calls[-1][1] == "https://api.bot.qq.com/v2/groups/group-one/messages"
        status = instance.runtime_status()
        assert status["state"] == "degraded" and not status["online"]
        assert status["message_ready"] and status["ws_available"]
        group = status["gateway_group"]
        assert group["planned"] == 2 and group["connected"] == 1 and len(group["shards"]) == 2
        node = group["shards"][1]
        assert node["state"] == "backoff" and node["recovery"] is None
        assert node["failure"]["code"] == "gateway_io_failure"
        assert node["consecutive_failures"] == 5 and node["next_retry_at"] is not None
        assert not instance.gateway.session.closed and sum(not t.done() for t in instance.gateway.tasks) == 2
        broken[0] = False
        release.set()
        login = await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert login["op"] == 2 and login["d"]["shard"] == [1, 2]
        assert instance.runtime_status()["online"] and faults[0].calls == 6
        assert instance.gateway.budget.remaining == 93
    finally:
        gate.set()
        release.set()
        await owner.terminate()
    assert not owner.instances and not ctx.platform_manager.get_insts() and not owner.delivery_slots.events
    assert instance._run_task.done() and instance.gateway.session.closed and instance.ingress.worker.done()
    assert all(not g.tasks and g.ws is None for g in instance.gateway.gateways)


@pytest.mark.parametrize("peer_state", ["connecting", "backoff"])
async def test_retrying_shard_does_not_cancel_pending_peer(group_env, monkeypatch, peer_state):
    e = group_env
    e.suggestion[0] = 2
    peer_release, fail_gate, parked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    fail_gate.set()
    original_init, original_connect = Gateway.__init__, Gateway._connect
    faults = {}
    async def peer_sleep(delay):
        if delay <= 15:
            await peer_release.wait()
        else:
            await asyncio.Event().wait()
    async def fault_sleep(delay):
        if delay >= 16:
            parked.set()
            await asyncio.Event().wait()
        await asyncio.sleep(0)
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs, sleep=peer_sleep if kwargs["shard"][0] == 1 else fault_sleep, jitter=lambda: 0)
        index = self.shard[0]
        faults[index] = FaultSocket(self.ws_session, fail_gate,
            lambda: index == 0 or peer_state == "backoff" and faults[index].calls == 1)
        self.ws_session = faults[index]
    async def connect(self):
        if self.shard[0] == 1 and peer_state == "connecting":
            await peer_release.wait()
        await original_connect(self)
    monkeypatch.setattr(Gateway, "__init__", init)
    monkeypatch.setattr(Gateway, "_connect", connect)
    task = asyncio.create_task(e.group.run())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        assert faults[0].calls == 5 and not task.done() and e.group.state == "degraded"
        peer_release.set()
        login = await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert login["d"]["shard"] == [1, 2]
        assert not task.done() and e.group.state == "degraded" and not e.group.online and e.group.available
        assert e.group.status()["planned"] == 2 and faults[0].calls == 5
    finally:
        peer_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await e.group.close()
    assert e.group.session.closed and not e.group.tasks and all(g.ws is None and not g.tasks for g in e.group.gateways)


async def test_all_shards_keep_retrying_without_closing_group(group_env, monkeypatch):
    e = group_env
    e.suggestion[0] = 2
    gate, parked = asyncio.Event(), asyncio.Event()
    gate.set()
    parked_indices = set()
    original_init = Gateway.__init__
    faults = []
    def init(self, *args, **kwargs):
        index = kwargs["shard"][0]
        async def fault_sleep(delay):
            if delay >= 16:
                parked_indices.add(index)
                if len(parked_indices) == 2:
                    parked.set()
                await asyncio.Event().wait()
            await asyncio.sleep(0)
        original_init(self, *args, **kwargs, sleep=fault_sleep, jitter=lambda: 0)
        self.ws_session = FaultSocket(self.ws_session, gate, lambda: True)
        faults.append(self.ws_session)
    monkeypatch.setattr(Gateway, "__init__", init)
    task = asyncio.create_task(e.group.run())
    try:
        await asyncio.wait_for(parked.wait(), 2)
        assert not task.done() and e.group.state == "degraded" and not e.group.available
        assert [f.calls for f in faults] == [5, 5]
        status = e.group.status()
        assert all(s["state"] == "backoff" and s["next_retry_at"] is not None for s in status["shards"])
        assert e.group.budget.remaining == 90 and not e.group.session.closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await e.group.close()
    assert e.group.session.closed and not e.group.tasks
    assert all(g.ws is None and not g.tasks for g in e.group.gateways)


@pytest.mark.parametrize("failure", [4010, 4011, 4012, 4013, 4014, 4914, 4915, "invalid_gateway", "stale_generation", "unsupported_environment", "program_error"])
async def test_non_exhaustion_failure_still_terminates_healthy_group(group_env, monkeypatch, failure):
    e = group_env
    e.suggestion[0] = 2
    release = asyncio.Event()
    original_connect = Gateway._connect
    error = (GatewayClosed(failure) if type(failure) is int else RuntimeError("synthetic program error")
             if failure == "program_error" else V2Error(failure, "synthetic group failure"))
    async def connect(self):
        if self.shard[0] == 1:
            await release.wait()
            raise error
        await original_connect(self)
    monkeypatch.setattr(Gateway, "_connect", connect)
    task = asyncio.create_task(e.group.run())
    try:
        await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        assert e.group.available
        release.set()
        with pytest.raises(RuntimeError) as exc:
            await asyncio.wait_for(task, 2)
        assert exc.value is error and e.group.state == "failed"
        assert not e.group.tasks and e.group.session.closed and not e.group.available
        assert all(g.ws is None and not g.tasks for g in e.group.gateways)
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
