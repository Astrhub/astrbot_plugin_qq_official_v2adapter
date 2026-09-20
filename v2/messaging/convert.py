"""Bounded conversion of original QQ chat structures, never interaction projections."""
import copy
import html
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from astrbot.core.message.components import At, Plain, Reply, Unknown
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType

from ..errors import V2Error
from ..models import SessionRoute, text_id
from ..protocol import CHAT_EVENTS


@dataclass(frozen=True)
class ReplySource:
    route: SessionRoute
    generation: str
    message_id: str
    event_id: str | None
    interaction_id: str | None
    ref_idx: str | None
    sent_at: float
    received_at: float

    @property
    def expires(self):
        return min(self.sent_at, self.received_at) + (3600 if self.route.scene == "c2c" else 300)


@dataclass
class Chat:
    identity: object
    route: SessionRoute
    source: ReplySource
    message: AstrBotMessage
    observations: list
    attachments: list
    references: list
    guild_id: str | None


def invalid():
    raise V2Error("invalid_chat", "Chat structure is incomplete or exceeds conversion limits.")


def bounded_structure(payload):
    stack, count = [(payload, 0)], 0
    while stack:
        value, depth = stack.pop()
        count += 1
        if depth > 20 or count > 2048:
            invalid()
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
        elif isinstance(value, str) and len(value.encode()) > 64 * 1024:
            invalid()
    if len(json.dumps(payload, ensure_ascii=False).encode()) > 64 * 1024:
        invalid()


def safe_avatar(value):
    if not isinstance(value, str) or len(value) > 2048:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname and parsed.username is None and parsed.password is None and not any(ord(c) <= 32 for c in value):
            return value
    except ValueError:
        pass
    return None


def convert_chat(identity, envelope, *, isolated=False, bot_id=""):
    payload = envelope.payload
    if payload.get("op") != 0 or payload.get("t") not in CHAT_EVENTS or payload.get("derived_from_interaction"):
        raise V2Error("not_chat_source", "Only original chat envelopes may enter the message pipeline.")
    bounded_structure(payload)
    data = payload.get("d")
    if not isinstance(data, dict):
        invalid()
    kind, scene, field = CHAT_EVENTS[payload["t"]]
    author = data.get("author")
    if not isinstance(author, dict):
        invalid()
    sender = text_id(author.get(field) or author.get("id"))
    message_id = text_id(data.get("id"))
    target = text_id(data.get({"group": "group_openid", "channel": "channel_id", "dm": "guild_id"}[scene])) if scene != "c2c" else sender
    route = SessionRoute(identity.robot, scene, target, sender if isolated and scene in {"group", "channel"} else None)
    try:
        timestamp = data["timestamp"]
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            invalid()
        sent_at = parsed.timestamp()
        if not math.isfinite(envelope.received_at) or not 0 <= sent_at <= envelope.received_at + 60:
            invalid()
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        invalid()
    scope = f"{scene}:{target}"
    observations, attachments, references = [], [], []

    def observe(user):
        if not isinstance(user, dict):
            invalid()
        user_id = user.get(field) or user.get("id")
        if user_id is None:
            return None
        user_id = text_id(user_id)
        record = {"user_id": user_id, "id_kind": kind, "scope": scope}
        if isinstance(user.get("username"), str) and len(user["username"]) <= 256:
            record["nickname"] = user["username"]
        avatar = safe_avatar(user.get("avatar"))
        if avatar:
            record["avatar_url"] = avatar
        observations.append(record)
        return user_id

    observe(author)
    message_scene = data.get("message_scene", {})
    if not isinstance(message_scene, dict):
        invalid()
    ext = message_scene.get("ext", [])
    if not isinstance(ext, list) or len(ext) > 32 or any(not isinstance(x, str) for x in ext):
        invalid()
    indices = {}
    for item in ext:
        key, sep, value = item.partition("=")
        if sep and key in {"msg_idx", "ref_msg_idx"}:
            if key in indices and indices[key] != value:
                invalid()
            indices[key] = text_id(value)
    mentions = data.get("mentions", [])
    if not isinstance(mentions, list) or len(mentions) > 64:
        invalid()
    mentioned = {user for entry in mentions if (user := observe(entry)) is not None}
    if bot_id:
        text_id(bot_id)
    content = data.get("content", "")
    if not isinstance(content, str):
        invalid()
    # Only documented channel tags with structured mentions (or known bot ID) become At.
    parts, plain, offset = [], [], 0
    if scene in {"channel", "dm"}:
        pattern = r'<@!?(?P<old>[^<>\s]+)>|<qqbot-at-user id="(?P<new>[^"<>]+)"\s*/>'
        for match in re.finditer(pattern, content):
            user = match.group("old") or html.unescape(match.group("new"))
            if user not in mentioned and user != bot_id:
                continue
            text = html.unescape(content[offset:match.start()])
            if text:
                parts.append(Plain(text))
                plain.append(text)
            parts.append(At(qq=user))
            offset = match.end()
    remaining = html.unescape(content[offset:]) if scene in {"channel", "dm"} else content
    if remaining:
        parts.append(Plain(remaining))
        plain.append(remaining)
    existing_ats = {p.qq for p in parts if isinstance(p, At)}
    for user in sorted(mentioned - existing_ats):
        parts.append(At(qq=user))
    quoted = {}
    nodes = 0

    def elements(node, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > 8 or nodes > 256 or not isinstance(node, dict):
            invalid()
        if "author" in node and not isinstance(node["author"], dict):
            invalid()
        attached = node.get("attachments", [])
        if not isinstance(attached, list) or len(attached) > 32:
            invalid()
        for item in attached:
            if not isinstance(item, dict) or len(attachments) >= 32:
                invalid()
            size = item.get("size")
            if size is not None and (type(size) is not int or size < 0):
                invalid()
            attachments.append(copy.deepcopy(item))
        if depth:
            idx = node.get("msg_idx")
            is_reference = node.get("message_type") == 103 or (idx and idx == indices.get("ref_msg_idx"))
            user = observe(node["author"]) if is_reference and "author" in node else None
            text = node.get("content", "")
            if not isinstance(text, str):
                invalid()
            if idx:
                idx = text_id(idx)
                quoted[idx] = (user, text, node.get("author", {}).get("username"))
                references.append({"ref_idx": idx, "sender": user})
        children = node.get("msg_elements", [])
        if not isinstance(children, list):
            invalid()
        for child in children:
            elements(child, depth + 1)
    elements(data)
    ref = indices.get("ref_msg_idx")
    if scene in {"channel", "dm"} and "message_reference" in data:
        reference = data["message_reference"]
        if not isinstance(reference, dict):
            invalid()
        ref = text_id(reference.get("message_id"))
        references.append({"message_id": ref})
    if ref:
        user, text, nickname = quoted.get(ref, (None, "", None))
        parts.insert(0, Reply(id=ref, sender_id=user, sender_nickname=nickname, time=None,
                              chain=[Plain(text)] if text else [], message_str=text))
    if attachments:
        parts.append(Unknown(text="[附件元数据；本阶段不自动下载]"))
    if not parts:
        # Empty/structured chat remains a real chat, not fabricated card prompt text.
        parts.append(Unknown(text="[结构化聊天；见原始信封]"))
    source = ReplySource(route, identity.generation, message_id, envelope.event_id, None,
                         indices.get("msg_idx") if scene in {"group", "c2c"} else None, sent_at, envelope.received_at)
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if scene in {"group", "channel"} else MessageType.FRIEND_MESSAGE
    message.session_id, message.self_id, message.message_id = route.encode(), bot_id, message_id
    message.sender = MessageMember(sender, author.get("username") if isinstance(author.get("username"), str) else None)
    message.group_id = target if scene in {"group", "channel"} else ""
    message.message, message.message_str = parts, "".join(plain)
    message.raw_message, message.timestamp = copy.deepcopy(payload), int(sent_at)
    message.v2_source = source
    guild = text_id(data["guild_id"]) if scene == "channel" and data.get("guild_id") else None
    return Chat(identity, route, source, message, observations, attachments, references, guild)
