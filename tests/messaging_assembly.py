"""Full host queue/pipeline and real local HTTP output, without invoking handlers directly."""
import asyncio
import copy
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
                assert event.unified_msg_origin == "webhook-fixture:GroupMessage:user-one_group-one"
                assert event.message_obj.session_id == event.get_session_id() == "user-one_group-one"
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
            await group_mention_roundtrip(client, owner, instance, callback, replies, completed, monkeypatch)
            await session_compat_roundtrip(lifecycle, client, owner, instance, callback, replies, completed)
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


async def group_mention_roundtrip(client, owner, instance, callback, replies, completed, monkeypatch):
    from astrbot.builtin_stars.builtin_commands.commands.help import HelpCommand
    from astrbot.core.star.star_handler import EventType, star_handlers_registry
    from test_messaging_wakeup import GROUP_BOT, GROUP_MEMBER, READY_ID

    async def no_external_notice(self):
        return ""
    monkeypatch.setattr(HelpCommand, "_query_astrbot_notice", no_external_notice)
    config = owner.context.get_config()
    previous_plugins, previous_bot_id = config.get("plugin_set", ["*"]), instance.bot_id
    config["plugin_set"] = ["another-plugin"]
    config.save_config()
    instance.bot_id = READY_ID
    try:
        handlers = star_handlers_registry.get_handlers_by_event_type(EventType.AdapterMessageEvent, plugins_name=config["plugin_set"])
        assert any(h.handler_name == "help" and h.handler_module_path == "astrbot.builtin_stars.builtin_commands.main" for h in handlers)
        assert all(not h.handler_module_path.startswith(f"data.plugins.{PLUGIN_NAME}.") for h in handlers)
        cases = [
            ({"id": GROUP_BOT, "member_openid": GROUP_BOT, "is_you": True}, f"<@{GROUP_BOT}> help", True),
            ({"id": GROUP_BOT, "is_you": True}, f"<@!{GROUP_BOT}> help", True),
            ({"member_openid": GROUP_MEMBER, "is_you": True}, f"<@{GROUP_MEMBER}> help", True),
            ({"id": GROUP_BOT, "member_openid": GROUP_MEMBER, "is_you": True}, f"<@{GROUP_BOT}> help", True),
            ({"id": GROUP_BOT, "is_you": True}, f"<@{GROUP_BOT}> 你好", True),
            (None, f"<@{GROUP_BOT}> help", False),
            ({"id": GROUP_BOT, "member_openid": GROUP_BOT, "bot": True, "is_you": False}, f"<@{GROUP_BOT}> help", False),
            ({"id": GROUP_BOT, "member_openid": GROUP_BOT, "is_you": 1}, f"<@{GROUP_BOT}> help", False),
        ]
        for index, (mention, text, expected_wake) in enumerate(cases):
            operation_count = owner.messages.db.execute("SELECT count(*) FROM operations").fetchone()[0]
            payload = chat_payload("GROUP_MESSAGE_CREATE", message_id=f"assembly-group-mention-{index}", timestamp=time.time(), text=text)
            payload["d"]["mentions"] = [mention] if mention else []
            request = signed(payload, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
            response = await client.post(callback, content=request.raw, headers=dict(request.headers))
            assert response.status_code == 200 and response.json() == {"op": 12}
            event = await asyncio.wait_for(completed.get(), 5)
            assert event.is_at_or_wake_command is expected_wake and event.raw_data == payload
            if expected_wake:
                assert event.is_wake
            assert PLUGIN_NAME not in event.plugins_name and event.role == "member"
            assert event.bot._source.message_id == payload["d"]["id"] and event.route.user == "user-one"
            if expected_wake:
                body = await asyncio.wait_for(replies.get(), 2)
                assert body["msg_id"] == payload["d"]["id"] and body["msg_seq"] == 1 and event._has_send_oper
                assert event.get_self_id() == (GROUP_MEMBER if index == 2 else GROUP_BOT)
                if index < 4:
                    assert "AstrBot v" in body["content"] and event.get_message_str() == "help"
                    assert any(h.handler_name == "help" for h in event.get_extra("activated_handlers"))
                else:
                    assert "未找到任何可用的对话模型" in body["content"] and event.get_message_str() == "你好"
            else:
                assert replies.empty() and not event._has_send_oper
            assert owner.messages.db.execute("SELECT count(*) FROM operations").fetchone()[0] == operation_count + int(expected_wake)
            assert instance.bot_id == READY_ID
        print("GROUP_MENTION: actual host pipeline, builtin help and basic wake pass with V2 handlers excluded; unrelated mentions trigger no command or send")
    finally:
        instance.bot_id = previous_bot_id
        config["plugin_set"] = previous_plugins
        config.save_config()


async def session_compat_roundtrip(lifecycle, client, owner, instance, callback, replies, completed):
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain

    manager = owner.context.astrbot_config_mgr
    config = copy.deepcopy(dict(owner.context.get_config()))
    config["wake_prefix"], config["admins_id"] = ["!"], ["user-one"]
    config["platform_settings"]["unique_session"] = True
    profile = await manager.create_conf(config, "native UMO fixture")
    previous_routes, previous_isolated = dict(manager.ucr.umop_to_conf_id), instance.session_isolated
    try:
        await lifecycle.reload_pipeline_scheduler(profile)
        cases = [
            ("GROUP_AT_MESSAGE_CREATE", False, "webhook-fixture:GroupMessage:group-one"),
            ("C2C_MESSAGE_CREATE", False, "webhook-fixture:FriendMessage:user-one"),
            ("GROUP_AT_MESSAGE_CREATE", True, "webhook-fixture:GroupMessage:user-one_group-one"),
        ]
        for index, (name, isolated, umo) in enumerate(cases):
            instance.session_isolated = isolated
            await manager.ucr.update_routing_data({umo: profile, **previous_routes})
            for command in ("sid", "v2menu"):
                payload = chat_payload(name, message_id=f"assembly-native-{index}-{command}", timestamp=time.time(), text="!" + command)
                payload["d"].pop("mentions", None)
                request = signed(payload, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
                response = await client.post(callback, content=request.raw, headers=dict(request.headers))
                assert response.status_code == 200 and response.json() == {"op": 12}
                event = await asyncio.wait_for(completed.get(), 5)
                body = await asyncio.wait_for(replies.get(), 2)
                assert event.unified_msg_origin == umo and event.role == "admin"
                assert event.message_obj.session_id == event.get_session_id() == umo.split(":", 2)[2]
                assert owner.context.get_config(umo)["wake_prefix"] == ["!"]
                assert event.raw_data == payload and event.bot._source.message_id == payload["d"]["id"]
                assert body["msg_id"] == payload["d"]["id"] and body["msg_seq"] == 1
                assert any(h.handler_name == ("sid" if command == "sid" else "menu") for h in event.get_extra("activated_handlers"))
                assert ("UMO: 「" + umo + "」" if command == "sid" else "!v2menu") in body["content"]
            assert await owner.context.send_message(umo, MessageChain([Plain("native UMO active send")]))
            body = await asyncio.wait_for(replies.get(), 2)
            assert body["content"] == "native UMO active send" and "msg_id" not in body
        print("NATIVE_UMO: group/C2C/isolated group use real host profile routing, sid, menu and serialized send_by_session; no extra isolation prefix")
    finally:
        instance.session_isolated = previous_isolated
        await manager.ucr.update_routing_data(previous_routes)
        lifecycle.pipeline_scheduler_mapping.pop(profile, None)
        await manager.delete_conf(profile)
