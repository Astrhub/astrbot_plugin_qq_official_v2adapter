"""Callback execution observes the host's effective session gates."""
from aiohttp import web
from astrbot.core import sp
from astrbot.core.message.components import Json
from astrbot.core.message.message_event_result import MessageChain
from test_button_callbacks import callback_case as callback_case
from test_button_callbacks import card, settle
from test_extension_dispatch import dispatch as dispatch
from test_interactions import interaction
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import accept
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW
from test_transport_http import MappedSession, upstream


async def test_group_callback_uses_current_profile_allowlist_and_real_host_admin_exemption(callback_case, monkeypatch):
    s, event, star, seen = callback_case
    config = s.owner.context.get_config()
    umo = event.unified_msg_origin
    profile = {**config, "admins_id": [], "platform_settings": {
        "enable_id_white_list": True, "id_whitelist": ["another-group"],
        "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False, "id_whitelist_log": False,
    }}
    monkeypatch.setattr(s.owner.context, "get_config", lambda target=None: profile if target == umo else config)

    button = event.qq.callback_button(star.direct, label="确认", data="denied")
    await event.send(MessageChain([Json(card(button))]))
    s.service.accept(interaction(s.config, token=button["action"]["data"]), NOW)
    await settle(s)
    assert seen == [] and s.service.records()[0]["error"]["code"] == "callback_session_denied"
    assert len([method for method, _, _ in s.calls if method == "POST"]) == 1

    # Host administrators are exempt only when the matching host switch is enabled.
    profile["admins_id"] = ["user-one"]
    profile["platform_settings"]["wl_ignore_admin_on_group"] = True
    button = event.qq.callback_button(star.direct, label="确认", data="admin-allowed")
    await event.send(MessageChain([Json(card(button))]))
    s.service.accept(interaction(s.config, token=button["action"]["data"], interaction_id="host-admin", event_id="admin-envelope"), NOW)
    await settle(s)
    assert seen == ["admin-allowed"] and s.service.records()[0]["business"] == "finished_unconfirmed"

    # Session profile overrides the global profile, as in the real host routing contract.
    profile["admins_id"] = []
    profile["platform_settings"]["id_whitelist"] = [umo]
    config["platform_settings"]["id_whitelist"] = ["unrelated-global"]
    button = event.qq.callback_button(star.direct, label="确认", data="profile-allowed")
    await event.send(MessageChain([Json(card(button))]))
    s.service.accept(interaction(s.config, token=button["action"]["data"], interaction_id="profile", event_id="profile-envelope"), NOW)
    await settle(s)
    assert seen == ["admin-allowed", "profile-allowed"]


async def test_group_callback_obeys_session_off_and_session_plugin_disable(callback_case):
    s, event, star, seen = callback_case
    umo = event.unified_msg_origin
    button = event.qq.callback_button(star.direct, label="确认", data="session-off")
    await event.send(MessageChain([Json(card(button))]))
    await sp.put_async("umo", umo, "session_service_config", {"session_enabled": False})
    try:
        s.service.accept(interaction(s.config, token=button["action"]["data"]), NOW)
        await settle(s)
        assert not seen and s.service.records()[0]["error"]["code"] == "callback_session_disabled"
    finally:
        await sp.remove_async("umo", umo, "session_service_config")

    button = event.qq.callback_button(star.direct, label="确认", data="plugin-off")
    await event.send(MessageChain([Json(card(button))]))
    await sp.put_async("umo", umo, "session_plugin_config", {umo: {"disabled_plugins": ["third-party-buttons"]}})
    try:
        s.service.accept(interaction(s.config, token=button["action"]["data"], interaction_id="session-plugin", event_id="session-plugin-envelope"), NOW)
        await settle(s)
        assert not seen and s.service.records()[0]["business"] == "rejected"
    finally:
        await sp.remove_async("umo", umo, "session_plugin_config")


async def test_c2c_callback_obeys_friend_allowlist_and_host_admin_exemption(callback_case):
    s, _, star, seen = callback_case
    accept(s.owner, s.instance, event="C2C_MESSAGE_CREATE", target="user-one", message_id="friend-gate")
    assert s.instance.consumer.step()
    event = s.instance._event_queue.get_nowait()
    calls = []
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "friend-gate-fixture", "expires_in": 7200})
        calls.append((request.method, request.path, await request.json()))
        return web.Response(status=204) if request.method == "PUT" else web.json_response({"id": "friend-gate-message"})
    def click(token, interaction_id):
        frame = interaction(s.config, token=token, interaction_id=interaction_id, event_id=f"{interaction_id}-envelope")
        frame["d"].update(scene="c2c", chat_type=2, user_openid="user-one")
        frame["d"].pop("group_openid")
        frame["d"].pop("group_member_openid")
        return frame
    try:
        async with upstream(handler) as base:
            s.instance.http._factory = lambda: MappedSession(base)
            s.instance.ack_http._factory = lambda: MappedSession(base)
            button = event.qq.callback_button(star.direct, label="执行", data="denied-friend")
            await event.send(MessageChain([Json(card(button))]))
            config = s.owner.context.get_config()
            config["platform_settings"] = {
                "enable_id_white_list": True, "id_whitelist": ["another-user"],
                "wl_ignore_admin_on_group": False, "wl_ignore_admin_on_friend": False, "id_whitelist_log": False,
            }
            s.service.accept(click(button["action"]["data"], "friend-denied"), NOW)
            await settle(s)
            assert not seen and s.service.records()[0]["error"]["code"] == "callback_session_denied"
            assert [(method, path) for method, path, _ in calls] == [
                ("POST", "/v2/users/user-one/messages"), ("PUT", "/interactions/friend-denied")]
            config["admins_id"] = ["user-one"]
            config["platform_settings"]["wl_ignore_admin_on_friend"] = True
            button = event.qq.callback_button(star.direct, label="执行", data="allowed-friend")
            await event.send(MessageChain([Json(card(button))]))
            s.service.accept(click(button["action"]["data"], "friend-admin"), NOW)
            await settle(s)
            assert seen == ["allowed-friend"] and s.service.records()[0]["business"] == "finished_unconfirmed"
            assert calls[-1][1] == "/v2/users/user-one/messages"
    finally:
        event.cleanup_temporary_local_files()
