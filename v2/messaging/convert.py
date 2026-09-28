"""Bounded conversion of original QQ chat structures, never interaction projections."""
import copy
import html
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from astrbot.core.message.components import (
    At,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Unknown,
    Video,
)
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
        # Omit malformed optional avatars without discarding the real chat identity.
        pass
    return None


def attachment_url(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        _ = parsed.port  # The parser rejects nonnumeric and out-of-range ports.
        if (value.startswith(("http://", "https://")) and parsed.hostname and parsed.username is None
                and parsed.password is None and not any(ord(c) <= 32 or ord(c) == 127 for c in value)):
            return value
    except ValueError:
        pass
    return None


def attachment_component(item):
    kind = item.get("content_type")
    url = attachment_url(item.get("url"))
    if kind == "voice":
        source = attachment_url(item.get("voice_wav_url")) or url
        if source:
            caption = item.get("asr_refer_text")
            return Record(file=source, url=source, text=caption if isinstance(caption, str) else None)
    elif url and isinstance(kind, str):
        if kind.startswith("image/") and len(kind) > len("image/"):
            return Image(file=url, url=url)
        if kind == "video/mp4":
            return Video(file=url, url=url)
    if url:
        name = item.get("filename")
        if (not isinstance(name, str) or not 1 <= len(name.encode()) <= 255
                or any(c in name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or name in {".", ".."}):
            name = "file"
        return File(name=name, url=url)
    return None


def attachment_parts(items):
    converted = [attachment_component(item) for item in items]
    parts = [part for part in converted if part is not None]
    if any(part is None for part in converted):
        parts.append(Unknown(text="[附件元数据；需显式受控读取]"))
    return parts


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
    sender = text_id(author.get(field))
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

    def observe(user, *, source="current_chat", as_of=sent_at):
        if not isinstance(user, dict):
            invalid()
        user_id = user.get(field)
        if user_id is None:
            return None
        user_id = text_id(user_id)
        record = {"user_id": user_id, "id_kind": kind, "scope": scope, "source": source, "as_of": as_of}
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
    mentioned, group_self_tags = set(), set()
    group_self_id = ""
    for entry in mentions:
        user = observe(entry)
        if scene == "group" and entry.get("is_you") is True:
            # Event-local mention IDs can differ from READY; only member_openid enters the cache.
            own_id = entry.get("id")
            own_id = user if own_id is None else text_id(own_id)
            if own_id is not None:
                group_self_id = group_self_id or own_id
                group_self_tags.add(own_id)
                if user is not None:
                    group_self_tags.add(user)
                user = own_id
        if user is not None:
            mentioned.add(user)
    if bot_id:
        text_id(bot_id)
    content = data.get("content", "")
    if not isinstance(content, str):
        invalid()
    for user in group_self_tags:
        content = content.replace(f"<@{user}>", "").replace(f"<@!{user}>", "")
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
        part = At(qq=user)
        if scene == "group":
            part.qq = user  # Preserve OpenID leading zeros; the host At constructor coerces numeric strings.
        parts.append(part)
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
        text = node.get("content", "")
        if not isinstance(text, str):
            invalid()
        idx = node.get("msg_idx") if depth else None
        if idx is not None:
            idx = text_id(idx)
        children = node.get("msg_elements", [])
        if not isinstance(children, list):
            invalid()
        return node, idx, [elements(child, depth + 1) for child in children]

    root = elements(data)
    ref = indices.get("ref_msg_idx")
    if scene in {"channel", "dm"} and "message_reference" in data:
        reference = data["message_reference"]
        if not isinstance(reference, dict):
            invalid()
        ref = text_id(reference.get("message_id"))
        references.append({"message_id": ref})
    if ref:
        selected = []
        has_other_index = False
        if scene in {"group", "c2c"} and data.get("message_type") not in (101, 102):
            def match(nodes):
                nonlocal has_other_index
                for node in nodes:
                    if node[0].get("message_type") in (101, 102):
                        continue
                    if node[1] == ref:
                        selected.append(node)
                    elif node[1] is not None:
                        has_other_index = True
                    else:
                        match(node[2])
            match(root[2])
            # Official group quotes can omit child msg_idx entirely.
            if data.get("message_type") == 103 and not selected and not has_other_index:
                selected = [node for node in root[2] if node[1] is None and node[0].get("message_type") not in (101, 102)]

        def quote_parts(node):
            payload, _, children = node
            result = [Plain(payload["content"])] if payload.get("content") else []
            result.extend(attachment_parts(payload.get("attachments", [])))
            for child in children:
                if child[0].get("message_type") not in (101, 102) and (child[1] is None or child[1] == ref):
                    result.extend(quote_parts(child))
            return result

        chain = [part for node in selected for part in quote_parts(node)]
        quoted_text = "".join(part.text for part in chain if isinstance(part, Plain))
        authors = []
        for node in selected:
            quoted_author = node[0].get("author")
            if quoted_author is not None and quoted_author.get(field) is not None:
                authors.append((text_id(quoted_author[field]), quoted_author, node[0].get("timestamp")))
        quoted_user, quoted_name = None, None
        if authors and len({user for user, _, _ in authors}) == 1:
            from ..profiles.store import observation_time
            quoted_user = observe(authors[0][1], source="chat_history", as_of=observation_time(authors[0][2]))
            quoted_name = authors[0][1].get("username")
            if not isinstance(quoted_name, str) or len(quoted_name) > 256:
                quoted_name = None
        if selected and any(node[1] == ref for node in selected):
            references.append({"ref_idx": ref, "sender": quoted_user})
        reply = Reply(id=ref, sender_id=quoted_user, sender_nickname=quoted_name, time=None,
                      chain=chain, message_str=quoted_text)
        if quoted_user is not None:
            reply.sender_id = quoted_user  # The host model coerces numeric-looking IDs to int.
        parts.insert(0, reply)
    parts.extend(attachment_parts(data.get("attachments", [])))
    if not parts:
        # Empty/structured chat remains a real chat, not fabricated card prompt text.
        parts.append(Unknown(text="[结构化聊天；见原始信封]"))
    source = ReplySource(route, identity.generation, message_id, envelope.event_id, None,
                         indices.get("msg_idx") if scene in {"group", "c2c"} else None, sent_at, envelope.received_at)
    message = AstrBotMessage()
    message.type = MessageType.GROUP_MESSAGE if scene in {"group", "channel"} else MessageType.FRIEND_MESSAGE
    message.session_id, message.self_id, message.message_id = route.public_session(identity.platform_id, sender=sender).session_id, group_self_id or bot_id, message_id
    message.sender = MessageMember(sender, author.get("username") if isinstance(author.get("username"), str) else None)
    message.group_id = target if scene in {"group", "channel"} else ""
    message.message, message.message_str = parts, "".join(plain)
    message.raw_message, message.timestamp = copy.deepcopy(payload), int(sent_at)
    message.v2_source = source
    guild = text_id(data["guild_id"]) if scene == "channel" and data.get("guild_id") else None
    return Chat(identity, route, source, message, observations, attachments, references, guild)
