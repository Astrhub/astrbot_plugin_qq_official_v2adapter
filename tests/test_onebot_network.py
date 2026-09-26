"""Real loopback OneBot transports share the production client and message ledger."""
import asyncio
import json
import socket
from contextlib import asynccontextmanager
from types import SimpleNamespace

import aiohttp
import pytest
from multidict import CIMultiDict
from test_messaging_send import sending as sending

from v2.errors import V2Error
from v2.network import MAX_FRAME, OneBotServer

TOKEN = "synthetic-onebot-token-only"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@asynccontextmanager
async def listener(sending, *, enabled=True, gate=True, writes=True, token=TOKEN, port=None):
    owner = SimpleNamespace(config={"onebot_network_enabled": gate}, messages=sending.store)
    adapter = SimpleNamespace(owner=owner, client=sending.client, identity=sending.client.identity, bot_id="", revoked=False)
    def check():
        if adapter.revoked:
            raise V2Error("stale_generation", "Fixture generation revoked.")
    adapter.check_generation = check
    server = OneBotServer(adapter, {"enable": enabled, "host": "127.0.0.1", "port": port or free_port(), "token": token, "writes": writes})
    adapter.runtime_status = lambda: {"online": False, "good": False, "platform_id": adapter.identity.platform_id, "generation": adapter.identity.generation, "network": server.status()}
    sending.client._state.status = adapter.runtime_status
    sending.client._state.network = server
    base = f"http://127.0.0.1:{server.config['port']}"
    async with aiohttp.ClientSession() as http:
        try:
            await server.start()
            yield SimpleNamespace(server=server, adapter=adapter, base=base, http=http, headers={"Authorization": "Bearer " + token})
        finally:
            await server.close()
            assert not server.requests and not server.peers and not server.queued_bytes and not server.events.contexts
            assert server.runner is None and server.site is None


@pytest.mark.parametrize("method,path,kwargs,status,code", [
    ("GET", "/get_status", {}, 401, "missing_token"),
    ("GET", "/get_status", {"headers": {"Authorization": "Bearer wrong"}}, 403, "invalid_token"),
    ("GET", "/get_status?access_token=" + TOKEN, {}, 200, None),
    ("GET", "/get_status?access_token=wrong", {"headers": {"Authorization": "Bearer " + TOKEN}}, 403, "invalid_token"),
    ("GET", "/get_status?access_token=" + TOKEN + "&access_token=" + TOKEN, {}, 400, "invalid_request"),
    ("POST", "/get_status", {"data": "x", "headers": {"Authorization": "Bearer " + TOKEN}}, 406, "unsupported_content_type"),
    ("POST", "/get_status", {"data": "[]", "headers": {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"}}, 400, "invalid_request"),
    ("GET", "/no_such_action", {"headers": {"Authorization": "Bearer " + TOKEN}}, 404, "unknown_action"),
    ("GET", "/get_friend_list", {"headers": {"Authorization": "Bearer " + TOKEN}}, 200, "unsupported"),
    ("GET", "/get_msg", {"headers": {"Authorization": "Bearer " + TOKEN}}, 200, "unsupported"),
])
async def test_http_status_and_authentication_contract(sending, method, path, kwargs, status, code, caplog):
    async with listener(sending) as n:
        async with n.http.request(method, n.base + path, **kwargs) as response:
            assert response.status == status
            result = await response.json()
            assert result.get("code") == code
            assert result["status"] == ("ok" if code is None else "failed")
            assert TOKEN not in json.dumps(result)
        assert TOKEN not in caplog.text and not sending.calls


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"x":{"a":1,"a":2}}', '{"x":NaN}', '{"x":Infinity}', '{"x":1e999}', '{"x":"\\ud800"}', "null", "true", "1", "[1]", "{", "[" * 30 + "0" + "]" * 30])
async def test_strict_json_rejected_without_execution(sending, raw):
    async with listener(sending) as n:
        async with n.http.post(n.base + "/send_group_msg", data=raw, headers={**n.headers, "Content-Type": "application/json"}) as r:
            assert r.status == 400 and (await r.json())["code"] == "invalid_request"
        assert not sending.calls


@pytest.mark.parametrize("style", ["query", "form", "json"])
@pytest.mark.parametrize("slash", ["", "/"])
async def test_http_formats_shared_codec_source_and_operation_id(sending, style, slash):
    async with listener(sending) as n:
        chat, internal = sending.observe()
        key = n.server.events.context(chat.source)
        params = {"group_id": "group-one", "message": '[{"type":"text","data":{"text":"hello &"}}]', "auto_escape": "false", "_qq_reply_context": key, "_qq_operation_id": "shared-operation"}
        opts = {"params" if style == "query" else "data" if style == "form" else "json": params}
        method = "GET" if style == "query" else "POST"
        async with n.http.request(method, n.base + "/send_group_msg" + slash, headers=n.headers, **opts) as r:
            assert r.status == 200
            result = await r.json()
            assert result["status"] == "ok", result
        reused = await internal.send_group_msg(group_id="group-one", message=[{"type": "text", "data": {"text": "hello &"}}], _qq_operation_id="shared-operation")
        assert reused == result["data"] and len(sending.calls) == 1
        assert sending.calls[0][1]["msg_id"] == chat.source.message_id
        assert sending.calls[0][1]["content"] == "hello &amp;"
        assert sending.store.operation(internal.identity.robot, "shared-operation")["seq"] == 1


@pytest.mark.parametrize("params", [
    {"group_id": 123, "message": "x"}, {"group_id": True, "message": "x"},
    {"group_id": "group-one", "message": "x", "auto_escape": "0"},
    {"group_id": "group-one", "message": "x", "msg_id": "forged"},
    {"group_id": "group-one", "message": [{"type": "unknown", "data": {}}]},
    {"group_id": "group-one", "message": "x", "access_token": TOKEN},
    {"group_id": "group-one", "message": "x", "self_id": "another-instance"},
])
async def test_no_silent_parameter_or_identity_coercion(sending, params):
    async with listener(sending) as n:
        sending.observe()
        async with n.http.post(n.base + "/send_group_msg", json=params, headers=n.headers) as r:
            result = await r.json()
            assert result["status"] == "failed"
            assert TOKEN not in json.dumps(result)
        assert not sending.calls


async def test_duplicate_conflicts_cq_and_auto_escape(sending):
    async with listener(sending) as n:
        chat, _ = sending.observe()
        key = n.server.events.context(chat.source)
        async with n.http.post(n.base + "/get_status?x=1", json={"x": 1}, headers=n.headers) as r:
            assert r.status == 400
        async with n.http.post(n.base + "/get_status", data="x=1&x=2", headers={**n.headers, "Content-Type": "application/x-www-form-urlencoded"}) as r:
            assert r.status == 400
        async with n.http.get(n.base + "/get_status", headers=CIMultiDict([("Authorization", "Bearer " + TOKEN)] * 2)) as r:
            assert r.status == 403
        for value, escaped in [("[CQ:at,qq=user-one] hi &amp;", False), ("[literal] &", False), ("[{literal}]", True)]:
            async with n.http.post(n.base + "/send_group_msg", json={"group_id": "group-one", "message": value, "auto_escape": escaped, "_qq_reply_context": key}, headers=n.headers) as r:
                assert (await r.json())["status"] == "ok"
        assert len(sending.calls) == 3


@pytest.mark.parametrize("echo", [None, True, False, 0, 7, 1.5, "echo", [], [1, {"x": "中"}], {}, {"nested": [None, False]}])
async def test_ws_echo_all_json_types_and_missing_are_distinct(sending, echo):
    async with listener(sending) as n:
        async with n.http.ws_connect(n.base + "/api/", headers=n.headers) as ws:
            await ws.send_json({"action": "get_version_info", "echo": echo})
            result = await ws.receive_json(timeout=2)
            assert result["echo"] == echo and type(result["echo"]) is type(echo) and result["status"] == "ok"
            await ws.send_json({"action": "no_action", "echo": echo})
            result = await ws.receive_json(timeout=2)
            assert result["retcode"] == 1404 and result["echo"] == echo
            await ws.send_json({"action": "get_status"})
            assert "echo" not in await ws.receive_json(timeout=2)


async def test_ws_auth_upgrade_malformed_and_secret_never_echoed(sending, caplog):
    async with listener(sending) as n:
        for headers, status in [({}, 401), ({"Authorization": "Bearer nope"}, 403)]:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await n.http.ws_connect(n.base + "/api", headers=headers)
            assert exc.value.status == status
        async with n.http.ws_connect(n.base + "/api?access_token=" + TOKEN) as ws:
            for raw in ['{"action":"get_status","echo":NaN}', "[]", "{", json.dumps({"action": "get_status", "echo": TOKEN})]:
                await ws.send_str(raw)
                result = await ws.receive_json(timeout=2)
                assert result["status"] == "failed" and "echo" not in result and TOKEN not in json.dumps(result)
            await ws.send_bytes(b"not-json")
            assert (await ws.receive_json(timeout=2))["retcode"] == 1400
        assert TOKEN not in caplog.text


async def test_read_only_and_business_failure_do_not_become_http_transport_errors(sending):
    async with listener(sending, writes=False) as n:
        sending.observe()
        async with n.http.post(n.base + "/send_group_msg", json={"group_id": "group-one", "message": "x"}, headers=n.headers) as r:
            assert r.status == 200 and (await r.json())["code"] == "network_read_only"
        assert not sending.calls
    async with listener(sending) as n:
        chat, _ = sending.observe(message_id="reject")
        key = n.server.events.context(chat.source)
        sending.modes.append(40034024)
        async with n.http.post(n.base + "/send_group_msg", json={"group_id": "group-one", "message": "x", "_qq_reply_context": key, "_qq_operation_id": "rejected"}, headers=n.headers) as r:
            result = await r.json()
            assert r.status == 200 and result["status"] == "failed"
            assert result["business_code"] == 40034024 and result["trace_id"] == "fixture-trace" and result["http_status"] == 200
            assert result["phase"] == "rejected" and result["operation_id"] == "rejected"


async def test_unknown_is_retained_and_queryable_not_replayed(sending):
    async with listener(sending) as n:
        chat, _ = sending.observe()
        key = n.server.events.context(chat.source)
        params = {"group_id": "group-one", "message": "x", "_qq_reply_context": key, "_qq_operation_id": "unknown-operation"}
        sending.modes.append("500")
        for _ in range(2):
            async with n.http.post(n.base + "/send_group_msg", json=params, headers=n.headers) as r:
                assert r.status == 200 and (await r.json())["status"] == "failed"
        async with n.http.get(n.base + "/_qq_get_send_status", params={"operation_id": "unknown-operation"}, headers=n.headers) as r:
            assert (await r.json())["data"]["state"] == "unknown"
        assert len(sending.calls) == 1


async def test_server_passive_quota_and_context_expiry_target_binding(sending):
    async with listener(sending) as n:
        chat, internal = sending.observe()
        key = n.server.events.context(chat.source)
        sending.observe(target="other", message_id="other")
        for params in [{"group_id": "other"}, {"user_id": "user-one"}]:
            action = "send_group_msg" if "group_id" in params else "send_private_msg"
            async with n.http.post(n.base + "/" + action, json={**params, "message": "x", "_qq_reply_context": key}, headers=n.headers) as r:
                assert (await r.json())["code"] == "identity_mismatch"
        for _ in range(4):
            await internal.send_group_msg(group_id="group-one", message="internal")
        async with n.http.ws_connect(n.base + "/api", headers=n.headers) as ws:
            for expected in ["passive", "active"]:
                if expected == "active":
                    sending.modes.append(40034128)
                await ws.send_json({"action": "send_group_msg", "params": {"group_id": "group-one", "message": "network", "_qq_reply_context": key}, "echo": "not-idempotency"})
                result = await ws.receive_json(timeout=2)
                assert result["status"] == "ok" and result["data"]["delivery"]["mode"] == expected
            assert result["data"]["delivery"]["reason"]["business_code"] == 40034128 and len(sending.calls) == 7
            assert sending.calls[-2][1]["msg_seq"] == 6 and "msg_id" not in sending.calls[-1][1]
        sending.clock[0] += 301
        async with n.http.post(n.base + "/send_group_msg", json={"group_id": "group-one", "message": "expired", "_qq_reply_context": key}, headers=n.headers) as r:
            assert (await r.json())["code"] == "reply_context_unavailable"
        assert len(sending.calls) == 7


async def test_frame_limits(sending):
    async with listener(sending) as n:
        async with n.http.post(n.base + "/get_status", data=b"x" * (MAX_FRAME + 1), headers={**n.headers, "Content-Type": "application/json"}) as r:
            assert r.status == 400
        async with n.http.ws_connect(n.base + "/api", headers=n.headers) as ws:
            await ws.send_str("x" * (MAX_FRAME + 1))
            result = await ws.receive(timeout=2)
            assert result.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}
        assert not sending.calls


@pytest.mark.parametrize("stop", ["disconnect", "close", "revoke"])
async def test_http_inflight_interruption_persists_unknown_without_replay(sending, monkeypatch, stop):
    async with listener(sending) as n:
        chat, _ = sending.observe()
        key = n.server.events.context(chat.source)
        finished = asyncio.Event()
        original = sending.store.finish
        def finish(*args, **kwargs):
            value = original(*args, **kwargs)
            finished.set()
            return value
        monkeypatch.setattr(sending.store, "finish", finish)
        sending.modes.append("wait")
        request = asyncio.create_task(n.http.post(n.base + "/send_group_msg", headers=n.headers,
            json={"group_id": "group-one", "message": "pending", "_qq_reply_context": key, "_qq_operation_id": "http-pending"}))
        try:
            await asyncio.wait_for(sending.entered.wait(), 2)
            if stop == "disconnect":
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)
            else:
                if stop == "revoke":
                    n.adapter.revoked = True
                    n.server.revoke()
                await n.server.close()
            await asyncio.wait_for(finished.wait(), 2)
            assert sending.store.operation(sending.client.identity.robot, "http-pending")["state"] == "unknown"
            assert len(sending.calls) == 1
        finally:
            sending.release.set()
            result = await asyncio.gather(request, return_exceptions=True)
            for item in result:
                if isinstance(item, aiohttp.ClientResponse):
                    item.release()


async def test_oversized_success_fails_explicitly_with_echo_and_capabilities_are_copies(sending, monkeypatch):
    async with listener(sending) as n:
        original = sending.client.call_action
        async def huge(action, **params):
            if action == "get_status":
                return {"body": "x" * MAX_FRAME}
            return await original(action, **params)
        monkeypatch.setattr(sending.client, "call_action", huge)
        async with n.http.get(n.base + "/get_status", headers=n.headers) as r:
            assert r.status == 200 and (await r.json())["code"] == "network_frame_exceeded"
        async with n.http.ws_connect(n.base + "/api", headers=n.headers) as ws:
            await ws.send_json({"action": "get_status", "echo": {"id": "retained"}})
            result = await ws.receive_json(timeout=2)
            assert result["code"] == "network_frame_exceeded" and result["echo"] == {"id": "retained"}
            for body in [{}, {"action": []}, {"action": "get_status", "params": []}]:
                await ws.send_json(body)
                assert (await ws.receive_json(timeout=2))["retcode"] == 1400
        first = sending.client.capabilities()
        first["actions"]["get_version_info"]["returns"].clear()
        assert sending.client.capabilities()["actions"]["get_version_info"]["returns"]


@pytest.mark.parametrize("raw,content_type", [(b"\xff", "application/json"), ('{"x":1}'.encode("utf-16"), "application/json"), (b"x=%FF", "application/x-www-form-urlencoded"), (b"x=%Z1", "application/x-www-form-urlencoded")])
async def test_invalid_character_encoding_does_not_change_inputs(sending, raw, content_type):
    async with listener(sending) as n:
        async with n.http.post(n.base + "/get_status", data=raw, headers={**n.headers, "Content-Type": content_type}) as r:
            assert r.status == 400
        assert not sending.calls


async def test_management_write_gate_and_extension_query_use_existing_services(sending):
    from v2.extensions.management import Management
    from v2.extensions.state import ExtensionStore
    state = ExtensionStore(sending.store)
    service = Management(sending.client.identity, sending.http, state, sending.store, settings=lambda: {"management_writes": False})
    sending.client._state.management = service
    sending.client._state.extension_state = state
    robot = sending.client.identity.robot
    state.begin(robot, "media-receipt", "upload_files", "synthetic-binding", context={"private": "do-not-export-context"})
    state.finish(robot, "media-receipt", "succeeded", result={"file_info": "do-not-export-receipt"})
    try:
        async with listener(sending) as n:
            async with n.http.get(n.base + "/set_group_ban", params={"group_id": "group-one", "user_id": "user-one", "duration": "0"}, headers=n.headers) as r:
                result = await r.json()
                assert r.status == 200 and result["code"] == "management_disabled"
            async with n.http.get(n.base + "/_qq_get_extension_status", params={"operation_id": "media-receipt"}, headers=n.headers) as r:
                data = (await r.json())["data"]
                assert data["state"] == "succeeded" and set(data) == {"op_id", "kind", "state", "updated", "error"}
                assert "do-not-export" not in str(data)
            async with n.http.get(n.base + "/_qq_get_capabilities", headers=n.headers) as r:
                cap = (await r.json())["data"]
                assert cap["actions"]["set_group_ban"]["permission"] == "unknown"
                assert cap["actions"]["get_msg"]["support"] == "unsupported"
                assert cap["actions"]["set_group_ban"]["parameters"]["duration"] == "int"
        assert not sending.calls
    finally:
        await service.close()
        await state.close()


@pytest.mark.parametrize("value", [True, "1.0", "01", "1e3", [], {}, 2**64])
async def test_integer_parameters_are_not_lossily_coerced(sending, value):
    async with listener(sending) as n:
        async with n.http.post(n.base + "/set_group_ban", json={"group_id": "group-one", "user_id": "user-one", "duration": value}, headers=n.headers) as r:
            assert r.status == 400 and (await r.json())["code"] == "invalid_request"
        assert not sending.calls


async def test_unicode_echo_uses_utf8_frame_budget_and_invalid_upgrade_is_closed(sending):
    async with listener(sending) as n:
        async with n.http.ws_connect(n.base + "/api", headers=n.headers) as ws:
            echo = "中" * 65000
            await ws.send_str(json.dumps({"action": "get_status", "echo": echo}, ensure_ascii=False))
            result = await ws.receive_json(timeout=2)
            assert result["status"] == "ok" and result["echo"] == echo
        async with n.http.get(n.base + "/api", headers={**n.headers, "Upgrade": "websocket", "Connection": "Upgrade"}) as r:
            assert r.status == 400 and (await r.json())["code"] == "invalid_request"
        assert not sending.calls


async def test_ws_close_frame_cancels_active_action_without_waiting_for_action_deadline(sending, monkeypatch):
    async with listener(sending) as n:
        chat, _ = sending.observe()
        key = n.server.events.context(chat.source)
        sending.modes.append("wait")
        finished = asyncio.Event()
        original = sending.store.finish
        def finish(*args, **kwargs):
            value = original(*args, **kwargs)
            finished.set()
            return value
        monkeypatch.setattr(sending.store, "finish", finish)
        ws = await n.http.ws_connect(n.base + "/api", headers=n.headers, autoping=False)
        try:
            await ws.send_json({"action": "send_group_msg", "params": {"group_id": "group-one", "message": "blocked", "_qq_reply_context": key, "_qq_operation_id": "close-frame"}})
            await asyncio.wait_for(sending.entered.wait(), 2)
            await ws.send_json({"action": "get_status", "echo": "busy-request"})
            busy = await ws.receive_json(timeout=2)
            assert busy["code"] == "network_action_capacity" and busy["echo"] == "busy-request"
            assert n.server.status()["active_ws_actions"] == 1 and len(sending.calls) == 1
            await ws.ping(b"bounded-control")
            pong = await ws.receive(timeout=2)
            assert pong.type == aiohttp.WSMsgType.PONG and pong.data == b"bounded-control"
            await asyncio.wait_for(ws.close(), 1)
            assert ws.close_code in {1000, 1001}
            await asyncio.wait_for(finished.wait(), 2)
            assert sending.store.operation(sending.client.identity.robot, "close-frame")["state"] == "unknown"
            print(f"P5_WS_CONTROL inflight_actions=1 overflow=explicit pong=received close_code={ws.close_code} ledger=unknown")
        finally:
            sending.release.set()
            await ws.close()
