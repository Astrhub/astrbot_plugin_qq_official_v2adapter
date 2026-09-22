"""OneBot observes the actual host pipeline and uses its reply ledger through real sockets."""
import asyncio
import time

import aiohttp
from test_messaging_state import chat_payload
from test_onebot_network import TOKEN
from test_transport_receive import signed

from v2 import PLUGIN_NAME


async def network_roundtrip(client, headers, owner, instance, callback, replies, completed):
    prefix = f"/api/v1/plugins/extensions/{PLUGIN_NAME}"
    network = instance.network
    assert network.listening and instance.gateway is None
    assert (await client.get(prefix + "/bootstrap", headers={"Authorization": "Bearer " + TOKEN})).status_code == 401
    base = f"http://127.0.0.1:{network.config['port']}"
    async with aiohttp.ClientSession(headers={"Authorization": "Bearer " + TOKEN}) as http:
        async with http.get(base + "/get_status", headers=headers) as response:
            assert response.status == 403  # A Dashboard JWT is not the dedicated token.
        async with http.ws_connect(base + "/event") as events, http.ws_connect(base + "/") as mixed:
            for ws in (events, mixed):
                notice = await ws.receive_json(timeout=2)
                assert notice["post_type"] == "qq_event" and "self_id" not in notice
            payload = chat_payload(message_id="network-pipeline-message", timestamp=time.time(), text="/provider")
            request = signed(payload, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
            response = await client.post(callback, content=request.raw, headers=dict(request.headers))
            assert response.status_code == 200
            host_reply = await asyncio.wait_for(replies.get(), 5)
            event = await asyncio.wait_for(completed.get(), 5)
            assert "权限不足" in host_reply["content"] and host_reply["msg_seq"] == 1
            assert event.role == "member" and event.raw_data == payload
            one, two = await events.receive_json(timeout=2), await mixed.receive_json(timeout=2)
            assert one == two and one["message_id"] == payload["d"]["id"]
            params = {"group_id": "group-one", "message": "explicit external reply", "_qq_reply_context": one["_qq_reply_context"], "_qq_operation_id": "network-assembly-op"}
            async with http.post(base + "/send_group_msg/", json=params) as sent:
                result = await sent.json()
                assert sent.status == 200 and result["status"] == "ok", result
            actual = await asyncio.wait_for(replies.get(), 2)
            assert actual["msg_id"] == payload["d"]["id"] and actual["msg_seq"] == 2
            await mixed.send_json({"action": "send_group_msg", "params": params, "echo": None})
            again = await mixed.receive_json(timeout=2)
            assert again["data"] == result["data"] and "echo" in again and again["echo"] is None and replies.empty()
            boot = (await client.get(prefix + "/bootstrap", headers=headers)).json()
            off = await client.post(prefix + "/flags", json={"csrf": boot["csrf"], "revision": boot["flags_revision"], "patch": {"webui_enabled": False}, "confirm": True}, headers=headers)
            assert off.status_code == 200
            assert (await client.get(prefix + "/connection", params={"platform_id": instance.identity.platform_id}, headers=headers)).status_code == 403
            async with http.get(base + "/_qq_get_capabilities") as response:
                capabilities = (await response.json())["data"]
                assert capabilities["network_api"]["state"] == "listening" and TOKEN not in str(capabilities)
            # Re-enable through the real plugin settings save path, as documented for a closed Page API.
            owner.config["webui_enabled"] = True
            owner.config.save_config()
            boot = (await client.get(prefix + "/bootstrap", headers=headers)).json()
            off = await client.post(prefix + "/flags", json={"csrf": boot["csrf"], "revision": boot["flags_revision"], "patch": {"onebot_network_enabled": False}, "confirm": True}, headers=headers)
            assert off.status_code == 200
            await asyncio.wait_for(network.stopped.wait(), 4)
            assert not network.requests and not network.peers and network.runner is None
            assert instance.state == "webhook_ready" and not instance.http.session.closed
            boot = (await client.get(prefix + "/bootstrap", headers=headers)).json()
            on = await client.post(prefix + "/flags", json={"csrf": boot["csrf"], "revision": boot["flags_revision"], "patch": {"onebot_network_enabled": True}, "confirm": True}, headers=headers)
            assert on.status_code == 200 and not network.listening  # Enabling does not silently restart it.
    print("P5_ASSEMBLY host_permission=preserved ws_subscribers=2 shared_operation_attempts=1 page_disabled_listener=running gate_disabled_listener=closed qq_extra_ws=0")
