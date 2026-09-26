"""Slow close of a real loopback socket must not stop a healthy shard."""
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
from test_shard_supervision import observe_terminals


async def test_slow_shard_close_releases_only_its_socket_and_keeps_host_reply(group_env, plugin_module, tmp_path, monkeypatch):
    e = group_env
    e.suggestion[0] = 2
    host = HostConfig(tmp_path / "host.json", {**e.config, "shard_mode": "auto"})
    host["platform_settings"] = {}
    host.save_config()
    ctx, queue = context(), asyncio.Queue()
    ctx.get_config = lambda *args: host
    ctx.platform_manager = PlatformManager(host, queue)
    http = importlib.import_module(plugin_module.__package__ + ".v2.transport.http")
    ws = importlib.import_module(plugin_module.__package__ + ".v2.transport.websocket")
    monkeypatch.setattr(http.HTTPTransport, "_make_session", lambda self: e.http._factory())
    async def no_metrics(**kwargs):
        pass
    monkeypatch.setattr(Metric, "upload", no_metrics)
    terminals = observe_terminals(monkeypatch, ws.Gateway)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        await ctx.platform_manager.reload(host["platform"][0])
        instance = next(iter(owner.instances))
        for _ in range(2):
            await asyncio.wait_for(e.handshakes.get(), 2)
            await asyncio.wait_for(e.beats.get(), 2)
        socket = instance.gateway.gateways[1].ws
        transport = socket._response.connection.transport
        e.slow_close.add(1)
        await e.sockets[1].send_json({"op": 7})
        failed, error = await asyncio.wait_for(terminals.get(), 4)
        assert error.code == "ws_close_timeout" and failed.shard == (1, 2)
        assert socket.closed and socket._response.closed and transport.is_closing()
        await e.sockets[0].send_json(chat_payload(message_id="healthy-after-slow-close", timestamp=time.time()))
        event = await asyncio.wait_for(queue.get(), 1)
        await event.send(MessageChain([Plain("healthy reply")]))
        event.cleanup_temporary_local_files()
        assert e.messages[-1] == ("/v2/groups/group-one/messages", {"msg_id": "healthy-after-slow-close", "msg_seq": 1, "msg_type": 0, "content": "healthy reply"})
        e.message_errors.append(40034128)
        await event.send(MessageChain([Plain("active healthy reply")]))
        assert len(e.messages) == 3 and e.messages[-2][1]["msg_seq"] == 2
        assert e.messages[-1] == ("/v2/groups/group-one/messages", {"msg_type": 0, "content": "active healthy reply"})
        assert instance.runtime_status()["state"] == "degraded" and instance.runtime_status()["message_ready"]
        node = instance.gateway.status()["shards"][1]
        assert node["state"] == "failed" and node["failure"]["code"] == "ws_close_timeout" and node["recovery"] == "reload_required"
        assert not failed.tasks and failed.ws is None and not instance.gateway.session.closed
    finally:
        e.close_release.set()
        await owner.terminate()
    assert not instance.gateway.tasks and instance.gateway.session.closed and not owner.instances


@pytest.mark.parametrize("failure", ["fatal", "cancel", "close_wait"])
async def test_slow_cleanup_preserves_fatal_and_cancellation_and_covers_close_wait(group_env, monkeypatch, failure):
    from v2.errors import V2Error
    from v2.transport.websocket import Gateway, GatewayClosed
    e = group_env
    e.suggestion[0] = 2
    e.slow_close.add(1)
    release = asyncio.Event()
    original_read = Gateway._read
    async def read(self):
        if self.shard[0] == 1:
            await release.wait()
            raise GatewayClosed(4014)
        await original_read(self)
    monkeypatch.setattr(Gateway, "_read", read)
    task = asyncio.create_task(e.group.run())
    try:
        for _ in range(2):
            await asyncio.wait_for(e.handshakes.get(), 2)
        await asyncio.wait_for(e.beats.get(), 2)
        socket = e.group.gateways[1].ws
        transport = socket._response.connection.transport
        if failure == "close_wait":
            original_close = socket.close
            async def close():
                socket._waiting = True
                socket._closing = False
                return await original_close()
            monkeypatch.setattr(socket, "close", close)
        if failure == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 4)
        else:
            release.set()
            with pytest.raises(V2Error) as exc:
                await asyncio.wait_for(task, 4)
            assert exc.value.business_code == 4014 and exc.value.code == "gateway_closed"
        assert socket.closed and socket._response.closed and transport.is_closing()
        assert e.group.session.closed and not e.group.tasks
        assert all(g.ws is None and not g.tasks for g in e.group.gateways)
        assert e.group.status()["shards"][1]["cleanup_failure"]["code"] == "ws_close_timeout"
    finally:
        e.close_release.set()
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_all_slow_closing_shards_terminate_group_without_socket_leaks(group_env):
    from v2.errors import V2Error
    e = group_env
    e.suggestion[0] = 2
    e.slow_close.update({0, 1})
    task = asyncio.create_task(e.group.run())
    try:
        for _ in range(2):
            await asyncio.wait_for(e.handshakes.get(), 2)
            await asyncio.wait_for(e.beats.get(), 2)
        sockets = [g.ws for g in e.group.gateways]
        for socket in e.sockets.values():
            await socket.send_json({"op": 7})
        with pytest.raises(V2Error) as exc:
            await asyncio.wait_for(task, 4)
        assert exc.value.code == "gateway_group_failed"
        assert all(s.closed and s._response.closed for s in sockets)
        assert e.group.session.closed and not e.group.tasks
        assert all(g.ws is None and not g.tasks for g in e.group.gateways)
        assert [s["failure"]["code"] for s in e.group.status()["shards"]] == ["ws_close_timeout", "ws_close_timeout"]
    finally:
        e.close_release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
