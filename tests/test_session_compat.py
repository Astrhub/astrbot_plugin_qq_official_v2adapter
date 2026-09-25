"""Public native UMOs retain independently scoped QQ routes and send policy."""
import base64
from dataclasses import replace
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from test_interactions import interaction, tickets as tickets
from test_messaging_send import sending as sending
from test_messaging_state import NOW, chat_payload

from v2 import PLATFORM_TYPE
from v2.adapter import V2Adapter
from v2.client import V2Client
from v2.errors import V2Error
from v2.event import V2MessageEvent
from v2.extensions.events import ExtensionEvent
from v2.extensions.projection import CommandProjection
from v2.messaging.convert import convert_chat
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, RobotKey, SessionRoute
from v2.protocol import RawEnvelope


@pytest.mark.parametrize("name,target,sender,isolated,umo", [
    ("GROUP_MESSAGE_CREATE", "group-one", "user-one", False, "官机:GroupMessage:group-one"),
    ("GROUP_AT_MESSAGE_CREATE", "group-one", "user-one", False, "官机:GroupMessage:group-one"),
    ("GROUP_AT_MESSAGE_CREATE", "group-one", "user-one", True, "官机:GroupMessage:user-one_group-one"),
    ("C2C_MESSAGE_CREATE", "ignored", "user-one", False, "官机:FriendMessage:user-one"),
    ("AT_MESSAGE_CREATE", "channel-one", "channel-user", False, "官机:GroupMessage:channel-one"),
    ("MESSAGE_CREATE", "channel-one", "channel-user", True, "官机:GroupMessage:channel-user_channel-one"),
    ("DIRECT_MESSAGE_CREATE", "dm-guild", "channel-user", False, "官机:FriendMessage:channel-user"),
])
def test_public_event_umo_is_native_and_source_route_remains_private(config, name, target, sender, isolated, umo):
    identity = InstanceKey.from_config({**config, "id": "官机"})
    payload = chat_payload(name, target=target, sender=sender)
    chat = convert_chat(identity, RawEnvelope(payload, NOW), isolated=isolated)
    event = V2MessageEvent(chat.message, PlatformMetadata(PLATFORM_TYPE, "test", identity.platform_id), V2Client(identity), chat.route)
    assert event.unified_msg_origin == umo
    assert event.get_session_id() == event.session_id == event.message_obj.session_id == umo.split(":", 2)[2]
    assert event.route == event.bot._source.route == chat.source.route
    assert event.route.target == (sender if name == "C2C_MESSAGE_CREATE" else target)
    assert SessionRoute.decode(event.route.encode()) == event.route
    assert event.raw_data == payload and event.bot._source.generation == identity.generation


@pytest.fixture
def adapter(sending):
    instance = object.__new__(V2Adapter)
    instance.identity = sending.client.identity
    instance.client = sending.client
    instance.owner = SimpleNamespace(messages=sending.store)
    return instance


@pytest.mark.parametrize("name,target,sender,public_id,message_type,path", [
    ("GROUP_MESSAGE_CREATE", "same:id/one", "user-one", "same:id/one", MessageType.GROUP_MESSAGE, "/v2/groups/same:id/one/messages"),
    ("C2C_MESSAGE_CREATE", "unused", "user-one", "user-one", MessageType.FRIEND_MESSAGE, "/v2/users/user-one/messages"),
    ("MESSAGE_CREATE", "channel-one", "channel-user", "channel-one", MessageType.GROUP_MESSAGE, "/channels/channel-one/messages"),
    ("DIRECT_MESSAGE_CREATE", "dm-guild", "channel-user", "channel-user", MessageType.FRIEND_MESSAGE, "/dms/dm-guild/messages"),
])
@pytest.mark.parametrize("legacy", [False, True])
async def test_standard_and_legacy_sessions_send_to_observed_actual_http_target(sending, adapter, name, target, sender, public_id, message_type, path, legacy):
    chat, _ = sending.observe(name, target=target, sender=sender)
    session = MessageSession("test-v2", message_type, chat.route.encode() if legacy else public_id)
    await adapter.send_by_session(session, MessageChain([Plain("active")]))
    assert sending.calls == [(path, {"content": "active", **({"msg_type": 0} if name in {"GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"} else {})})]
    assert sending.http.session.calls[-1][1] == "https://api.bot.qq.com" + (path.replace("same:id/one", "same%3Aid%2Fone"))
    assert sending.store.db.execute("SELECT source FROM operations").fetchone()[0] is None


async def test_serialized_isolated_session_resolves_observed_member_after_another_sender(sending, adapter):
    first, _ = sending.observe(sender="first_user", target="group_with_underscores")
    sending.observe(sender="second", target="group_with_underscores", message_id="second")
    await adapter.send_by_session(MessageSession("test-v2", MessageType.GROUP_MESSAGE, "first_user_group_with_underscores"), MessageChain([Plain("active")]))
    assert sending.calls[0][0] == "/v2/groups/group_with_underscores/messages"
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(MessageSession("test-v2", MessageType.GROUP_MESSAGE, "invented_group_with_underscores"), MessageChain([Plain("no")]))
    assert exc.value.code == "identity_not_observed" and len(sending.calls) == 1
    assert first.source.message_id == "msg-one"


@pytest.mark.parametrize("collision", ["group_channel", "c2c_dm", "dm_dm", "isolated_target"])
async def test_ambiguous_public_sessions_fail_without_guessing_and_bound_event_stays_exact(sending, adapter, collision):
    if collision == "group_channel":
        first, _ = sending.observe(target="collision")
        sending.observe("MESSAGE_CREATE", target="collision", message_id="second")
        mt, sid, path = MessageType.GROUP_MESSAGE, "collision", "/v2/groups/collision/messages"
    elif collision == "isolated_target":
        first, _ = sending.observe(target="user_group")
        sending.observe(target="group", sender="user", message_id="second")
        mt, sid, path = MessageType.GROUP_MESSAGE, "user_group", "/v2/groups/user_group/messages"
    else:
        first, _ = sending.observe("C2C_MESSAGE_CREATE" if collision == "c2c_dm" else "DIRECT_MESSAGE_CREATE", target="guild-one", sender="collision")
        sending.observe("DIRECT_MESSAGE_CREATE", target="guild-two", sender="collision", message_id="second")
        mt, sid = MessageType.FRIEND_MESSAGE, "collision"
        path = "/v2/users/collision/messages" if collision == "c2c_dm" else "/dms/guild-one/messages"
    session = MessageSession("test-v2", mt, sid)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(session, MessageChain([Plain("no")]))
    assert exc.value.code == "ambiguous_session" and not sending.calls
    event = adapter.create_event(first.message)
    assert event.session_id == sid
    await adapter.send_by_session(event.session, MessageChain([Plain("bound active")]))
    assert sending.calls[0][0] == path and "msg_id" not in sending.calls[0][1]


@pytest.mark.parametrize("case", ["unknown", "foreign_bot", "foreign_environment", "foreign_platform", "wrong_type", "unknown_type", "unknown_scene", "expired"])
async def test_session_route_rejections_precede_http(sending, adapter, case):
    chat, _ = sending.observe()
    sid, mt, platform = "group-one", MessageType.GROUP_MESSAGE, "test-v2"
    if case == "unknown":
        sid = "never-observed"
    elif case in {"foreign_bot", "foreign_environment"}:
        robot = RobotKey("another-app") if case == "foreign_bot" else RobotKey(chat.route.robot.appid, "sandbox")
        sid = SessionRoute(robot, "group", "group-one").encode()
    elif case == "foreign_platform":
        platform = "官机"
    elif case == "wrong_type":
        sid, mt = chat.route.encode(), MessageType.FRIEND_MESSAGE
    elif case == "unknown_scene":
        sid = "v2." + base64.urlsafe_b64encode(b'["test-app","production","unknown","group-one",null]').decode().rstrip("=")
    elif case == "unknown_type":
        mt = MessageType.OTHER_MESSAGE
    else:
        sending.clock[0] += 86401
    with pytest.raises(V2Error):
        await adapter.send_by_session(MessageSession(platform, mt, sid), MessageChain([Plain("no")]))
    assert not sending.calls


async def test_other_robot_and_environment_observations_are_not_public_route_evidence(sending, adapter, config):
    for cfg in ({**config, "appid": "another-app"}, {**config, "environment": "sandbox"}):
        chat = convert_chat(InstanceKey.from_config(cfg), RawEnvelope(chat_payload(target="unowned"), NOW))
        sending.store.observe(chat)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(MessageSession("test-v2", MessageType.GROUP_MESSAGE, "unowned"), MessageChain([Plain("no")]))
    assert exc.value.code == "identity_not_observed" and not sending.calls


async def test_bound_event_session_cannot_change_owner_generation_or_public_target(sending, adapter, config):
    chat, _ = sending.observe()
    event = adapter.create_event(chat.message)
    event.session.session_id = "another"
    with pytest.raises(V2Error):
        await adapter.send_by_session(event.session, MessageChain([Plain("no")]))
    event = adapter.create_event(chat.message)
    other = object.__new__(V2Adapter)
    other.identity = InstanceKey.from_config(config)
    other.client, other.owner = sending.client, adapter.owner
    with pytest.raises(V2Error) as exc:
        await other.send_by_session(event.session, MessageChain([Plain("no")]))
    assert exc.value.code == "stale_generation" and not sending.calls


@pytest.mark.parametrize("scene,isolated,expected", [
    ("group", False, "test-v2:GroupMessage:group-one"),
    ("group", True, "test-v2:GroupMessage:user-one_group-one"),
    ("c2c", True, "test-v2:FriendMessage:user-one"),
])
def test_projection_keyboard_and_host_configuration_share_public_umo(tickets, config, scene, isolated, expected):
    s = tickets
    lookups = []
    selected = {"wake_prefix": ["!"], "admins_id": []}
    s.service.adapter.owner.context.get_config = lambda umo: lookups.append(umo) or selected
    adapter = s.service.adapter
    adapter.session_isolated = isolated
    adapter.bot_id = "real-bot"
    adapter.client = V2Client(s.identity)
    adapter.meta = lambda: PlatformMetadata(PLATFORM_TYPE, "test", s.identity.platform_id)
    payload = interaction(config)
    if scene == "c2c":
        payload["d"].update(scene="c2c", user_openid="user-one")
        del payload["d"]["group_openid"], payload["d"]["group_member_openid"]
    event = ExtensionEvent.parse(s.identity, payload, NOW)
    route = event.route(s.identity, isolated=isolated)
    projection = CommandProjection(adapter, event, {"command": "!run", "handler": "fixture.command"}, s.service)
    assert projection.unified_msg_origin == expected and projection.message_obj.session_id == expected.split(":", 2)[2]
    assert s.service.config(route) is selected and lookups == [expected]
    assert projection.bot._source.route == route and projection.raw_data["derived_from_interaction"] is True
    assert not s.store.db.execute("SELECT 1 FROM targets").fetchone()


def test_channel_keyboard_configuration_is_not_misclassified_as_c2c(tickets):
    seen = []
    tickets.service.adapter.owner.context.get_config = lambda umo: seen.append(umo) or {}
    tickets.service.config(SessionRoute(tickets.identity.robot, "channel", "channel-one", "channel-user"))
    assert seen == ["test-v2:GroupMessage:channel-user_channel-one"]


async def test_event_uses_bound_source_not_mutable_public_message_id(sending, adapter):
    chat, _ = sending.observe()
    chat.message.session_id = "v2.not-a-route"
    event = adapter.create_event(chat.message)
    assert event.unified_msg_origin == "test-v2:GroupMessage:group-one"
    assert event.route == chat.source.route and event.bot._source is chat.source
    chat.message.v2_source = replace(chat.source, generation="old-generation")
    with pytest.raises(V2Error) as exc:
        adapter.create_event(chat.message)
    assert exc.value.code == "stale_generation" and not sending.calls


async def test_identical_digit_ids_are_not_conflated_across_native_message_types(sending, adapter):
    sending.observe(target="001234")
    sending.observe("C2C_MESSAGE_CREATE", sender="001234", message_id="private")
    await adapter.send_by_session(MessageSession.from_str("test-v2:GroupMessage:001234"), MessageChain([Plain("group")]))
    await adapter.send_by_session(MessageSession.from_str("test-v2:FriendMessage:001234"), MessageChain([Plain("private")]))
    assert [call[0] for call in sending.calls] == ["/v2/groups/001234/messages", "/v2/users/001234/messages"]
    assert [call[1] for call in sending.http.session.calls] == [
        "https://api.bot.qq.com/app/getAppAccessToken",
        "https://api.bot.qq.com/v2/groups/001234/messages", "https://api.bot.qq.com/v2/users/001234/messages"]


def test_persisted_observations_resolve_without_schema_changes_or_touching_unknown(config, tmp_path):
    identity = InstanceKey.from_config(config)
    chat = convert_chat(identity, RawEnvelope(chat_payload(), NOW))
    path = tmp_path / "existing-state"
    store = MessageStore(path, clock=lambda: NOW)
    try:
        store.observe(chat)
        store.reserve(chat.route, chat.source, "digest", "preserved")
        store.mark_in_flight(identity.robot, "preserved")
        store.finish(identity.robot, "preserved", "unknown")
        before = list(store.db.iterdump())
    finally:
        store.close()
    store = MessageStore(path, clock=lambda: NOW)
    try:
        for session_id in ("group-one", "user-one_group-one", chat.route.encode()):
            assert store.resolve_session(identity.robot, MessageType.GROUP_MESSAGE, session_id).target == "group-one"
        assert list(store.db.iterdump()) == before and store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        with pytest.raises(V2Error) as exc:
            store.reserve(chat.route, chat.source, "digest", "preserved")
        assert exc.value.code == "send_result_unknown"
    finally:
        store.close()
