"""WebSocket correlation, scheduled heartbeat and pre-writer overflow boundaries."""
import asyncio
import json
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp import web
from test_onebot_network import listener
from test_onebot_network import sending as sending

from v2.network_events import Peer


@pytest.mark.parametrize("echo", [None, False, 0, "correlation", [1, None], {"key": "value"}])
async def test_rejected_ws_envelope_keeps_safe_echo(sending, echo):
    async with listener(sending) as n:
        async with n.http.ws_connect(n.base + "/api", headers=n.headers) as ws:
            await ws.send_json({"action": "get_status", "echo": echo, "unexpected": True})
            result = await ws.receive_json(timeout=2)
            assert result["code"] == "invalid_request"
            assert "echo" in result and result["echo"] == echo and type(result["echo"]) is type(echo)
            assert not sending.calls


async def test_ws_malformed_or_credential_envelope_never_reuses_previous_echo(sending):
    async with listener(sending) as n:
        async with n.http.ws_connect(n.base + "/api", headers=n.headers) as ws:
            await ws.send_json({"action": "get_status", "echo": "previous"})
            assert (await ws.receive_json(timeout=2))["echo"] == "previous"
            for raw in ("{", json.dumps({"action": "get_status", "echo": n.server.config["token"], "unexpected": True}),
                        json.dumps({"action": "get_status", "unexpected": True})):
                await ws.send_str(raw)
                result = await ws.receive_json(timeout=2)
                assert result["code"] == "invalid_request" and "echo" not in result
                assert n.server.config["token"] not in json.dumps(result)


async def test_heartbeat_still_runs_while_event_queue_remains_nonempty(sending):
    async with listener(sending) as n:
        n.server.heartbeat = 0.01
        n.adapter.bot_id = "observed-bot-id"
        heartbeat = asyncio.Event()
        messages = []
        async def transmit(frame):
            value = json.loads(frame)
            messages.append(value)
            if value.get("meta_event_type") == "heartbeat":
                heartbeat.set()
            else:
                peer.put('{"post_type":"qq_event","qq_type":"continuous"}')
            await asyncio.sleep(0.003)  # Simulated socket drain, with data always ready next.
        peer = Peer(n.server, SimpleNamespace(send_str=transmit), True)
        peer.put('{"post_type":"qq_event","qq_type":"continuous"}')
        peer.writer = asyncio.create_task(n.server.write_peer(peer))
        try:
            await asyncio.wait_for(heartbeat.wait(), 0.3)
            assert any(value.get("qq_type") == "continuous" for value in messages)
            assert next(value for value in messages if value.get("meta_event_type") == "heartbeat")["self_id"] == "observed-bot-id"
        finally:
            peer.writer.cancel()
            await asyncio.gather(peer.writer, return_exceptions=True)
            peer.clear()
            assert n.server.queued_bytes == 0


async def test_overflow_during_ws_prepare_closes_without_flushing_backlog(sending, monkeypatch):
    async with listener(sending) as n:
        n.server.queue_frames = 1
        entered, release = asyncio.Event(), asyncio.Event()
        original = web.WebSocketResponse.prepare
        async def delayed_prepare(ws, request):
            entered.set()
            await release.wait()
            return await original(ws, request)
        monkeypatch.setattr(web.WebSocketResponse, "prepare", delayed_prepare)
        connecting = asyncio.ensure_future(n.http.ws_connect(n.base + "/event", headers=n.headers))
        ws = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            peer = next(iter(n.server.peers))
            assert peer.writer is None
            for index in range(2):
                n.server.publish({"post_type": "qq_event", "qq_type": "during_handshake", "index": index})
            assert peer.failure == "subscriber_overflow"
            release.set()
            ws = await asyncio.wait_for(connecting, 2)
            received = await ws.receive(timeout=2)
            assert received.type == aiohttp.WSMsgType.CLOSE
            assert received.data == 1013
        finally:
            release.set()
            if ws is not None:
                await ws.close()
            if not connecting.done():
                connecting.cancel()
            await asyncio.gather(connecting, return_exceptions=True)
