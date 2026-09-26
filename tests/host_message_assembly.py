"""Profile routing, builtin group context and reply decoration on real V2 events."""
import asyncio
import copy
import time

from astrbot.core.message.components import At
from astrbot.core.provider.entities import ProviderRequest
from test_messaging_state import chat_payload
from test_messaging_wakeup import GROUP_BOT, READY_ID
from test_transport_receive import signed


async def host_message_roundtrip(lifecycle, client, owner, instance, callback, replies, completed):
    manager = owner.context.astrbot_config_mgr
    config = copy.deepcopy(dict(owner.context.get_config()))
    config["wake_prefix"] = ["#"]
    config["platform_settings"].update(unique_session=True, ignore_bot_self_message=False, ignore_at_all=True,
        reply_with_mention=False, reply_with_quote=False, empty_mention_waiting=True)
    config["provider_ltm_settings"]["group_icl_enable"] = True
    config["provider_ltm_settings"]["active_reply"]["enable"] = False
    profile = await manager.create_conf(config, "V2 host configuration")
    previous_routes = dict(manager.ucr.umop_to_conf_id)
    previous_isolated, previous_bot = instance.session_isolated, instance.bot_id
    builtin = next(s.star_cls for s in owner.context.get_all_stars() if s.module_path == "astrbot.builtin_stars.astrbot.main")
    instance.session_isolated, instance.bot_id = False, READY_ID
    count = 0

    async def receive(text, *, mention=False, sender="user-one"):
        nonlocal count
        count += 1
        payload = chat_payload("GROUP_MESSAGE_CREATE", message_id=f"host-config-{count}", timestamp=time.time(), text=text, sender=sender)
        if mention:
            payload["d"]["mentions"] = [{"id": GROUP_BOT, "is_you": True}]
        request = signed(payload, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
        response = await client.post(callback, content=request.raw, headers=dict(request.headers))
        assert response.status_code == 200 and response.json() == {"op": 12}
        event = await asyncio.wait_for(completed.get(), 5)
        bodies = []
        while not replies.empty():
            bodies.append(replies.get_nowait())
        assert event.unified_msg_origin == "webhook-fixture:GroupMessage:group-one"
        assert event.raw_data == payload and event.bot._source.message_id == payload["d"]["id"]
        assert manager.get_conf_info(event.unified_msg_origin)["id"] == profile
        assert owner.context.get_config(event.unified_msg_origin)["wake_prefix"] == ["#"]
        assert not event.get_extra("_session_isolated")
        return event, bodies

    try:
        await manager.ucr.update_routing_data({"webhook-fixture:*:*": profile, **previous_routes})
        await lifecycle.reload_pipeline_scheduler(profile)
        assert owner.context.get_config()["wake_prefix"] == ["/"]
        event, bodies = await receive("#sid")
        assert event.is_at_or_wake_command and event.get_message_str() == "sid"
        assert len(bodies) == 1 and "UMO: 「webhook-fixture:GroupMessage:group-one」" in bodies[0]["content"]
        assert "message_reference" not in bodies[0] and "qqbot-at-user" not in bodies[0]["content"]
        event, bodies = await receive("/sid")
        assert not event.is_at_or_wake_command and not bodies
        assert not any(h.handler_name == "sid" for h in event.get_extra("activated_handlers"))

        event, bodies = await receive(f"<@{GROUP_BOT}> 你好", mention=True)
        assert event.is_at_or_wake_command and event.get_self_id() == GROUP_BOT != instance.bot_id
        assert event.trace.message_outline == event.get_message_outline() == " 你好"
        assert event.message_str == "你好" and event.message_obj.message_str == " 你好"
        assert any(isinstance(p, At) and p.qq == GROUP_BOT for p in event.get_messages())
        assert len(bodies) == 1 and "未找到任何可用的对话模型" in bodies[0]["content"]
        group = builtin.group_chat_context
        records = group.raw_records[event.unified_msg_origin]
        assert any("[DIRECTED AT YOU]" in record and "[At: ]" in record and "你好" in record for record in records)
        following, bodies = await receive("ordinary continuation")
        assert not following.is_at_or_wake_command and not bodies
        assert not await group.need_active_reply(following)
        request = ProviderRequest(prompt=following.message_str)
        await group.on_req_llm(following, request)
        assert request.prompt == "ordinary continuation"
        assert any("[DIRECTED AT YOU]" in p.text and "你好" in p.text for p in request.extra_user_content_parts)
        assert not group.raw_records[following.unified_msg_origin]

        selected = owner.context.get_config(event.unified_msg_origin)
        selected["provider_ltm_settings"]["group_icl_enable"] = False
        selected["platform_settings"].update(ignore_bot_self_message=True, reply_prefix="selected:", reply_with_mention=True, reply_with_quote=True)
        selected.save_config()
        await lifecycle.reload_pipeline_scheduler(profile)
        event, bodies = await receive("#sid")
        assert len(bodies) == 1 and bodies[0]["content"].startswith('<qqbot-at-user id="user-one" />\nselected:')
        assert bodies[0]["message_reference"] == {"message_id": "REFIDX_" + event.message_obj.message_id}
        assert bodies[0]["msg_id"] == event.message_obj.message_id and bodies[0]["msg_seq"] == 1
        event, bodies = await receive(f"<@{GROUP_BOT}> #sid", mention=True, sender=GROUP_BOT)
        assert event.is_stopped() and not event.is_wake and not bodies
        event, bodies = await receive("context disabled")
        assert not event.get_extra("_group_context_record_id") and not bodies
        assert not group.raw_records[event.unified_msg_origin]
        print("HOST_MESSAGE_COMPAT: real UMO-selected # prefix, local-self outline/Trace, unchanged group context, self-message ignore and decorated HTTP reply")
    finally:
        instance.session_isolated, instance.bot_id = previous_isolated, previous_bot
        await manager.ucr.update_routing_data(previous_routes)
        lifecycle.pipeline_scheduler_mapping.pop(profile, None)
        await manager.delete_conf(profile)
