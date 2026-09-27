"""Atomic native card delivery and strict QQ field boundaries."""
import copy

import pytest
from aiohttp import web
from astrbot.core.message.components import Json, Plain
from astrbot.core.message.message_event_result import MessageChain
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_transport_http import MappedSession, upstream

from test_messaging_delivery import accept


@pytest.fixture
async def card_receiver(receiver):
    owner, instance = receiver
    seen = []

    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "card-fixture", "expires_in": 7200})
        seen.append((request.method, request.path, await request.json()))
        return web.json_response({"id": "qq-card-message"})

    async with upstream(handler) as base:
        instance.http._factory = lambda: MappedSession(base)
        instance.sender.is_online = lambda: True
        accept(owner, instance)
        assert instance.consumer.step()
        instance._event_queue.get_nowait().cleanup_temporary_local_files()
        accept(owner, instance, event="C2C_MESSAGE_CREATE", message_id="c2c-card", target="user-one")
        assert instance.consumer.step()
        instance._event_queue.get_nowait().cleanup_temporary_local_files()
        yield owner, instance, seen


def payload():
    return {"msg_type": 2, "markdown": {"content": "## 功能菜单\n请选择操作"},
            "keyboard": {"content": {"rows": [{"buttons": [{
                "id": "help", "render_data": {"label": "查看帮助", "style": 1},
                "action": {"type": 2, "permission": {"type": 2}, "data": "/帮助", "enter": False},
            }]}]}}}


@pytest.mark.asyncio
async def test_native_json_card_uses_one_exact_official_message_request(card_receiver):
    _, instance, seen = card_receiver
    route = instance.client.route_for("group", "group-one")
    result = await instance.client.send(route, MessageChain([Json(payload())]))
    assert result["message_id"] == "qq-card-message" and result["state"] == "sent"
    assert seen == [("POST", "/v2/groups/group-one/messages", payload())]
    assert [(method, url) for method, url, _ in instance.http.session.calls if "/messages" in url] == [
        ("POST", "https://api.bot.qq.com/v2/groups/group-one/messages")]


@pytest.mark.parametrize("mutation", [
    lambda p: p.update({"msg_id": "fake"}),
    lambda p: p.update({"content": "incompatible"}),
    lambda p: p["keyboard"].update({"id": "template"}),
    lambda p: p["keyboard"]["content"]["rows"].append(p["keyboard"]["content"]["rows"][0]),
    lambda p: p["keyboard"]["content"]["rows"][0]["buttons"][0]["action"].update({"enter": True}),
    lambda p: p["keyboard"]["content"]["rows"][0]["buttons"][0]["render_data"].update({"style": 2}),
    lambda p: p["keyboard"]["content"]["rows"][0]["buttons"][0]["action"].update({"type": 1, "data": "unowned"}),
])
@pytest.mark.asyncio
async def test_invalid_cards_never_send(card_receiver, mutation):
    _, instance, seen = card_receiver
    bad = copy.deepcopy(payload())
    mutation(bad)
    with pytest.raises(RuntimeError):
        await instance.client.send(instance.client.route_for("group", "group-one"), MessageChain([Json(bad)]))
    assert not seen


@pytest.mark.asyncio
async def test_card_must_be_the_only_component(card_receiver):
    _, instance, seen = card_receiver
    route = instance.client.route_for("group", "group-one")
    for chain in (MessageChain([Plain("prefix"), Json(payload())]),
                  MessageChain([Json(payload()), Json(payload())]),
                  MessageChain([Json(payload())]).use_markdown(True)):
        with pytest.raises(RuntimeError):
            await instance.client.send(route, chain)
    assert not seen


@pytest.mark.asyncio
async def test_template_text_card_c2c_and_bounded_markdown(card_receiver):
    _, instance, seen = card_receiver
    c2c = instance.client.route_for("c2c", "user-one")
    card = {"msg_type": 0, "content": "hello", "keyboard": {"id": "qq-template"}}
    await instance.client.send(c2c, MessageChain([Json(card)]))
    assert seen == [("POST", "/v2/users/user-one/messages", card)]
    huge = payload()
    huge["markdown"]["content"] = "a" * 4097
    with pytest.raises(RuntimeError) as error:
        await instance.client.send(c2c, MessageChain([Json(huge)]))
    assert error.value.code == "invalid_message" and len(seen) == 1


@pytest.mark.asyncio
async def test_native_view_and_host_session_send_share_card_path(card_receiver):
    _, instance, seen = card_receiver
    chain = MessageChain([Json(payload())])
    result = await instance.client.qq.send("group", "group-one", chain)
    assert result["state"] == "sent"
    session = instance.client.route_for("group", "group-one").public_session(instance.identity.platform_id)
    await instance.send_by_session(session, chain)
    assert len(seen) == 2
    assert all(method == "POST" and path == "/v2/groups/group-one/messages" for method, path, _ in seen)


@pytest.mark.asyncio
async def test_documented_button_fields_and_restricted_scenes(card_receiver):
    _, instance, seen = card_receiver
    route = instance.client.route_for("c2c", "user-one")
    item = payload()
    button = item["keyboard"]["content"]["rows"][0]["buttons"][0]
    button["render_data"].update(style=4, visited_label="已查看")
    button["action"].update(enter=True, reply=True, modal={"content": "继续吗？", "confirm_text": "确认", "cancel_text": "取消"})
    await instance.client.send(route, MessageChain([Json(item)]))
    assert seen[-1][2]["keyboard"] == item["keyboard"]
    button["action"].update(type=0, data="https://example.test/entry")
    button["action"].pop("enter")
    button["action"].pop("reply")
    await instance.client.send(route, MessageChain([Json(item)]))
    assert seen[-1][2]["keyboard"] == item["keyboard"]
    button["action"]["data"] = "mqqapi://app/open"
    await instance.client.send(route, MessageChain([Json(item)]))
    button["action"]["data"] = "javascript:alert(1)"
    with pytest.raises(RuntimeError):
        await instance.client.send(route, MessageChain([Json(item)]))
    assert len(seen) == 3
    with pytest.raises(RuntimeError) as error:
        await instance.client.send(instance.client.route_for("channel", "channel-one"), MessageChain([Json(payload())]))
    assert error.value.code == "unsupported" and len(seen) == 3


@pytest.mark.parametrize("mutate", [
    lambda button: button.update({"group_id": "g"}),
    lambda button: button["action"].update({"anchor": 1}),
    lambda button: button["action"].update({"modal": {"content": "HTTPS://site.test"}}),
    lambda button: button["action"].update({"click_limit": 1}),
    lambda button: button["action"].update({"permission": {"type": 0}}),
    lambda button: button["action"].update({"permission": {"type": 3}}),
    lambda button: button["render_data"].update({"unknown": "ignored?"}),
])
@pytest.mark.asyncio
async def test_invalid_schema_fields_cannot_partially_send(card_receiver, mutate):
    _, instance, seen = card_receiver
    item = payload()
    mutate(item["keyboard"]["content"]["rows"][0]["buttons"][0])
    with pytest.raises(RuntimeError):
        await instance.client.send(instance.client.route_for("group", "group-one"), MessageChain([Json(item)]))
    assert not seen


@pytest.mark.asyncio
async def test_card_reference_resolves_only_real_target_scoped_index(card_receiver):
    _, instance, seen = card_receiver
    route = instance.client.route_for("group", "group-one")
    item = payload()
    item["message_reference"] = {"message_id": "msg-one"}
    await instance.client.send(route, MessageChain([Json(item)]))
    assert seen[0][2]["message_reference"] == {"message_id": "REFIDX_msg-one"}
    item["message_reference"] = {"message_id": "unknown-message"}
    with pytest.raises(RuntimeError):
        await instance.client.send(route, MessageChain([Json(item)]))
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_card_operation_id_does_not_replay_conflicting_body(card_receiver):
    _, instance, seen = card_receiver
    route = instance.client.route_for("group", "group-one")
    await instance.client.send(route, MessageChain([Json(payload())]), operation_id="card-operation")
    other = payload()
    other["markdown"]["content"] = "different content"
    with pytest.raises(RuntimeError) as error:
        await instance.client.send(route, MessageChain([Json(other)]), operation_id="card-operation")
    assert error.value.code == "operation_conflict"
    assert len(seen) == 1
