"""Group self mentions wake the real host stage without an addressed handler."""
import copy
import sqlite3

import pytest
from astrbot.core.message.components import At, Plain, Reply
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
from v2.models import InstanceKey, SessionRoute
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


async def test_structured_group_self_mention_wakes_host_and_matches_command(config, host_waking):
    mentions = [
        ({"id": GROUP_BOT, "member_openid": GROUP_BOT, "is_you": True}, GROUP_BOT),
        ({"id": GROUP_BOT, "is_you": True}, GROUP_BOT),
        ({"member_openid": GROUP_MEMBER, "is_you": True}, GROUP_MEMBER),
        ({"id": GROUP_BOT, "member_openid": GROUP_MEMBER, "is_you": True}, GROUP_BOT),
        ({"id": "01234567890123456789012345678901", "is_you": True}, "01234567890123456789012345678901"),
    ]
    for mention, expected_id in mentions:
        for bot_id in (READY_ID, ""):
            for marker in ("<@{}>", "<@!{}>"):
                payload = chat_payload("GROUP_MESSAGE_CREATE", text=marker.format(expected_id) + " help")
                payload["d"]["mentions"] = [mention]
                chat, event = event_from(config, payload, bot_id=bot_id)
                assert not event.is_at_or_wake_command
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


async def test_unconfirmed_group_mentions_and_nonliteral_is_you_do_not_wake_host(config, host_waking):
    for mentions, text in (
        ([], "你好"),
        ([], f"<@{GROUP_BOT}> 你好"),
        ([], f"<@!{READY_ID}> 你好"),
        ([{"id": GROUP_BOT, "member_openid": GROUP_BOT}], f"<@{GROUP_BOT}> 你好"),
        ([{"id": GROUP_BOT, "member_openid": GROUP_BOT, "bot": True, "username": "same-name"}], f"<@{GROUP_BOT}> 你好"),
        ([{"id": "another-user", "member_openid": "another-user", "is_you": False}], "<@another-user> 你好"),
        ([{"is_you": True, "username": "same-name", "bot": True}], f"<@{GROUP_BOT}> 你好"),
        ([{"user_openid": GROUP_BOT, "is_you": True}], f"<@{GROUP_BOT}> 你好"),
    ):
        payload = chat_payload("GROUP_MESSAGE_CREATE", text=text)
        payload["d"]["mentions"] = mentions
        chat, event = event_from(config, payload)
        await host_waking.process(event)
        assert event.is_stopped() and not event.is_wake and not event.is_at_or_wake_command
        assert chat.message.self_id == READY_ID and chat.message.message_str == text

    for flag in (False, None, 0, 1, "true", "false", [], {}):
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


def finished_bot_send(config, state, identity, *, event="GROUP_MESSAGE_CREATE", message_id="incoming"):
    """Record a confirmed bot send so its refs carry from_self=1."""
    chat = convert_chat(identity, RawEnvelope(chat_payload(event, message_id=message_id, text="hi"), NOW))
    state.observe(chat)
    state.reserve(chat.route, chat.source, "digest", "bot-send")
    state.mark_in_flight(identity.robot, "bot-send")
    state.finish(identity.robot, "bot-send", "sent", result={"message_id": "bot-msg", "ref_idx": "REFIDX_bot-msg"})
    return chat


def quote_chat(config, state, *, ref="REFIDX_bot-msg", event="GROUP_MESSAGE_CREATE", target="group-one",
               sender="user-two", elements=None, bot_id=READY_ID, identity=None, message_id="quote", now=NOW + 1):
    identity = identity if identity is not None else InstanceKey.from_config(config)
    payload = chat_payload(event, message_id=message_id, sender=sender, text="继续", target=target, timestamp=now)
    payload["d"]["message_type"] = 103
    payload["d"]["message_scene"]["ext"] = [f"msg_idx=REFIDX_{message_id}", f"ref_msg_idx={ref}"]
    payload["d"]["msg_elements"] = elements if elements is not None else [{"message_type": 103, "content": "hi"}]
    return payload, convert_chat(identity, RawEnvelope(payload, now), store=state, bot_id=bot_id)


def quote_event(config, chat):
    identity = chat.identity
    return V2MessageEvent(chat.message, PlatformMetadata(PLATFORM_TYPE, "QQ V2", id=identity.platform_id),
                          V2Client(identity), chat.route)


async def test_group_quote_reply_to_own_message_binds_self_and_wakes(config, host_waking, tmp_path):
    identity = InstanceKey.from_config(config)
    state = MessageStore(tmp_path / "quote-group.sqlite3", clock=lambda: NOW)
    try:
        finished_bot_send(config, state, identity)
        assert state.is_self_reference(SessionRoute(identity.robot, "group", "group-one"), "REFIDX_bot-msg")
        for elements in (
            [{"message_type": 103, "content": "hi"}],
            [{"message_type": 103, "content": "hi", "author": {"member_openid": GROUP_MEMBER, "username": "someone"}}],
        ):
            payload, chat = quote_chat(config, state, elements=elements)
            before = copy.deepcopy(payload)
            reply = next(part for part in chat.message.message if isinstance(part, Reply))
            assert reply.id == "REFIDX_bot-msg" and reply.sender_id == READY_ID == chat.message.self_id
            assert payload == before and chat.message.raw_message == before
            event = quote_event(config, chat)
            await host_waking.process(event)
            assert event.is_wake and event.is_at_or_wake_command and not event.is_stopped()
            assert str(reply.sender_id) == str(event.get_self_id())
        # Without a READY bot ID the binding falls back to the robot AppID identity.
        _, bare = quote_chat(config, state, bot_id="")
        assert bare.message.self_id == identity.robot.appid
        # Observing the quote chat must not fabricate a bot identity from the binding.
        state.observe(bare)
        for subject in (READY_ID, GROUP_BOT):
            with pytest.raises(V2Error):
                state.lookup(identity.robot, "member_openid", "group:group-one", subject)
    finally:
        state.close()


async def test_c2c_quote_reply_to_own_message_wakes_despite_wake_prefix(config, monkeypatch, tmp_path):
    monkeypatch.setattr(waking, "star_handlers_registry", StarHandlerRegistry())
    c2c_config = {"platform_settings": {"friend_message_needs_wake_prefix": True}, "wake_prefix": ["/"],
                  "admins_id": [], "plugin_set": ["another-plugin"]}
    stage = waking.WakingCheckStage()
    await stage.initialize(PipelineContext(c2c_config, None, "c2c-quote"))
    identity = InstanceKey.from_config(config)
    state = MessageStore(tmp_path / "quote-c2c.sqlite3", clock=lambda: NOW)
    try:
        finished_bot_send(config, state, identity, event="C2C_MESSAGE_CREATE")
        _, chat = quote_chat(config, state, event="C2C_MESSAGE_CREATE", sender="user-one",
                             elements=[{"message_type": 103, "content": "hi", "author": {"user_openid": GROUP_MEMBER}}])
        reply = next(part for part in chat.message.message if isinstance(part, Reply))
        assert reply.sender_id == READY_ID == chat.message.self_id
        event = quote_event(config, chat)
        await stage.process(event)
        assert event.is_wake and event.is_at_or_wake_command and not event.is_stopped()
    finally:
        state.close()


async def test_quote_of_other_unknown_expired_or_foreign_refs_do_not_wake(config, host_waking, tmp_path):
    identity = InstanceKey.from_config(config)
    other_identity = InstanceKey.from_config({**config, "appid": "another-app"})
    clock = [NOW]
    state = MessageStore(tmp_path / "quote-negative.sqlite3", clock=lambda: clock[0])
    try:
        chat = finished_bot_send(config, state, identity)
        state.observe(convert_chat(identity, RawEnvelope(chat_payload(message_id="mate-msg", sender="mate", text="hello"), NOW)))
        mate_quote = [{"message_type": 103, "content": "hello", "author": {"member_openid": "mate"}}]
        cases = [
            {"ref": "REFIDX_mate-msg"},
            {"ref": "REFIDX_mate-msg", "elements": mate_quote},
            {"ref": "REFIDX_unknown"},
            {"ref": "REFIDX_bot-msg", "target": "group-two"},
            {"ref": "REFIDX_bot-msg", "identity": other_identity},
        ]
        for case in cases:
            _, quoted = quote_chat(config, state, **case)
            reply = next(part for part in quoted.message.message if isinstance(part, Reply))
            assert reply.sender_id != READY_ID and quoted.message.self_id == READY_ID
            event = quote_event(config, quoted)
            await host_waking.process(event)
            assert event.is_stopped() and not event.is_wake and not event.is_at_or_wake_command
        # An expired self ref stops binding once its lease passes.
        clock[0] = NOW + 86401
        _, expired = quote_chat(config, state, ref="REFIDX_bot-msg", now=NOW + 86401)
        reply = next(part for part in expired.message.message if isinstance(part, Reply))
        assert reply.sender_id != READY_ID and expired.message.self_id == READY_ID
        await host_waking.process(quote_event(config, expired))
        assert not state.is_self_reference(chat.route, "REFIDX_bot-msg")
        assert not state.is_self_reference(chat.route, "REFIDX_mate-msg")
        assert not state.is_self_reference(chat.route, "REFIDX_unknown")
        assert not state.is_self_reference(SessionRoute(identity.robot, "group", "group-two"), "REFIDX_bot-msg")
        assert not state.is_self_reference(SessionRoute(other_identity.robot, "group", "group-one"), "REFIDX_bot-msg")
        assert not state.is_self_reference(chat.route, 123)
    finally:
        state.close()


def test_v4_upgrade_backfills_from_self_from_sent_operations(config, tmp_path):
    identity = InstanceKey.from_config(config)
    robot = identity.robot
    path = tmp_path / "upgrade.sqlite3"
    state = MessageStore(path, clock=lambda: NOW)
    try:
        chat = finished_bot_send(config, state, identity)
        state.observe(convert_chat(identity, RawEnvelope(chat_payload(message_id="mate-msg", sender="mate"), NOW)))
    finally:
        state.close()
    downgrade = sqlite3.connect(path)
    try:
        downgrade.execute("ALTER TABLE refs DROP COLUMN from_self")
        downgrade.commit()
    finally:
        downgrade.close()
    state = MessageStore(path, clock=lambda: NOW)
    try:
        for message_id in ("bot-msg", "REFIDX_bot-msg"):
            assert state.db.execute("SELECT from_self FROM refs WHERE message_id=?", (message_id,)).fetchone()[0] == 1
        for message_id in ("incoming", "REFIDX_incoming", "mate-msg", "REFIDX_mate-msg"):
            assert state.db.execute("SELECT from_self FROM refs WHERE message_id=?", (message_id,)).fetchone()[0] == 0
        assert state.is_self_reference(chat.route, "REFIDX_bot-msg")
        assert not state.is_self_reference(chat.route, "REFIDX_incoming")
        assert not state.is_self_reference(chat.route, "REFIDX_mate-msg")
        assert state.db.execute("PRAGMA user_version").fetchone()[0] == 4
    finally:
        state.close()
