"""Group self mentions wake the real host stage without an addressed handler."""
import copy

import pytest
from astrbot.core.message.components import At, Plain
from astrbot.core.pipeline.context import PipelineContext
from astrbot.core.pipeline.waking_check import stage as waking
from astrbot.core.platform.platform_metadata import PlatformMetadata
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.star_handler import StarHandlerRegistry
from test_messaging_state import NOW, chat_payload

from v2 import PLATFORM_TYPE, PLUGIN_NAME
from v2.client import V2Client
from v2.errors import V2Error
from v2.event import V2MessageEvent
from v2.messaging.convert import convert_chat
from v2.messaging.store import MessageStore
from v2.models import InstanceKey
from v2.protocol import RawEnvelope

READY_ID = "1234567890123456789"
GROUP_BOT = "0123456789ABCDEF0123456789ABCDEF"
GROUP_MEMBER = "FEDCBA9876543210FEDCBA9876543210"


@pytest.fixture
async def host_waking(monkeypatch):
    # Use the host's real stage with no plugin filters capable of forcing wake.
    monkeypatch.setattr(waking, "star_handlers_registry", StarHandlerRegistry())
    config = {"platform_settings": {}, "wake_prefix": ["/"], "admins_id": [],
              "plugin_set": ["another-plugin"]}
    stage = waking.WakingCheckStage()
    await stage.initialize(PipelineContext(config, None, "mention-test"))
    return stage


def event_from(config, payload, *, bot_id=READY_ID):
    identity = InstanceKey.from_config(config)
    chat = convert_chat(identity, RawEnvelope(payload, NOW), isolated=True, bot_id=bot_id)
    event = V2MessageEvent(chat.message, PlatformMetadata(PLATFORM_TYPE, "QQ V2", id=identity.platform_id),
                           V2Client(identity), chat.route)
    return chat, event


@pytest.mark.parametrize("mention,expected_id", [
    ({"id": GROUP_BOT, "member_openid": GROUP_BOT, "is_you": True}, GROUP_BOT),
    ({"id": GROUP_BOT, "is_you": True}, GROUP_BOT),
    ({"member_openid": GROUP_MEMBER, "is_you": True}, GROUP_MEMBER),
    ({"id": GROUP_BOT, "member_openid": GROUP_MEMBER, "is_you": True}, GROUP_BOT),
    ({"id": "01234567890123456789012345678901", "is_you": True}, "01234567890123456789012345678901"),
])
@pytest.mark.parametrize("bot_id", [READY_ID, ""])
@pytest.mark.parametrize("marker", ["<@{}>", "<@!{}>"])
async def test_structured_group_self_mention_wakes_host_and_matches_command(config, host_waking, mention, expected_id, bot_id, marker):
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=marker.format(expected_id) + " help")
    payload["d"]["mentions"] = [mention]
    chat, event = event_from(config, payload, bot_id=bot_id)
    assert not event.is_at_or_wake_command  # Constructor's explicit AT-event path cannot rescue this test.
    await host_waking.process(event)
    assert event.is_wake and not event.is_stopped()
    assert PLUGIN_NAME not in event.plugins_name and not event.get_extra("activated_handlers")
    assert event.is_at_or_wake_command and event.get_message_str() == "help"
    command = CommandFilter("help")
    command.handler_params = {}
    assert command.filter(event, host_waking.ctx.astrbot_config)
    assert chat.message.self_id == expected_id != READY_ID
    assert [part.qq for part in chat.message.message if isinstance(part, At)] == [expected_id]
    assert chat.message.message_str == " help" and event.raw_data == payload


@pytest.mark.parametrize("mentions,text", [
    ([], "你好"),
    ([], f"<@{GROUP_BOT}> 你好"),
    ([], f"<@!{READY_ID}> 你好"),
    ([{"id": GROUP_BOT, "member_openid": GROUP_BOT}], f"<@{GROUP_BOT}> 你好"),
    ([{"id": GROUP_BOT, "member_openid": GROUP_BOT, "bot": True, "username": "same-name"}], f"<@{GROUP_BOT}> 你好"),
    ([{"id": "another-user", "member_openid": "another-user", "is_you": False}], "<@another-user> 你好"),
    ([{"is_you": True, "username": "same-name", "bot": True}], f"<@{GROUP_BOT}> 你好"),
    ([{"user_openid": GROUP_BOT, "is_you": True}], f"<@{GROUP_BOT}> 你好"),
])
async def test_unconfirmed_group_mentions_do_not_wake_host(config, host_waking, mentions, text):
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=text)
    payload["d"]["mentions"] = mentions
    chat, event = event_from(config, payload)
    await host_waking.process(event)
    assert event.is_stopped() and not event.is_wake and not event.is_at_or_wake_command
    assert chat.message.self_id == READY_ID and chat.message.message_str == text
    assert event.raw_data == payload


@pytest.mark.parametrize("flag", [False, None, 0, 1, "true", "false", [], {}])
async def test_is_you_requires_literal_true(config, host_waking, flag):
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=f"<@{GROUP_BOT}> help")
    payload["d"]["mentions"] = [{"id": GROUP_BOT, "member_openid": GROUP_BOT, "bot": True, "is_you": flag}]
    chat, event = event_from(config, payload)
    await host_waking.process(event)
    assert event.is_stopped() and not event.is_wake
    assert chat.message.self_id == READY_ID and chat.message.message_str == payload["d"]["content"]


def test_group_self_cleanup_preserves_other_content_source_and_typed_identity(config, tmp_path):
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=f"前文  <@{GROUP_BOT}> help\n<@!{GROUP_MEMBER}> <@other> &amp; <@fake>  后文")
    payload["d"]["mentions"] = [
        {"id": GROUP_BOT, "member_openid": GROUP_MEMBER, "is_you": True},
        {"id": "other-token", "member_openid": "other", "is_you": False},
    ]
    before = copy.deepcopy(payload)
    chat, event = event_from(config, payload)
    assert chat.message.message_str == "前文   help\n <@other> &amp; <@fake>  后文"
    assert "".join(p.text for p in chat.message.message if isinstance(p, Plain)) == chat.message.message_str
    assert {p.qq for p in chat.message.message if isinstance(p, At)} == {GROUP_BOT, "other"}
    assert chat.message.self_id == GROUP_BOT
    assert {o["user_id"] for o in chat.observations} == {"user-one", GROUP_MEMBER, "other"}
    assert payload == before == event.raw_data == chat.message.raw_message
    assert chat.message.raw_message is not payload and chat.message.raw_message["d"] is not payload["d"]
    assert event.bot._source is chat.source and event.bot._route == chat.route
    assert chat.route.scene == "group" and chat.route.target == "group-one" and chat.route.user == "user-one"
    assert chat.source.message_id == "msg-one" and chat.source.event_id == "event-msg-one"
    assert chat.source.ref_idx == "REFIDX_msg-one" and chat.source.expires == NOW + 300
    state = MessageStore(tmp_path / "group-mentions.sqlite3", clock=lambda: NOW)
    try:
        state.observe(chat)
        assert state.lookup(chat.identity.robot, "member_openid", "group:group-one", GROUP_MEMBER)["source_message_id"] == "msg-one"
        with pytest.raises(V2Error) as exc:
            state.lookup(chat.identity.robot, "member_openid", "group:group-one", GROUP_BOT)
        assert exc.value.code == "identity_not_observed"
        assert state.reserve(chat.route, chat.source, "reply", "mention-reply")["seq"] == 1
    finally:
        state.close()


async def test_id_only_self_mention_does_not_become_member_cache_or_future_self_id(config, host_waking, tmp_path):
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=f"<@{GROUP_BOT}> 你好")
    payload["d"]["mentions"] = [{"id": GROUP_BOT, "is_you": True}]
    chat, event = event_from(config, payload)
    await host_waking.process(event)
    assert event.is_wake and event.get_self_id() == GROUP_BOT
    assert {o["user_id"] for o in chat.observations} == {"user-one"}
    state = MessageStore(tmp_path / "id-only.sqlite3", clock=lambda: NOW)
    try:
        state.observe(chat)
        with pytest.raises(V2Error) as exc:
            state.lookup(chat.identity.robot, "member_openid", "group:group-one", GROUP_BOT)
        assert exc.value.code == "identity_not_observed"
    finally:
        state.close()
    followup = chat_payload("GROUP_MESSAGE_CREATE", message_id="next", text=f"<@{GROUP_BOT}> 你好")
    followup["d"]["mentions"] = [{"member_openid": GROUP_BOT, "is_you": False}]
    next_chat, next_event = event_from(config, followup)
    await host_waking.process(next_event)
    assert next_chat.message.self_id == READY_ID and not next_event.is_wake


@pytest.mark.parametrize("text", ["你好", ""])
async def test_structured_self_mention_wakes_even_if_server_already_removed_marker(config, host_waking, text):
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=text)
    payload["d"]["mentions"] = [{"id": GROUP_BOT, "is_you": True}]
    chat, event = event_from(config, payload)
    await host_waking.process(event)
    assert event.is_wake and chat.message.message_str == text
    assert event.get_self_id() == GROUP_BOT


@pytest.mark.parametrize("event_name", ["AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
async def test_channel_and_dm_keep_ready_id_and_tag_parsing(config, host_waking, event_name):
    payload = chat_payload(event_name, text=f'<@!{READY_ID}> help <qqbot-at-user id="channel-other"/> &amp;')
    payload["d"]["mentions"] = [{"id": "channel-other", "member_openid": GROUP_BOT, "is_you": True}]
    chat, event = event_from(config, payload)
    await host_waking.process(event)
    assert event.is_wake and chat.message.self_id == READY_ID
    assert [str(p.qq) for p in chat.message.message if isinstance(p, At)] == [READY_ID, "channel-other"]
    assert chat.message.message_str == " help  &" and chat.message.raw_message == payload
    assert {o["id_kind"] for o in chat.observations} == {"channel_user_id"}


def test_c2c_does_not_use_group_self_mention_rules(config):
    payload = chat_payload("C2C_MESSAGE_CREATE", text=f"<@{GROUP_BOT}> 原文")
    payload["d"]["mentions"] = [{"id": GROUP_BOT, "member_openid": GROUP_MEMBER, "is_you": True}]
    chat, _ = event_from(config, payload)
    assert chat.message.self_id == READY_ID and chat.message.message_str == payload["d"]["content"]
    assert not any(isinstance(p, At) for p in chat.message.message)
    assert {o["id_kind"] for o in chat.observations} == {"user_openid"}
