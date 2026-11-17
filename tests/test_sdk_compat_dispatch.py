"""In-process native and OneBot-style calls share one V2Client."""
from types import SimpleNamespace

import pytest

from v2.client import ClientState, V2Client, public_native_method
from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey, SessionRoute

pytest_plugins = ("test_management",)


async def test_group_info_native_and_onebot_projections_differ(management):
    m = management
    client = V2Client(m.http.identity)
    client._state.management, client._state.http = m.service, m.http
    assert client.api is client
    native = await client.qq.get_group_info("g")
    compat = await client.api.call_action("get_group_info", group_id="g")
    same = await client.call_action("get_group_info", group_id="g")
    via_attr = await client.get_group_info(group_id="g")
    bound = client.bind(SessionRoute(client.identity.robot, "group", "g"))
    assert bound.api is bound and bound._state is client._state
    bound_compat = await bound.api.call_action("get_group_info", group_id="g")
    assert native["group_openid"] == "g" and native["group_member_num"] == 2
    assert "group_id" not in native and "member_count" not in native
    assert compat == same == via_attr == bound_compat
    assert compat["group_id"] == "g" and compat["member_count"] == 2
    assert compat["partial"] is True and compat["_qq"]["source"] == "official_query"
    assert client.capabilities()["actions"]["get_group_info"]["support"] == "conditional"
    assert client.capabilities()["actions"]["get_login_info"]["support"] == "conditional"


async def test_login_info_uses_native_me_with_onebot_projection(management):
    m = management
    client = V2Client(m.http.identity)
    client._state.http = m.http
    native = await client.qq.me()
    compat = await client.api.call_action("get_login_info")
    same = await client.call_action("get_login_info")
    via_attr = await client.get_login_info()
    assert native["id"] == "actual-bot-id" and native["bot"] is True
    assert "user_id" not in native
    assert compat == same == via_attr
    assert compat["user_id"] == "actual-bot-id" and compat["nickname"] == "actual-bot-name"
    assert compat["id_kind"] == "channel_user_id" and compat["source"] == "official_profile"


async def test_call_action_forwards_public_native_methods():
    identity = InstanceKey("p", RobotKey("a"))
    client = V2Client(identity, state=ClientState(identity))
    captured = []

    async def request(spec, before_send=None):
        if before_send:
            before_send()
        captured.append(spec.path)
        if spec.path.endswith("/info"):
            return SimpleNamespace(data={"username": "Nick", "avatar": "https://a.test/x", "allow_proactive_msg": False})
        return SimpleNamespace(data={"strategies": [], "next_cursor": ""})

    client._state.http = SimpleNamespace(request=request)
    native = await client.qq.get_user_info("u/1")
    forwarded = await client.call_action("get_user_info", user_openid="u/1")
    via_api = await client.api.call_action("get_user_info", user_openid="u/1")
    via_attr = await client.get_user_info(user_openid="u/1")
    assert native == forwarded == via_api == via_attr
    assert native["username"] == "Nick"
    strategies = await client.call_action("get_join_approval_strategies")
    assert strategies == await client.qq.get_join_approval_strategies()
    assert captured[0] == "/v2/users/u%2F1/info"
    assert captured[-1] == "/v2/groups/join_approval_strategy"
    assert client.capabilities()["actions"]["get_group_info"]["support"] == "conditional"


async def test_get_msg_is_not_channel_get_message():
    identity = InstanceKey("p", RobotKey("a"))
    client = V2Client(identity, state=ClientState(identity))
    captured = []

    async def request(spec, before_send=None):
        if before_send:
            before_send()
        captured.append(spec.path)
        return SimpleNamespace(data={"id": "m", "content": "quoted"})

    client._state.http = SimpleNamespace(request=request)
    native = await client.qq.get_message("c", "m")
    forwarded = await client.call_action("get_message", channel_id="c", message_id="m")
    via_api = await client.api.call_action("get_message", channel_id="c", message_id="m")
    via_attr = await client.get_message(channel_id="c", message_id="m")
    assert native == forwarded == via_api == via_attr == {"id": "m", "content": "quoted"}
    assert captured == ["/channels/c/messages/m"] * 4
    with pytest.raises(V2Error) as history:
        await client.call_action("get_msg", message_id="m")
    assert history.value.code == "history_not_ready"
    with pytest.raises(V2Error) as forward:
        await client.api.call_action("get_forward_msg", id="m")
    assert forward.value.code == "history_not_ready"
    assert client.capabilities()["actions"]["get_msg"]["support"] == "unsupported"
    assert client.capabilities()["actions"]["get_msg"]["reason"] == "history_not_ready"
    assert client.capabilities()["actions"]["get_forward_msg"]["reason"] == "history_not_ready"


async def test_unsupported_private_and_removed_network_params():
    identity = InstanceKey("p", RobotKey("a"))
    client = V2Client(identity, state=ClientState(identity))
    client._state.sender = SimpleNamespace(media=None)
    assert client.api is client
    with pytest.raises(V2Error) as missing:
        await client.call_action("get_friend_list")
    assert missing.value.code == "unsupported"
    with pytest.raises(V2Error) as private_forward:
        await client.send_private_forward_msg(user_id="u", messages=[])
    assert private_forward.value.code == "unsupported"
    assert client.capabilities()["actions"]["send_private_forward_msg"]["support"] == "unsupported"
    with pytest.raises(V2Error) as private:
        await client.call_action("_check")
    assert private.value.code == "unsupported"
    with pytest.raises(V2Error) as iterator:
        await client.call_action("iter_group_members", group_openid="g")
    assert iterator.value.code == "unsupported"
    with pytest.raises(V2Error) as reply:
        await client.call_action("send_group_msg", group_id="g", message="x", _qq_reply_context="ticket")
    assert reply.value.code == "invalid_params"
    assert public_native_method(client.qq, "events") is None
    assert public_native_method(client.qq, "_check") is None
    assert client.capabilities()["network_api"] == {"support": "removed", "state": "not_provided"}
    assert "network" not in client.capabilities()["actions"]["get_status"]["returns"]
    assert "network" not in (await client.get_status())
    assert client.capabilities()["actions"]["get_group_info"]["reason"] == "transport_not_ready"


async def test_event_bot_api_delete_msg_uses_management_and_sent_route(management):
    m = management
    client = V2Client(m.http.identity)
    client._state.management, client._state.http = m.service, m.http
    seen = []
    real = m.service.delete_message

    async def wrapped(message_id, *, operation_id=None, guard=None):
        seen.append({"message_id": message_id, "operation_id": operation_id, "guard": guard})
        return await real(message_id, operation_id=operation_id, guard=guard)

    m.service.delete_message = wrapped

    def seed_sent(message_id):
        route = client.route_for("group", "g")
        op_id = "seed-" + message_id
        m.store.reserve(route, None, "fixture-message", op_id)
        m.store.finish(route.robot, op_id, "sent", result={"message_id": message_id, "wire_started": m.store.now()})
        return route

    def deletes():
        return [row for row in m.calls if row[0] == "DELETE"]

    seed_sent("sent-msg")
    event = SimpleNamespace(
        bot=client.bind(SessionRoute(client.identity.robot, "group", "g")),
        message_obj=SimpleNamespace(message_id="sent-msg"),
    )
    assert event.bot.api is event.bot
    payloads = {"message_id": event.message_obj.message_id}
    result = await event.bot.api.call_action("delete_msg", **payloads)
    assert result["state"] == "succeeded"
    assert seen[-1]["message_id"] == "sent-msg" and seen[-1]["guard"] is not None
    assert seen[-1]["guard"].__self__ is event.bot
    assert deletes()[-1][1] == "/v2/groups/g/messages/sent-msg"
    assert client.capabilities()["actions"]["delete_msg"]["support"] == "conditional"

    inbound = SimpleNamespace(message_id="inbound-user")
    event.message_obj = inbound
    before = len(deletes())
    with pytest.raises(V2Error) as missing:
        await event.bot.api.call_action("delete_msg", message_id=event.message_obj.message_id)
    assert missing.value.code == "message_route_unavailable"
    assert len(deletes()) == before

    seed_sent("old-msg")
    m.clock[0] += 121
    with pytest.raises(V2Error) as expired:
        await event.bot.api.call_action("delete_msg", message_id="old-msg")
    assert expired.value.code == "delete_expired"
    assert len(deletes()) == before

    seed_sent("unk-msg")
    m.modes[:] = ["delete_unknown"]
    with pytest.raises(V2Error) as unknown:
        await event.bot.api.call_action("delete_msg", message_id="unk-msg", _qq_operation_id="del-unknown")
    assert unknown.value.phase == "result_unknown"
    assert m.state.operation(client.identity.robot, "del-unknown")["state"] == "unknown"
    unknown_deletes = len(deletes())
    with pytest.raises(V2Error) as replay:
        await event.bot.api.call_action("delete_msg", message_id="unk-msg", _qq_operation_id="del-unknown")
    assert replay.value.code == "extension_result_unknown"
    assert len(deletes()) == unknown_deletes
    m.modes.clear()

    seed_sent("gated-msg")
    m.policy["management_writes"] = False
    with pytest.raises(V2Error) as disabled:
        await event.bot.api.call_action("delete_msg", message_id="gated-msg")
    assert disabled.value.code == "management_disabled"
    m.policy["management_writes"] = True
    assert len(deletes()) == unknown_deletes

    seed_sent("stale-msg")
    client._state.closed = True
    with pytest.raises(V2Error) as stale:
        await event.bot.api.call_action("delete_msg", message_id="stale-msg")
    assert stale.value.code == "stale_generation"
    assert len(deletes()) == unknown_deletes
