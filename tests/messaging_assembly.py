"""Full host queue/pipeline and real local HTTP output, without invoking handlers directly."""
import asyncio
import importlib
import time

from aiohttp import web
from network_assembly import network_roundtrip
from test_messaging_state import chat_payload
from test_transport_http import MappedSession, upstream
from test_transport_receive import signed

from v2 import PLUGIN_NAME


async def messaging_roundtrip(lifecycle, client, headers, owner, instance, callback, monkeypatch):
    from extensions_assembly import ExtensionProbe, extension_roundtrip
    probe = ExtensionProbe()
    replies, completed = asyncio.Queue(), asyncio.Queue()
    sent = []
    panels, panel_requests = {}, []
    async def upstream_handler(request):
        result = await probe.handle(request)
        if result is not None:
            return result
        if request.path.startswith("/v2/panels"):
            assert request.headers["X-Union-Appid"] == "new-fixture-app"
            panel_requests.append((request.method, request.path))
            if request.method == "GET" and request.path == "/v2/panels":
                assert set(request.query) == {"scope", "limit"} and request.query["limit"] == "50"
                assert request.query["scope"] in {"group", "c2c", "channel", "dm"}
                return web.json_response({"records": [p for p in panels.values() if p["scope"] == request.query["scope"]], "is_end": True, "next_cursor": ""})
            if request.method == "GET":
                return web.json_response(panels["assembly-panel"])
            assert request.method == "POST" and request.path == "/v2/panels"
            data = await request.json()
            assert data["scope"] == "group" and data["target_type"] == "all" and len(data["panel"]["items"]) == 1
            panels["assembly-panel"] = {**data, "panel_id": "assembly-panel", "version": 1}
            return web.json_response({"panel_id": "assembly-panel"})
        assert request.method == "POST" and request.path in {"/v2/groups/group-one/messages", "/v2/users/user-one/messages"}
        assert request.headers["X-Union-Appid"] == "new-fixture-app"
        assert request.headers["Authorization"].startswith("QQBot ")
        body = await request.json()
        sent.append(body)
        replies.put_nowait(body)
        return web.json_response({"id": "assembly-real-id-" + str(len(sent)), "timestamp": "2026-09-20T08:00:00+08:00", "ext_info": {"ref_idx": "REFIDX_assembly" + str(len(sent))}})
    module = importlib.import_module(f"data.plugins.{PLUGIN_NAME}.v2.event")
    original_cleanup = module.V2MessageEvent.cleanup_temporary_local_files
    def cleanup(event):
        original_cleanup(event)
        completed.put_nowait(event)
    monkeypatch.setattr(module.V2MessageEvent, "cleanup_temporary_local_files", cleanup)
    config = owner.context.get_config()
    config["wake_prefix"] = ["/"]
    config["admins_id"] = ["not-the-fixture-user"]
    config.save_config()
    instance.session_isolated = True
    dispatcher = asyncio.create_task(lifecycle.event_bus.dispatch(), name="assembly-real-event-bus")
    try:
        async with upstream(upstream_handler) as base:
            await instance.http.session.close()
            instance.http.session = None
            instance.http._factory = lambda: MappedSession(base)
            for number, text in enumerate(("v2menu system", "/provider")):
                payload = chat_payload(message_id="assembly-chat-" + str(number), timestamp=time.time(), text=text)
                request = signed(payload, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
                response = await client.post(callback, content=request.raw, headers=dict(request.headers))
                assert response.status_code == 200 and response.json() == {"op": 12}
                body = await asyncio.wait_for(replies.get(), 5)
                event = await asyncio.wait_for(completed.get(), 5)
                assert body["msg_id"] == payload["d"]["id"] and body["msg_seq"] == 1
                assert event._has_send_oper
                assert event.get_extra("qq_send_result")["message_id"] == "assembly-real-id-" + str(number + 1)
                assert event.raw_data == payload and event.route.user == "user-one"
                assert event.get_extra("_session_isolated") is False and event.role == "member"
                assert owner.messages.lookup(instance.identity.robot, "member_openid", "group:group-one", "user-one")["source_message_id"] == payload["d"]["id"]
                if number == 0:
                    assert "系统指令" in body["content"] and "首页" in body["content"]
                    assert any(h.handler_name == "menu" for h in event.get_extra("activated_handlers"))
                else:
                    assert "权限不足" in body["content"]
            from astrbot.core.message.components import Plain
            from astrbot.core.message.message_event_result import MessageChain
            await instance.send_by_session(event.session, MessageChain([Plain("explicit active send")]))
            active = await asyncio.wait_for(replies.get(), 2)
            assert active["content"] == "explicit active send" and "msg_id" not in active
            prefix = f"/api/v1/plugins/extensions/{PLUGIN_NAME}"
            boot = (await client.get(prefix + "/bootstrap", headers=headers)).json()
            view = (await client.get(prefix + "/config", params={"platform_id": instance.identity.platform_id}, headers=headers)).json()
            virtual_clock = [time.time()]
            monkeypatch.setattr(owner.panels, "clock", lambda: virtual_clock[0])
            body = {"csrf": boot["csrf"], "platform_id": instance.identity.platform_id, "fingerprint": view["fingerprint"], "scene": "group", "menu_only": True}
            response = await client.post(prefix + "/panels/plan", json=body, headers=headers)
            assert response.status_code == 200 and not panel_requests
            virtual_clock[0] += 2
            enable_body = {**body, "plan_fingerprint": response.json()["fingerprint"]}
            assert (await client.post(prefix + "/panels/enable", json=enable_body, headers=headers)).status_code == 400
            assert not panel_requests
            response = await client.post(prefix + "/panels/enable", json={**enable_body, "confirm": True}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["panel_id"] == "assembly-panel" and response.json()["state"] == "synced"
            assert panel_requests.count(("POST", "/v2/panels")) == 1
            stopped = await client.post(prefix + "/panels/disable", json={**body, "confirm": True}, headers=headers)
            assert stopped.status_code == 200 and not stopped.json()["enabled"] and len(panels) == 1
            await extension_roundtrip(lifecycle, client, headers, owner, instance, callback, base, replies, completed, probe, event, monkeypatch)
            await network_roundtrip(client, headers, owner, instance, callback, replies, completed)
            assert not owner.delivery_slots.events
            assert instance.consumer.task and not instance.consumer.task.done()
            await asyncio.wait_for(asyncio.gather(*lifecycle.event_bus._pending_tasks), 5)
            diagnostic = (await client.get(prefix + "/config", params={"platform_id": instance.identity.platform_id}, headers=headers)).json()
            assert diagnostic["extension_state"]["operations"]
            assert all("result" not in row and "context" in row for row in diagnostic["extension_state"]["operations"])
    finally:
        dispatcher.cancel()
        await asyncio.gather(dispatcher, return_exceptions=True)
