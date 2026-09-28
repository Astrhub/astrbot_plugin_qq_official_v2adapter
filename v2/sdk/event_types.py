"""QQ dispatch body contracts; optional fields and unknown keys remain untouched."""

from dataclasses import dataclass
from typing import Literal, TypedDict, get_args, get_origin

from .catalog import EVENT_NAMES


class User(TypedDict, total=False):
    id: str
    member_openid: str
    user_openid: str
    username: str
    bot: bool
    avatar: str


class Guild(TypedDict, total=False):
    id: str
    name: str
    icon: str
    owner_id: str
    member_count: int
    max_members: int
    description: str
    joined_at: str
    op_user_id: str


class Channel(TypedDict, total=False):
    id: str
    guild_id: str
    name: str
    type: int
    sub_type: int
    owner_id: str
    op_user_id: str


class GuildMember(TypedDict, total=False):
    guild_id: str
    joined_at: str
    nick: str
    op_user_id: str
    roles: list[str]
    user: User


class Message(TypedDict, total=False):
    id: str
    author: User
    content: str
    timestamp: str
    guild_id: str
    channel_id: str
    group_openid: str
    member: GuildMember
    attachments: list[dict[str, object]]
    mentions: list[User]
    message_type: int
    message_scene: dict[str, object]
    msg_elements: list[dict[str, object]]
    seq: int


class GroupMember(TypedDict, total=False):
    timestamp: int
    group_openid: str
    member_openid: str
    user_openid: str


class GroupOperation(TypedDict, total=False):
    timestamp: int
    group_openid: str
    op_member_openid: str


class FriendOperation(TypedDict, total=False):
    timestamp: int
    openid: str
    scene: int
    scene_param: str
    author: User
    short_code: str
    union_openid: str


class C2COperation(TypedDict, total=False):
    timestamp: int
    openid: str


class JoinRequest(TypedDict, total=False):
    group_openid: str
    join_request_id: str
    member_openid: str
    username: str
    apply_at: str
    apply_source: str
    invited_by: str
    verify_info: dict[str, object]
    auto_approved: dict[str, object]
    risk_tips: str
    union_openid: str
    bot: bool


class Interaction(TypedDict, total=False):
    id: str
    type: int
    scene: str
    chat_type: int
    timestamp: str
    guild_id: str
    channel_id: str
    user_openid: str
    group_openid: str
    group_member_openid: str
    data: dict[str, object]
    version: int
    application_id: str


class Reaction(TypedDict, total=False):
    user_id: str
    emoji: dict[str, object]
    channel_id: str
    guild_id: str
    target: dict[str, object]


class Audit(TypedDict, total=False):
    audit_id: str
    audit_time: str
    channel_id: str
    create_time: str
    guild_id: str
    message_id: str


class Forum(TypedDict, total=False):
    guild_id: str | int
    channel_id: str | int
    author_id: str | int
    thread_info: dict[str, object]
    post_info: dict[str, object]
    reply_info: dict[str, object]
    thread_id: str
    post_id: str
    reply_id: str
    type: int
    result: int
    err_msg: str


class Audio(TypedDict, total=False):
    guild_id: str
    channel_id: str
    audio_url: str
    text: str
    channel_type: int
    user_id: str


class SubscribeStatus(TypedDict, total=False):
    group_openid: str
    openid: str
    result: list[dict[str, object]]


class Ready(TypedDict, total=False):
    version: int
    session_id: str
    user: User
    shard: list[int]


class GuildNotice(TypedDict):
    t: Literal["GUILD_CREATE", "GUILD_UPDATE", "GUILD_DELETE"]
    d: Guild


class ChannelNotice(TypedDict):
    t: Literal["CHANNEL_CREATE", "CHANNEL_UPDATE", "CHANNEL_DELETE"]
    d: Channel


class MemberNotice(TypedDict):
    t: Literal["GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE", "GUILD_MEMBER_REMOVE"]
    d: GuildMember


class ChatNotice(TypedDict):
    t: Literal["MESSAGE_CREATE", "MESSAGE_DELETE", "AT_MESSAGE_CREATE", "PUBLIC_MESSAGE_DELETE",
               "DIRECT_MESSAGE_CREATE", "DIRECT_MESSAGE_DELETE", "GROUP_AT_MESSAGE_CREATE",
               "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"]
    d: Message


class GroupMemberNotice(TypedDict):
    t: Literal["GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"]
    d: GroupMember


class GroupActionNotice(TypedDict):
    t: Literal["GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE"]
    d: GroupOperation


class FriendNotice(TypedDict):
    t: Literal["FRIEND_ADD", "FRIEND_DEL"]
    d: FriendOperation


class C2CNotice(TypedDict):
    t: Literal["C2C_MSG_REJECT", "C2C_MSG_RECEIVE"]
    d: C2COperation


class JoinNotice(TypedDict):
    t: Literal["GROUP_JOIN_REQUEST"]
    d: JoinRequest


class InteractionNotice(TypedDict):
    t: Literal["INTERACTION_CREATE"]
    d: Interaction


class ReactionNotice(TypedDict):
    t: Literal["MESSAGE_REACTION_ADD", "MESSAGE_REACTION_REMOVE"]
    d: Reaction


class AuditNotice(TypedDict):
    t: Literal["MESSAGE_AUDIT_PASS", "MESSAGE_AUDIT_REJECT"]
    d: Audit


class ForumNotice(TypedDict):
    t: Literal["FORUM_THREAD_CREATE", "FORUM_THREAD_UPDATE", "FORUM_THREAD_DELETE",
               "FORUM_POST_CREATE", "FORUM_POST_DELETE", "FORUM_REPLY_CREATE", "FORUM_REPLY_DELETE",
               "FORUM_PUBLISH_AUDIT_RESULT", "OPEN_FORUM_THREAD_CREATE", "OPEN_FORUM_THREAD_UPDATE",
               "OPEN_FORUM_THREAD_DELETE", "OPEN_FORUM_POST_CREATE", "OPEN_FORUM_POST_DELETE",
               "OPEN_FORUM_REPLY_CREATE", "OPEN_FORUM_REPLY_DELETE"]
    d: Forum


class AudioNotice(TypedDict):
    t: Literal["AUDIO_START", "AUDIO_FINISH", "AUDIO_ON_MIC", "AUDIO_OFF_MIC",
               "AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER", "AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT"]
    d: Audio


class SubscribeNotice(TypedDict):
    t: Literal["SUBSCRIBE_MESSAGE_STATUS"]
    d: SubscribeStatus


class ReadyNotice(TypedDict):
    t: Literal["READY"]
    d: Ready


class ResumedNotice(TypedDict):
    t: Literal["RESUMED"]
    d: Literal[""]


TypedNotice = (GuildNotice | ChannelNotice | MemberNotice | ChatNotice | GroupMemberNotice | GroupActionNotice |
               FriendNotice | C2CNotice | JoinNotice | InteractionNotice | ReactionNotice | AuditNotice |
               ForumNotice | AudioNotice | SubscribeNotice | ReadyNotice | ResumedNotice)

@dataclass(frozen=True)
class EventShape:
    family: str
    source: str
    required: tuple[str, ...]
    fields: tuple[str, ...]
    types: dict[str, object]
    documented_body: bool = True

_BASE = "plan/qq-wiki-v2/develop/api-v2/"
_AUTO = _BASE + "autogen/event/"
_CHANNEL = _BASE + "server-inter/channel/"


def shape(family, source, required=(), fields=(), documented_body=True):
    return EventShape(family, source, tuple(required), tuple(fields), dict(fields), documented_body)

SHAPES: dict[str, EventShape] = {}


def add(names, family, source, required=(), fields=(), documented_body=True):
    for name in names.split():
        if name in SHAPES:
            raise ValueError(f"Duplicate event contract: {name}")
        actual_source = _AUTO + name.lower() + ".md" if source.startswith(_AUTO) else source
        SHAPES[name] = shape(family, actual_source, required, fields, documented_body)


add("GUILD_CREATE GUILD_UPDATE GUILD_DELETE", "guild", _AUTO + "guild_create.md", ("id",),
    Guild.__annotations__)
add("CHANNEL_CREATE CHANNEL_UPDATE CHANNEL_DELETE", "channel", _AUTO + "channel_create.md", ("id", "guild_id"),
    Channel.__annotations__)
add("GUILD_MEMBER_ADD GUILD_MEMBER_UPDATE GUILD_MEMBER_REMOVE", "guild_member",
    _CHANNEL + "role/guild_member.md", ("guild_id", "user"), GuildMember.__annotations__)
add("MESSAGE_CREATE AT_MESSAGE_CREATE DIRECT_MESSAGE_CREATE", "channel_message",
    _CHANNEL + "message/event.md", ("id", "author", "guild_id", "channel_id"), Message.__annotations__)
add("MESSAGE_DELETE PUBLIC_MESSAGE_DELETE DIRECT_MESSAGE_DELETE", "channel_message",
    "botpy 1.2.1 connection.py; current dedicated payload page not confirmed", (), Message.__annotations__, False)
add("GROUP_MESSAGE_CREATE GROUP_AT_MESSAGE_CREATE", "group_message",
    _AUTO + "group_message_create.md", ("id", "author", "group_openid"), Message.__annotations__)
add("C2C_MESSAGE_CREATE", "c2c_message", _AUTO + "c2c_message_create.md",
    ("id", "author"), Message.__annotations__)
add("GROUP_MEMBER_ADD GROUP_MEMBER_REMOVE", "group_member", _AUTO + "group_member_add.md",
    ("timestamp", "group_openid", "member_openid"), GroupMember.__annotations__)
add("GROUP_ADD_ROBOT GROUP_DEL_ROBOT GROUP_MSG_REJECT GROUP_MSG_RECEIVE", "group_operation",
    _AUTO + "group_add_robot.md", ("timestamp", "group_openid", "op_member_openid"), GroupOperation.__annotations__)
add("FRIEND_ADD FRIEND_DEL", "friend", _AUTO + "friend_add.md", ("timestamp", "openid"), FriendOperation.__annotations__)
add("C2C_MSG_REJECT C2C_MSG_RECEIVE", "c2c_operation", _AUTO + "c2c_msg_receive.md",
    ("timestamp", "openid"), C2COperation.__annotations__)
add("GROUP_JOIN_REQUEST", "join_request", _AUTO + "group_join_request.md",
    ("group_openid", "join_request_id", "member_openid", "apply_at"), JoinRequest.__annotations__)
add("INTERACTION_CREATE", "interaction", _AUTO + "interaction_create.md",
    ("id", "type", "scene", "timestamp"), Interaction.__annotations__)
add("MESSAGE_REACTION_ADD MESSAGE_REACTION_REMOVE", "reaction", _BASE + "server-inter/message/trans/emoji.md",
    ("user_id", "emoji", "channel_id", "guild_id", "target"), Reaction.__annotations__)
add("MESSAGE_AUDIT_PASS MESSAGE_AUDIT_REJECT", "audit", _CHANNEL + "message/event.md",
    ("audit_id", "guild_id", "channel_id", "message_id"), Audit.__annotations__)
add("OPEN_FORUM_THREAD_CREATE OPEN_FORUM_THREAD_UPDATE OPEN_FORUM_THREAD_DELETE OPEN_FORUM_POST_CREATE "
    "OPEN_FORUM_POST_DELETE OPEN_FORUM_REPLY_CREATE OPEN_FORUM_REPLY_DELETE", "open_forum",
    _CHANNEL + "content/forum/open_forum.md", ("guild_id", "channel_id", "author_id"), Forum.__annotations__)
add("FORUM_THREAD_CREATE FORUM_THREAD_UPDATE FORUM_THREAD_DELETE", "forum_thread",
    _CHANNEL + "content/forum/forum.md", ("guild_id", "channel_id", "author_id", "thread_info"), Forum.__annotations__)
add("FORUM_POST_CREATE FORUM_POST_DELETE", "forum_post", _CHANNEL + "content/forum/forum.md",
    ("guild_id", "channel_id", "author_id", "post_info"), Forum.__annotations__)
add("FORUM_REPLY_CREATE FORUM_REPLY_DELETE", "forum_reply", _CHANNEL + "content/forum/forum.md",
    ("guild_id", "channel_id", "author_id", "reply_info"), Forum.__annotations__)
add("FORUM_PUBLISH_AUDIT_RESULT", "forum_audit", _CHANNEL + "content/forum/forum.md",
    ("guild_id", "channel_id", "author_id", "type", "result"), Forum.__annotations__)
add("AUDIO_OR_LIVE_CHANNEL_MEMBER_ENTER AUDIO_OR_LIVE_CHANNEL_MEMBER_EXIT", "audio_member",
    _CHANNEL + "role/audio_or_live_channel_member.md", ("guild_id", "channel_id", "channel_type", "user_id"),
    Audio.__annotations__)
add("AUDIO_START AUDIO_FINISH AUDIO_ON_MIC AUDIO_OFF_MIC", "audio_action",
    _CHANNEL + "content/audio/model.md", ("guild_id", "channel_id"), Audio.__annotations__)
add("SUBSCRIBE_MESSAGE_STATUS", "subscription", _AUTO + "subscribe_message_status.md",
    ("result",), SubscribeStatus.__annotations__)
add("READY", "ready", _BASE + "dev-prepare/interface-framework/reference.md",
    ("session_id", "user"), Ready.__annotations__)
add("RESUMED", "resumed", _BASE + "dev-prepare/interface-framework/reference.md", (), {"d": str})

if set(SHAPES) != EVENT_NAMES:
    raise AssertionError("Event body contracts must cover every dispatch name")

def _matches(expected, value):
    if expected in (str, int, bool, float):
        return type(value) is expected
    if isinstance(expected, type) and hasattr(expected, "__required_keys__"):
        return isinstance(value, dict)
    origin = get_origin(expected)
    if origin in (list, dict):
        return isinstance(value, origin)
    if get_args(expected):
        return any(_matches(option, value) for option in get_args(expected))
    return True


def missing_fields(name: str, data: object) -> tuple[str, ...]:
    spec = SHAPES.get(name)
    if spec is None:
        return ()
    if name == "RESUMED":
        return () if data == "" or isinstance(data, dict) else ("d",)
    if not isinstance(data, dict):
        return ("d",)
    return tuple(key for key in spec.required if key not in data or data[key] is None or
                 not _matches(spec.types.get(key), data[key]) or
                 isinstance(data[key], str) and not data[key])


def invalid_fields(name: str, data: object) -> tuple[str, ...]:
    spec = SHAPES.get(name)
    if spec is None or not isinstance(data, dict):
        return ()
    return tuple(key for key, expected in spec.types.items() if key in data and data[key] is not None and
                 not _matches(expected, data[key]))
