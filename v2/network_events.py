"""Bounded live observation of the existing dispatch path, without another consumer."""
import asyncio
import secrets
import time
from collections import OrderedDict

from astrbot.core.message.components import At, Plain, Reply

from .errors import V2Error
from .models import text_id


class Peer:
    def __init__(self, server, ws, events):
        self.server, self.ws, self.events = server, ws, events
        self.queue = asyncio.Queue(maxsize=server.queue_frames)
        self.bytes = 0
        self.failure = None
        self.writer = None
        self.action = None

    def abort(self, reason):
        if self.failure is None:
            self.failure = reason
            self.server.last_error = reason
            self.server.disconnections += 1
        if self.writer:
            self.writer.cancel()

    def put(self, frame):
        if self.failure:
            return
        size = len(frame.encode())
        if (self.queue.full() or self.bytes + size > self.server.peer_bytes
                or self.server.queued_bytes + size > self.server.total_bytes):
            self.abort("subscriber_overflow")
            return
        self.queue.put_nowait((frame, size))
        self.bytes += size
        self.server.queued_bytes += size

    def clear(self):
        self.server.queued_bytes -= self.bytes
        self.bytes = 0
        while not self.queue.empty():
            self.queue.get_nowait()


class LiveEvents:
    def __init__(self, server):
        self.server = server
        self.contexts = OrderedDict()
        self.capacity = 1024

    def prune(self):
        now = self.server.adapter.owner.messages.now()
        for key, source in list(self.contexts.items()):
            if source.expires <= now:
                del self.contexts[key]

    def context(self, source):
        self.prune()
        if source.expires <= self.server.adapter.owner.messages.now():
            return None
        for key, previous in self.contexts.items():
            if previous == source:
                return key
        while len(self.contexts) >= self.capacity:
            self.contexts.popitem(last=False)
        key = secrets.token_urlsafe(24)
        self.contexts[key] = source
        return key

    def bind(self, key, action, params):
        self.server.check()
        self.prune()
        source = self.contexts.get(text_id(key))
        if source is None:
            raise V2Error("reply_context_unavailable", "This live context expired, was evicted or belongs to another generation/instance.")
        scene = "group" if action == "send_group_msg" or action == "send_msg" and "group_id" in params else "c2c"
        target = params.get("group_id" if scene == "group" else "user_id")
        if (scene, target) != (source.route.scene, source.route.target):
            raise V2Error("identity_mismatch", "The reply context cannot be used for a different target.")
        self.server.adapter.owner.messages.reply_mode(source.route, source)
        return self.server.adapter.client.bind(source.route, source=source)

    def meta(self, kind):
        adapter = self.server.adapter
        result = {"time": int(time.time()), "post_type": "meta_event" if adapter.bot_id else "qq_event",
                  "_qq": {"platform_id": adapter.identity.platform_id, "generation": adapter.identity.generation,
                          "id_semantics": "official string IDs", "self_id_known": bool(adapter.bot_id)}}
        if not adapter.bot_id:
            result["qq_type"] = "meta_event"
        if adapter.bot_id:
            result["self_id"] = adapter.bot_id
        if kind == "connect":
            result.update({"meta_event_type": "lifecycle", "sub_type": "connect"})
        else:
            result.update({"meta_event_type": "heartbeat", "status": adapter.runtime_status(), "interval": int(self.server.heartbeat * 1000)})
        return result

    def chat(self, chat):
        if not self.server.observed():
            return
        adapter = self.server.adapter
        segments, missing = [], ["font", "strict_numeric_ids"]
        for part in chat.message.message:
            if isinstance(part, Plain):
                segments.append({"type": "text", "data": {"text": part.text}})
            elif isinstance(part, At):
                segments.append({"type": "at", "data": {"qq": part.qq}})
            elif isinstance(part, Reply):
                segments.append({"type": "reply", "data": {"id": part.id}})
            else:
                missing.append("structured_or_attachment_content")
        sender = {"user_id": chat.message.sender.user_id}
        profile = next((r for r in chat.observations if r["user_id"] == sender["user_id"]), {})
        if "nickname" in profile:
            sender["nickname"] = profile["nickname"]
        result = {"post_type": "qq_event", "qq_type": "message", "time": int(chat.source.sent_at),
                  "message_id": chat.source.message_id, "user_id": sender["user_id"], "sender": sender, "message": segments,
                  "_qq": {"platform_id": adapter.identity.platform_id, "generation": adapter.identity.generation,
                          "scene": chat.route.scene, "target": chat.route.target, "attachment_count": len(chat.attachments),
                          "missing": missing, "compatibility": "partial OpenID message view, not a standard v11 event"}}
        if "content" in chat.message.raw_message["d"]:
            result["raw_message"] = chat.message.raw_message["d"]["content"]
        else:
            missing.append("raw_message")
        if adapter.bot_id:
            result["self_id"] = adapter.bot_id
        else:
            missing.append("self_id")
        if chat.route.scene in {"group", "c2c"}:
            result["message_type"] = "group" if chat.route.scene == "group" else "private"
            if chat.route.scene == "group":
                result["group_id"] = chat.route.target
            missing.append("sub_type")
            context = self.context(chat.source)
            if context:
                result["_qq_reply_context"] = context
        self.server.publish(result)

    def extension(self, event):
        if not self.server.observed():
            return
        # Do not export raw interaction payloads, callback tickets or approval flags.
        result = {"post_type": "qq_event", "qq_type": "extension", "event_type": event.name,
                  "_qq": {"platform_id": self.server.adapter.identity.platform_id,
                          "generation": self.server.adapter.identity.generation, "projection": "metadata_only"}}
        for key, value in (("time", event.sent_at), ("scene", event.scene), ("target", event.target), ("actor", event.actor)):
            if value is not None:
                result[key] = value
        self.server.publish(result)

    def retained(self, name):
        if name in {"FRIEND_ADD", "FRIEND_DEL", "GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT", "C2C_MSG_REJECT", "C2C_MSG_RECEIVE", "GROUP_MSG_REJECT", "GROUP_MSG_RECEIVE"}:
            self.server.publish({"post_type": "qq_event", "qq_type": "retained", "event_type": name,
                                 "_qq": {"platform_id": self.server.adapter.identity.platform_id,
                                         "generation": self.server.adapter.identity.generation, "projection": "name_only; no request semantics"}})
