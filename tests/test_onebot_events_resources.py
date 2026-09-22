"""Live subscriptions, disconnects and resource bounds without a second QQ receiver."""
import asyncio
import copy
from contextlib import AsyncExitStack
from types import SimpleNamespace

import aiohttp
import pytest
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import accept
from test_messaging_delivery import receiver as receiver
from test_onebot_network import TOKEN, free_port, listener
from test_onebot_network import sending as sending

from v2.errors import V2Error
from v2.network import MAX_FRAME, OneBotServer
from v2.network_events import Peer


async def test_single_host_dispatch_keeps_original_payload_ack_and_identity(receiver):
    owner, adapter = receiver
    owner.config["onebot_network_enabled"] = True
    adapter.network = type(adapter.network)(adapter, {"enable": True, "port": free_port(), "token": TOKEN})
    adapter.client._state.network = adapter.network
    await adapter.network.start()
    base = f"http://127.0.0.1:{adapter.network.config['port']}"
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as client:
        async with client.ws_connect(base + "/event") as first, client.ws_connect(base + "/") as second:
            for ws in (first, second):
                meta = await ws.receive_json(timeout=2)
                assert meta["post_type"] == "qq_event" and "self_id" not in meta
            raw = accept(owner, adapter)
            snapshot = copy.deepcopy(raw)
            assert adapter.consumer.step()
            one, two = await first.receive_json(timeout=2), await second.receive_json(timeout=2)
            assert one == two and one["post_type"] == "qq_event" and one["qq_type"] == "message"
            assert one["user_id"] == "user-one" and one["group_id"] == "group-one" and one["message_id"] == "msg-one"
            assert one["time"] == 1800000000 and one["sender"]["nickname"] == "same-name"
            assert not {"font", "sex", "age", "role"} & one.keys()
            assert not {"sex", "age", "role"} & one["sender"].keys()
            assert "self_id" not in one and one["_qq_reply_context"]
            assert owner.inbox.count(adapter.identity.settings_key) == 0 and adapter._event_queue.qsize() == 1
            event = adapter._event_queue.get_nowait()
            assert event.raw_data == snapshot and event.bot._source.message_id == one["message_id"]
            event.cleanup_temporary_local_files()
            profiles = copy.deepcopy(adapter.client._state.cache.items)
            await first.send_json({"reply": "must not execute", "kick": True})
            await second.send_json({"action": "get_status", "echo": [None]})
            assert (await second.receive_json(timeout=2))["echo"] == [None]
            assert profiles == adapter.client._state.cache.items and not adapter.sender.tasks
            assert adapter.ingress.worker is None and not adapter.gateway.online
            assert not owner.delivery_slots.events


@pytest.mark.parametrize("event", ["C2C_MESSAGE_CREATE", "AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
async def test_scene_views_remain_explicit_extensions_without_invented_ids(sending, event):
    async with listener(sending) as n:
        async with n.http.ws_connect(n.base + "/event/", headers=n.headers) as ws:
            await ws.receive_json(timeout=2)
            chat, _ = sending.observe(event)
            n.server.observe_chat(chat)
            data = await ws.receive_json(timeout=2)
            assert data["post_type"] == "qq_event" and data["_qq"]["scene"] == chat.route.scene
            assert data["time"] == chat.source.sent_at and data["message_id"] == chat.source.message_id
            assert ("_qq_reply_context" in data) == (chat.route.scene == "c2c")
            assert "self_id" not in data and "sub_type" not in data


async def test_real_self_id_meta_and_no_ticket_or_original_payload_export(sending):
    async with listener(sending) as n:
        n.adapter.bot_id = "observed-bot-id"
        n.server.heartbeat = 0.01
        async with n.http.ws_connect(n.base + "/event", headers=n.headers) as ws:
            frame = await ws.receive_json(timeout=2)
            assert frame["post_type"] == "meta_event" and frame["self_id"] == "observed-bot-id" and frame["sub_type"] == "connect"
            heartbeat = await ws.receive_json(timeout=2)
            assert heartbeat["meta_event_type"] == "heartbeat" and heartbeat["status"]["online"] is False and heartbeat["interval"] == 10
            n.server.heartbeat = 15
            event = SimpleNamespace(name="INTERACTION_CREATE", sent_at=1800000000, scene="group", target="group-one", actor="user-one", payload={"secret_ticket": "never-export"})
            n.server.observe_extension(event)
            frame = await ws.receive_json(timeout=2)
            assert frame["qq_type"] == "extension" and "never-export" not in str(frame)
            n.server.events.retained("FRIEND_ADD")
            frame = await ws.receive_json(timeout=2)
            assert frame["event_type"] == "FRIEND_ADD" and frame["post_type"] != "request" and "time" not in frame


async def test_slow_peer_disconnect_does_not_block_another_subscriber(sending, monkeypatch):
    async with listener(sending) as n:
        n.server.queue_frames = 2
        async with n.http.ws_connect(n.base + "/event", headers=n.headers) as slow:
            await slow.receive_json(timeout=2)
            slow_peer = next(iter(n.server.peers))
            entered, release = asyncio.Event(), asyncio.Event()
            async def blocked(frame):
                entered.set()
                await release.wait()
            monkeypatch.setattr(slow_peer.ws, "send_str", blocked)
            async with n.http.ws_connect(n.base + "/event", headers=n.headers) as fast:
                await fast.receive_json(timeout=2)
                n.server.publish({"post_type": "qq_event", "qq_type": "fixture"})
                await asyncio.wait_for(entered.wait(), 2)
                assert (await fast.receive_json(timeout=2))["qq_type"] == "fixture"
                for _ in range(3):
                    slow_peer.put('{"post_type":"qq_event"}')
                assert slow_peer.failure == "subscriber_overflow"
                assert (await slow.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
                n.server.publish({"post_type": "qq_event", "qq_type": "after_overflow"})
                assert (await fast.receive_json(timeout=2))["qq_type"] == "after_overflow"
                assert n.server.disconnections == 1
                release.set()


@pytest.mark.parametrize("limit", ["frames", "peer_bytes", "total_bytes"])
async def test_subscription_budgets_are_explicit_and_accounted(sending, limit):
    async with listener(sending) as n:
        frame = '{"x":"' + "x" * 32 + '"}'
        if limit == "frames":
            n.server.queue_frames = 1
        elif limit == "peer_bytes":
            n.server.peer_bytes = len(frame)
        else:
            n.server.total_bytes = len(frame)
        peer = Peer(n.server, None, True)
        peer.put(frame)
        assert n.server.queued_bytes == len(frame)
        peer.put(frame)
        assert peer.failure == "subscriber_overflow" and n.server.queued_bytes == len(frame)
        peer.clear()
        assert n.server.queued_bytes == 0


async def test_connection_and_active_request_limits(sending, monkeypatch):
    async with listener(sending) as n:
        async with AsyncExitStack() as stack:
            for _ in range(n.server.max_peers):
                await stack.enter_async_context(n.http.ws_connect(n.base + "/api", headers=n.headers))
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await n.http.ws_connect(n.base + "/api", headers=n.headers)
            assert exc.value.status == 503 and len(n.server.peers) == 16
        await n.server.close()
    async with listener(sending) as n:
        entered, release = asyncio.Queue(), asyncio.Event()
        async def blocked(action, **params):
            entered.put_nowait(True)
            await release.wait()
            return {"online": False}
        monkeypatch.setattr(sending.client, "call_action", blocked)
        requests = [asyncio.create_task(n.http.get(n.base + "/get_status", headers=n.headers)) for _ in range(n.server.max_requests)]
        try:
            for _ in requests:
                await asyncio.wait_for(entered.get(), 2)
            assert len(n.server.requests) == 32
            async with n.http.get(n.base + "/get_status", headers=n.headers) as response:
                assert response.status == 200 and (await response.json())["code"] == "network_capacity"
        finally:
            release.set()
            for response in await asyncio.gather(*requests):
                await response.read()
                response.release()


@pytest.mark.parametrize("stop", ["close", "disconnect", "timeout"])
async def test_inflight_cancel_uses_shared_unknown_ledger(sending, stop):
    async with listener(sending) as n:
        chat, _ = sending.observe()
        key = n.server.events.context(chat.source)
        sending.modes.append("wait")
        n.server.action_timeout = 0.1 if stop == "timeout" else 120
        ws = await n.http.ws_connect(n.base + "/api", headers=n.headers)
        try:
            await ws.send_json({"action": "send_group_msg", "params": {"group_id": "group-one", "message": "x", "_qq_reply_context": key, "_qq_operation_id": "inflight"}})
            await asyncio.wait_for(sending.entered.wait(), 2)
            if stop == "close":
                await n.server.close()
            elif stop == "disconnect":
                await ws.close()
                await n.server.close()
            else:
                result = await ws.receive_json(timeout=2)
                assert result["code"] == "action_timeout" and result["operation_id"] == "inflight" and result["phase"] == "result_unknown"
            assert sending.store.operation(sending.client.identity.robot, "inflight")["state"] == "unknown"
            assert len(sending.calls) == 1
        finally:
            sending.release.set()
            await ws.close()


@pytest.mark.parametrize("enable,gate", [(False, False), (False, True), (True, False)])
async def test_default_off_allocates_no_listener_or_network_tasks(sending, enable, gate):
    before = {task for task in asyncio.all_tasks() if task.get_name().startswith("qq-v2-onebot")}
    async with listener(sending, enabled=enable, gate=gate) as n:
        assert n.server.runner is None and n.server.site is None and not n.server.listening
        assert {task for task in asyncio.all_tasks() if task.get_name().startswith("qq-v2-onebot")} == before
        with pytest.raises(aiohttp.ClientConnectorError):
            await n.http.get(n.base + "/get_status", headers=n.headers)


async def test_port_conflict_cleans_failed_runner_and_generation_rebind_rotates_token(sending):
    async with listener(sending) as first:
        second = OneBotServer(first.adapter, {**first.server.config})
        with pytest.raises(V2Error) as exc:
            await second.start()
        assert exc.value.code == "network_bind_failed" and second.runner is None and second.site is None
        chat, _ = sending.observe()
        old_key = first.server.events.context(chat.source)
        port = first.server.config["port"]
        ws = await first.http.ws_connect(first.base + "/event", headers=first.headers)
        await ws.receive_json(timeout=2)
        await first.server.close()
        assert (await ws.receive(timeout=2)).type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED}
        async with listener(sending, port=port, token="rotated-synthetic-token-only") as replacement:
            async with replacement.http.get(replacement.base + "/get_status", headers=first.headers) as r:
                assert r.status == 403
            async with replacement.http.get(replacement.base + "/get_status", headers=replacement.headers) as r:
                assert r.status == 200
            with pytest.raises(V2Error) as exc:
                replacement.server.bind_context(old_key, "send_group_msg", {"group_id": "group-one"})
            assert exc.value.code == "reply_context_unavailable"
        await ws.close()


async def test_projection_failure_disconnects_instead_of_interrupting_host(sending, monkeypatch):
    async with listener(sending) as n:
        async with n.http.ws_connect(n.base + "/event", headers=n.headers) as ws:
            await ws.receive_json(timeout=2)
            def failure(chat):
                raise RuntimeError("synthetic invalid projection")
            monkeypatch.setattr(n.server.events, "chat", failure)
            n.server.observe_chat(object())
            assert n.server.last_error == "event_projection_failed"
            assert (await ws.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE


async def test_frame_overflow_context_eviction_and_no_replay(sending):
    async with listener(sending) as n:
        n.server.events.capacity = 2
        keys = [n.server.events.context(sending.observe(message_id=str(i))[0].source) for i in range(3)]
        assert len(n.server.events.contexts) == 2 and keys[0] not in n.server.events.contexts
        async with n.http.ws_connect(n.base + "/event", headers=n.headers) as ws:
            assert (await ws.receive_json(timeout=2))["meta_event_type"] == "lifecycle"
            n.server.publish({"post_type": "qq_event", "payload": "x" * MAX_FRAME})
            assert (await ws.receive(timeout=2)).type == aiohttp.WSMsgType.CLOSE
            assert n.server.last_error == "event_frame_exceeded"
        assert not sending.calls
