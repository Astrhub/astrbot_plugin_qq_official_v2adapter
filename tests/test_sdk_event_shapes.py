"""Documented QQ dispatch samples remain typed without losing wire extras."""


from v2.sdk.event_types import SHAPES
from v2.sdk.events import EventContext, NativeEvent


# Each event name is a literal fixture, independent of the production event catalog.
DOCUMENTED = [
    ("GUILD_CREATE", {"id": "guild", "name": "guild-name", "member_count": 0}),
    ("GUILD_UPDATE", {"id": "guild", "name": "renamed", "member_count": 1}),
    ("GUILD_DELETE", {"id": "guild", "name": "old"}),
    ("CHANNEL_CREATE", {"id": "channel", "guild_id": "guild", "name": "channel"}),
    ("CHANNEL_UPDATE", {"id": "channel", "guild_id": "guild", "name": "renamed"}),
    ("CHANNEL_DELETE", {"id": "channel", "guild_id": "guild"}),
    ("GUILD_MEMBER_ADD", {"guild_id": "guild", "joined_at": "2026-01-01T00:00:00Z", "user": {"id": "u"}, "roles": ["1"]}),
    ("GUILD_MEMBER_UPDATE", {"guild_id": "guild", "user": {"id": "u"}, "nick": "new"}),
    ("GUILD_MEMBER_REMOVE", {"guild_id": "guild", "user": {"id": "u"}, "op_user_id": "admin"}),
    ("MESSAGE_CREATE", {"id": "m", "guild_id": "guild", "channel_id": "channel", "author": {"id": "u"}, "content": "hi", "timestamp": "2026-01-01T00:00:00Z", "seq": 5}),
    ("AT_MESSAGE_CREATE", {"id": "m", "guild_id": "guild", "channel_id": "channel", "author": {"id": "u"}, "content": "@bot"}),
    ("DIRECT_MESSAGE_CREATE", {"id": "m", "guild_id": "guild", "channel_id": "dm", "author": {"id": "u"}}),
    ("MESSAGE_REACTION_ADD", {"user_id": "u", "emoji": {"id": "277", "type": 1}, "channel_id": "c", "guild_id": "g", "target": {"id": "m", "type": 0}}),
    ("MESSAGE_REACTION_REMOVE", {"user_id": "u", "emoji": {"id": "277", "type": 1}, "channel_id": "c", "guild_id": "g", "target": {"id": "m", "type": 0}}),
    ("GROUP_MEMBER_ADD", {"timestamp": 1784276757, "group_openid": "g", "member_openid": "u", "user_openid": "cross-app-id"}),
    ("GROUP_MEMBER_REMOVE", {"timestamp": 1784276758, "group_openid": "g", "member_openid": "u"}),
    ("GROUP_JOIN_REQUEST", {"group_openid": "g", "join_request_id": "flag", "member_openid": "u", "apply_at": "2026-08-05T16:21:40+08:00", "apply_source": "self_apply", "verify_info": {"method": "verify_message"}}),
    ("GROUP_AT_MESSAGE_CREATE", {"id": "m", "author": {"member_openid": "u"}, "group_openid": "g", "timestamp": "2026-01-01T00:00:00Z", "mentions": [{"member_openid": "bot"}]}),
    ("GROUP_MESSAGE_CREATE", {"id": "m", "author": {"member_openid": "u"}, "group_openid": "g", "timestamp": "2026-01-01T00:00:00Z"}),
    ("C2C_MESSAGE_CREATE", {"id": "m", "author": {"user_openid": "u"}, "content": "hi", "timestamp": "2026-01-01T00:00:00Z"}),
    ("FRIEND_ADD", {"timestamp": 1784276800, "openid": "u", "scene": 1, "author": {"username": "friend"}}),
    ("FRIEND_DEL", {"timestamp": 1784276801, "openid": "u"}),
    ("C2C_MSG_REJECT", {"timestamp": 1784276800, "openid": "u"}),
    ("C2C_MSG_RECEIVE", {"timestamp": 1784276800, "openid": "u"}),
    ("GROUP_ADD_ROBOT", {"timestamp": 1784276800, "group_openid": "g", "op_member_openid": "admin"}),
    ("GROUP_DEL_ROBOT", {"timestamp": 1784276800, "group_openid": "g", "op_member_openid": "admin"}),
    ("GROUP_MSG_REJECT", {"timestamp": 1784276800, "group_openid": "g", "op_member_openid": "admin"}),
    ("GROUP_MSG_RECEIVE", {"timestamp": 1784276800, "group_openid": "g", "op_member_openid": "admin"}),
    ("INTERACTION_CREATE", {"id": "interaction", "type": 11, "scene": "group", "group_openid": "g", "timestamp": "2026-07-20T21:53:54+08:00", "data": {"resolved": {"button_id": "confirm"}}}),
    ("MESSAGE_AUDIT_PASS", {"audit_id": "audit", "audit_time": "2026-01-01T00:00:00Z", "guild_id": "g", "channel_id": "c", "message_id": "m"}),
    ("MESSAGE_AUDIT_REJECT", {"audit_id": "audit", "guild_id": "g", "channel_id": "c", "message_id": "m"}),
    ("SUBSCRIBE_MESSAGE_STATUS", {"openid": "u", "result": [{"template_id": 10001, "op": 1, "subscribe_id": "sub"}]}),
    ("AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER", {"guild_id": "g", "channel_id": "c", "channel_type": 2, "user_id": "u"}),
    ("AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT", {"guild_id": "g", "channel_id": "c", "channel_type": 5, "user_id": "u"}),
    ("OPEN_FORUM_THREAD_CREATE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("OPEN_FORUM_THREAD_UPDATE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("OPEN_FORUM_THREAD_DELETE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("OPEN_FORUM_POST_CREATE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("OPEN_FORUM_POST_DELETE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("OPEN_FORUM_REPLY_CREATE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("OPEN_FORUM_REPLY_DELETE", {"guild_id": "g", "channel_id": "c", "author_id": "u"}),
    ("FORUM_THREAD_CREATE", {"guild_id": 47129941624960822, "channel_id": 1661124, "author_id": 144115218182563108, "thread_info": {"thread_id": "t"}}),
    ("FORUM_THREAD_UPDATE", {"guild_id": "g", "channel_id": "c", "author_id": "u", "thread_info": {"thread_id": "t"}}),
    ("FORUM_THREAD_DELETE", {"guild_id": "g", "channel_id": "c", "author_id": "u", "thread_info": {"thread_id": "t"}}),
    ("FORUM_POST_CREATE", {"guild_id": "g", "channel_id": "c", "author_id": "u", "post_info": {"thread_id": "t", "post_id": "p"}}),
    ("FORUM_POST_DELETE", {"guild_id": "g", "channel_id": "c", "author_id": "u", "post_info": {"thread_id": "t", "post_id": "p"}}),
    ("FORUM_REPLY_CREATE", {"guild_id": "g", "channel_id": "c", "author_id": "u", "reply_info": {"thread_id": "t", "post_id": "p", "reply_id": "r"}}),
    ("FORUM_REPLY_DELETE", {"guild_id": "g", "channel_id": "c", "author_id": "u", "reply_info": {"thread_id": "t", "post_id": "p", "reply_id": "r"}}),
    ("FORUM_PUBLISH_AUDIT_RESULT", {"guild_id": "g", "channel_id": "c", "author_id": "u", "thread_id": "t", "type": 1, "result": 0}),
    ("AUDIO_START", {"guild_id": "g", "channel_id": "c", "audio_url": "https://audio.test/a.mp3"}),
    ("AUDIO_FINISH", {"guild_id": "g", "channel_id": "c"}),
    ("AUDIO_ON_MIC", {"guild_id": "g", "channel_id": "c"}),
    ("AUDIO_OFF_MIC", {"guild_id": "g", "channel_id": "c"}),
    ("READY", {"session_id": "session", "user": {"id": "bot", "bot": True}, "shard": [0, 1], "version": 1}),
    ("RESUMED", ""),
]

# Botpy dispatches these names; their current dedicated body pages were not found.
SDK_COMPATIBILITY = [
    ("MESSAGE_DELETE", {"id": "m", "guild_id": "g", "channel_id": "c"}),
    ("DIRECT_MESSAGE_DELETE", {"id": "m", "guild_id": "g", "channel_id": "dm"}),
    ("PUBLIC_MESSAGE_DELETE", {"id": "m", "guild_id": "g", "channel_id": "c"}),
]


def make_event(name, body, *, platform_id="platform", appid="app", generation="generation"):
    context = EventContext(platform_id, appid, "production", generation, "websocket", (0, 1), "session", 100, 1)
    return NativeEvent({"op": 0, "id": "event-id", "t": name, "s": 11, "d": body}, context)


def test_documented_and_sdk_compatible_event_shapes_keep_extra_fields_and_validate_types():
    for name, body in DOCUMENTED:
        sample = {**body, "future_field": {"nested": [0, False, None]}} if isinstance(body, dict) else body
        notice = make_event(name, sample)
        assert notice.known and notice.schema_valid, (name, notice.schema_missing, notice.schema_invalid)
        assert notice.shape is SHAPES[name]
        assert notice.typed == {"t": name, "d": sample}
        assert notice.raw()["d"] == sample and notice.payload["d"] == notice.d
        if isinstance(sample, dict):
            notice.typed["d"]["future_field"]["nested"].append("detached")
            assert notice.d["future_field"]["nested"] == (0, False, None)
            first = notice.shape.required[0] if notice.shape.required else None
            if first:
                missing = make_event(name, {key: value for key, value in sample.items() if key != first})
                assert not missing.schema_valid and first in missing.schema_missing and missing.typed is None
                invalid = make_event(name, {**sample, first: None})
                assert not invalid.schema_valid and first in invalid.schema_missing

    for name, body in SDK_COMPATIBILITY:
        notice = make_event(name, body)
        assert notice.known and notice.schema_valid
        assert notice.shape is SHAPES[name] and notice.shape.required == ()
        assert notice.typed == {"t": name, "d": body}

def test_all_fifty_six_business_events_and_connection_notices_have_an_independent_fixture():
    actual = {name for name, *_ in DOCUMENTED} | {name for name, *_ in SDK_COMPATIBILITY}
    assert len(actual) == 58 and len(DOCUMENTED) == 55 and len(SDK_COMPATIBILITY) == 3
    assert set(SHAPES) == actual
    assert len([name for name in actual if name not in {"READY", "RESUMED"}]) == 56


def test_invalid_schema_never_becomes_a_typed_event_and_diagnostics_keep_raw():
    wrong = make_event("GROUP_MEMBER_ADD", {"timestamp": True, "group_openid": "g", "member_openid": "u"})
    assert wrong.known and not wrong.schema_valid and wrong.schema_invalid == ("timestamp",)
    assert wrong.typed is None and wrong.raw()["d"]["timestamp"] is True
    invalid_optional = make_event("GROUP_MEMBER_ADD", {"timestamp": 1, "group_openid": "g", "member_openid": "u", "user_openid": 7})
    assert invalid_optional.schema_missing == () and invalid_optional.schema_invalid == ("user_openid",)
    unknown = make_event("FUTURE_EVENT", {"custom": {"nested": 1}})
    assert not unknown.known and unknown.shape is None and unknown.typed is None
    assert unknown.raw()["d"] == {"custom": {"nested": 1}}
    non_object = make_event("GUILD_CREATE", [1, 2])
    assert non_object.known and non_object.schema_missing == ("d",) and non_object.typed is None
    assert make_event("RESUMED", "").typed == {"t": "RESUMED", "d": ""}
    assert make_event("RESUMED", {}).schema_valid
    assert make_event("RESUMED", []).typed is None
    other_bot = make_event("GROUP_MEMBER_ADD", {"timestamp": 1, "group_openid": "g", "member_openid": "u"}, appid="other")
    assert other_bot.key != wrong.key and other_bot.context.appid == "other"
