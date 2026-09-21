"""One preflight/send/quota path; unknown writes are never replayed or refunded."""
import asyncio
import hashlib
import html
import json
import re
import sqlite3
from html.parser import HTMLParser
from urllib.parse import quote, unquote_plus
from uuid import uuid4

from astrbot.core.message.components import At, File, Image, Plain, Record, Reply, Video
from astrbot.core.message.message_event_result import MessageChain

from ..errors import V2Error, unsupported
from ..media.types import FilePart, MediaInput
from ..models import text_id
from ..protocol import RequestSpec, openapi_base

AMBIGUOUS_CODES = {304023, 304024, 40054005, 50055001, 50055002, 50055006}
EXPIRED_CODES = {304103, 40034005, 40034024, 40034025, 40034026, 40034027, 40034128}
MARKDOWN_DENIED = {304036, 40034127}


def invalid(message="Unsupported or malformed message; nothing was sent."):
    raise V2Error("invalid_message", message)


def cq_decode(text, *, parameter=False):
    replacements = {"&#91;": "[", "&#93;": "]", "&amp;": "&"}
    if parameter:
        replacements["&#44;"] = ","
    return re.sub("|".join(re.escape(k) for k in replacements), lambda m: replacements[m.group()], text)


def parse_message(message, *, onebot=False, auto_escape=False, markdown=None):
    if type(auto_escape) is not bool or markdown is not None and type(markdown) is not bool:
        invalid("auto_escape and markdown must be booleans.")
    if isinstance(message, MessageChain):
        use_md = message.use_markdown_ is True if markdown is None else markdown
        segments = message.chain
    else:
        use_md = markdown is True
        segments = message
    if isinstance(segments, str):
        if len(segments.encode()) > 32 * 1024:
            invalid("Message input exceeds 32 KiB.")
        if not onebot or auto_escape:
            segments = [Plain(segments)]
        else:
            parsed, offset = [], 0
            for match in re.finditer(r"\[CQ:([^\[\]]*)\]", segments):
                text = segments[offset:match.start()]
                if "[CQ:" in text:
                    invalid()
                if text:
                    parsed.append(Plain(cq_decode(text)))
                fields = match[1].split(",")
                data = {}
                for field in fields[1:]:
                    key, separator, value = field.partition("=")
                    if not separator or key in data or not key or key.strip() != key:
                        invalid()
                    data[key] = cq_decode(value, parameter=True)
                parsed.append({"type": fields[0], "data": data})
                offset = match.end()
            tail = segments[offset:]
            if "[CQ:" in tail:
                invalid()
            if tail:
                parsed.append(Plain(cq_decode(tail)))
            segments = parsed
    if isinstance(segments, dict):
        segments = [segments]
    if not isinstance(segments, list) or not 1 <= len(segments) <= 64:
        invalid()
    atoms, markdown_segments = [], 0
    for segment in segments:
        if isinstance(segment, Plain):
            kind, value = "text", segment.text
        elif isinstance(segment, At):
            kind, value = "at", segment.qq
        elif isinstance(segment, Reply):
            kind, value = "reply", segment.id
        elif isinstance(segment, MediaInput):
            kind, value = "media", segment.validate()
        elif isinstance(segment, (Image, Record, Video, File)):
            media_kind = {Image: "image", Record: "record", Video: "video", File: "file"}[type(segment)]
            source = (segment.file_ if isinstance(segment, File) else segment.file) or segment.url
            kind, value = "media", MediaInput(media_kind, source, getattr(segment, "name", None) or "upload").validate()
        elif isinstance(segment, dict) and segment.keys() <= {"type", "data"}:
            kind = segment.get("type")
            data = segment.get("data")
            if kind in {"image", "record", "video", "_qq_file"}:
                if not isinstance(data, dict) or "file" not in data or data.keys() - {"file", "name", "allow_file_fallback"}:
                    invalid("Unsupported media segment fields.")
                atoms.append(("media", MediaInput("file" if kind == "_qq_file" else kind, data["file"], data.get("name", "upload"),
                    allow_file_fallback=data.get("allow_file_fallback", False)).validate()))
                continue
            name = {"text": "text", "at": "qq", "reply": "id", "markdown": "content"}.get(kind)
            if name is None:
                raise unsupported("This message segment has no basic QQ mapping.")
            if not isinstance(data, dict) or data.keys() != {name}:
                invalid()
            value = data[name]
        else:
            raise unsupported("This message component is not supported by basic sending.")
        if kind == "media":
            atoms.append((kind, value))
            continue
        if not isinstance(value, str):
            invalid("QQ identifiers and text must be strings, not fabricated numeric IDs.")
        if kind in {"reply", "at"}:
            text_id(value)
        if kind == "markdown":
            markdown_segments += 1
            if markdown is False:
                invalid("Markdown cannot be silently converted to text.")
            use_md = True
        atoms.append((kind, value))
    if markdown_segments and (markdown_segments != 1 or any(k == "text" for k, _ in atoms)):
        invalid("Mixed text and Markdown segments cannot be sent as one equivalent message.")
    return atoms, use_md


class MarkupCheck(HTMLParser):
    def __init__(self, route, store):
        super().__init__(convert_charrefs=True)
        self.route, self.store = route, store
        self.tags = 0
        self.self_closing = False

    def handle_starttag(self, tag, attrs):
        if not tag.startswith("qqbot-"):
            return
        if not self.self_closing:
            invalid("QQ directives must be self-closing.")
        self.tags += 1
        values = dict(attrs)
        if len(values) != len(attrs) or any(v is None for v in values.values()):
            invalid("Malformed QQ Markdown attributes.")
        if tag in {"qqbot-cmd-input", "qqbot-cmd-enter"}:
            allowed = {"text", "show", "reference"} if tag.endswith("input") else {"text"}
            if not values.get("text") or values.keys() - allowed:
                invalid()
            if tag.endswith("enter") and self.route.scene in {"group", "channel"}:
                raise unsupported("cmd-enter is forbidden in groups and text channels.")
            for key in {"text", "show"} & values.keys():
                encoded = values[key]
                if (len(encoded) > 100 or len(unquote_plus(encoded)) > 100
                        or re.search(r"%(?![0-9a-fA-F]{2})", encoded)
                        or any(c in encoded for c in '<>"\r\n')):
                    invalid("QQ command text/show exceeds the safe encoded limit.")
            if values.get("reference", "false") not in {"true", "false"}:
                invalid()
        elif tag == "qqbot-at-user" and values.keys() == {"id"}:
            check_at(self.route, values["id"], self.store)
        elif tag == "qqbot-at-everyone" and not values and self.route.scene == "channel":
            pass
        else:
            raise unsupported("Unknown QQ Markdown directive or unsupported scene.")

    def handle_endtag(self, tag):
        if tag.startswith("qqbot-"):
            invalid("QQ directives must be self-closing.")

    def handle_startendtag(self, tag, attrs):
        self.self_closing = True
        try:
            self.handle_starttag(tag, attrs)
        finally:
            self.self_closing = False


def check_at(route, user, store):
    if route.scene not in {"group", "channel"} or user == "all" and route.scene != "channel":
        raise unsupported("This scene cannot express the requested mention.")
    if user != "all":
        kind = "member_openid" if route.scene == "group" else "channel_user_id"
        store.lookup(route.robot, kind, f"{route.scene}:{route.target}", text_id(user))


def build_body(route, atoms, markdown, store):
    parts, reference = [], None
    for kind, value in atoms:
        if kind == "reply":
            if reference is not None:
                invalid("Only one reply reference can be expressed.")
            reference = store.reference(route, value)
        elif kind == "at":
            check_at(route, value, store)
            parts.append("<qqbot-at-everyone />" if value == "all" else f'<qqbot-at-user id="{html.escape(value, quote=True)}" />')
        else:
            parts.append(value if markdown else html.escape(value, quote=False))
    content = "".join(parts)
    if not content.strip() or len(content) > 4096 or len(content.encode()) > 16384:
        invalid("Basic sending requires 1..4096 characters and at most 16 KiB; messages are not split.")
    if markdown:
        parser = MarkupCheck(route, store)
        parser.feed(content)
        parser.close()
        if len(re.findall(r"<\s*qqbot-", content, re.I)) != parser.tags:
            invalid("Malformed QQ Markdown directive.")
    body = {"markdown": {"content": content}} if markdown else {"content": content}
    if route.scene in {"group", "c2c"}:
        body["msg_type"] = 2 if markdown else 0
    if reference:
        body["message_reference"] = {"message_id": reference}
    return body


class SendingCore:
    def __init__(self, identity, http, store, *, guard=lambda: None, is_online=lambda: False, ws_online=lambda: False, media=None):
        self.identity, self.http, self.store, self.guard = identity, http, store, guard
        self.is_online, self.ws_online = is_online, ws_online
        self.media = media
        self.tasks = set()
        self.closed = False
        self.storage_failed = False

    def check(self, route, source):
        if self.closed:
            raise V2Error("service_stopped", "Message sending is stopped.", status=503)
        if self.storage_failed:
            raise V2Error("send_storage_unavailable", "Restore message storage and reload before attempting more writes.", status=503)
        self.guard()
        openapi_base(self.identity.robot.environment)
        if route.robot != self.identity.robot:
            raise V2Error("identity_mismatch", "Cannot send into another robot's namespace.", status=409)
        if source and source.generation != self.identity.generation:
            raise V2Error("stale_generation", "The originating event generation has ended.", status=409)

    def connected(self, route):
        if route.scene in {"channel", "dm"}:
            if not self.ws_online():
                raise V2Error("channel_ws_required", "The current channel contract requires an online WebSocket.", status=503)
        elif not self.is_online():
            raise V2Error("transport_not_ready", "No authenticated transport is ready to send.", status=503)

    async def send(self, route, message, *, source=None, onebot=False, auto_escape=False, markdown=None, operation_id=None, keyboard=None):
        self.check(route, source)
        if len(self.tasks) >= 32:
            raise V2Error("send_capacity", "Too many pending sends.", status=429)
        atoms, use_md = parse_message(message, onebot=onebot, auto_escape=auto_escape, markdown=markdown)
        if keyboard is not None:
            from ..extensions.keyboard import OwnedKeyboard
            if not isinstance(keyboard, OwnedKeyboard) or not use_md or route.scene not in {"group", "c2c"}:
                raise unsupported("Only owned group/C2C Markdown keyboards are supported.")
            keyboard.validate(route)
        media = [value for kind, value in atoms if kind == "media"]
        if media:
            if self.media is None:
                raise unsupported("This client has no owned media service.")
            if len(media) != 1 or use_md:
                raise unsupported("One media item is supported; mixed Markdown/media or multiple items cannot be sent equivalently.")
            ordinary = [(kind, value) for kind, value in atoms if kind != "media"]
            if route.scene in {"group", "c2c"} and any(kind != "reply" for kind, _ in ordinary):
                raise unsupported("Group/C2C media cannot preserve a text caption as one equivalent message.")
            if route.scene in {"channel", "dm"} and atoms[-1][0] != "media":
                raise unsupported("Channel/DM media must follow its text/reference components.")
            if any(kind != "reply" for kind, _ in ordinary):
                body = build_body(route, ordinary, False, self.store)
            else:
                if len(ordinary) > 1:
                    invalid("Only one reply reference can be expressed.")
                body = {"message_reference": {"message_id": self.store.reference(route, ordinary[0][1])}} if ordinary else {}
        else:
            body = build_body(route, atoms, use_md, self.store)
        if keyboard is not None:
            body["keyboard"] = keyboard.validate(route)
        op_id = uuid4().hex if operation_id is None else text_id(operation_id)
        task = asyncio.current_task()
        self.tasks.add(task)
        attempted, wire_started, prepared, upload = False, None, None, None
        def check_source():
            self.check(route, source)
            if prepared:
                self.media.check(route)
                if prepared.binding != self.media.binding(route):
                    raise V2Error("media_policy_changed", "Media authorization changed before message delivery.", status=409)
            self.connected(route)
            if source:
                self.store.check_source(route, source)
            else:
                self.store.target(route)
        try:
            if media:
                check_source()
                prepared = await self.media.prepare(route, media[0])
            binding = {"body": body, "media": prepared.descriptor()} if prepared else body
            fingerprint = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
            operation = self.store.reserve(route, source, fingerprint, op_id)
            if operation["state"] == "sent":
                return operation["result"]
            robot = route.robot
            response_received = False
            try:
                self.connected(route)
                if prepared:
                    if route.scene in {"group", "c2c"}:
                        upload = await self.media.upload(route, prepared, operation_id=op_id, check=check_source)
                        body.update({"msg_type": 7, "media": {"file_info": upload["file_info"]}})
                    elif prepared.blob is None:
                        body["image"] = prepared.input.value
                if source:
                    body["msg_id" if source.message_id is not None else "event_id"] = source.message_id if source.message_id is not None else source.event_id
                    if route.scene in {"c2c", "group"}:
                        body["msg_seq"] = operation["seq"]
                path = {"group": "/v2/groups/", "c2c": "/v2/users/", "channel": "/channels/", "dm": "/dms/"}[route.scene]
                path += quote(route.target, safe="") + "/messages"
                if prepared and route.scene == "channel" and prepared.blob is not None:
                    form = {key: json.dumps(value) if isinstance(value, dict) else str(value) for key, value in body.items()}
                    # file_image needs file bytes, not a locally inferred image format.
                    form["file_image"] = FilePart(prepared.blob, prepared.input.name, "application/octet-stream", check_source)
                    spec = RequestSpec(robot.environment, "POST", path, multipart=form)
                else:
                    spec = RequestSpec(robot.environment, "POST", path, json_body=body)
                def before_send():
                    nonlocal attempted, wire_started
                    check_source()
                    if upload and upload["expires_at"] <= self.store.now():
                        raise V2Error("upload_ticket_expired", "The upload receipt expired before the message request.", status=409)
                    if keyboard is not None:
                        keyboard.validate(route)
                    self.store.prepare_attempt(route, source, op_id)
                    attempted = True
                    wire_started = self.store.now()
                response = await self.http.request(spec, before_send=before_send)
                response_received = True
                data = response.data
                if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"] or len(data["id"]) > 512:
                    raise V2Error("invalid_send_response", "QQ did not return a real message ID; the write is unknown.",
                                  status=502, phase="result_unknown", http_status=response.status, trace_id=response.trace_id)
                result = {"message_id": data["id"], "operation_id": op_id, "msg_seq": operation["seq"], "state": "sent"}
                result["wire_started"] = wire_started
                if prepared:
                    result["media"] = {"kind": prepared.kind, "requested_kind": prepared.input.kind,
                                       "source": "url" if prepared.blob is None else "bytes",
                                       "upload_operation_id": upload["operation_id"] if upload else None}
                    if prepared.blob is not None:
                        result["media"]["size"] = prepared.blob.size
                if isinstance(data.get("timestamp"), str) and len(data["timestamp"]) <= 80:
                    result["timestamp"] = data["timestamp"]
                ext = data.get("ext_info")
                if isinstance(ext, dict) and isinstance(ext.get("ref_idx"), str) and ext["ref_idx"]:
                    result["ref_idx"] = text_id(ext["ref_idx"])
                self.store.finish(robot, op_id, "sent", result=result)
                return result
            except asyncio.CancelledError as exc:
                phase = "not_sent" if prepared and not attempted else getattr(exc, "phase", "result_unknown" if attempted else "not_sent")
                self.store.finish(robot, op_id, "unknown" if phase == "result_unknown" else "not_sent")
                raise
            except V2Error as exc:
                phase, details = exc.phase, exc.details
                if prepared and not attempted:
                    details = {"message_sent": False, "media_operation_id": exc.operation_id, "media_phase": exc.phase}
                    phase = "not_sent"
                if attempted and exc.code != "token_refresh_failed" and (exc.business_code in AMBIGUOUS_CODES or exc.http_status is not None and exc.http_status >= 500):
                    phase = "result_unknown"
                if response_received:
                    phase = "result_unknown"
                if exc.business_code in EXPIRED_CODES:
                    self.store.block_source(route, source, exc.business_code)
                outcome = {"not_sent": "not_sent", "rejected": "rejected"}.get(phase, "unknown")
                error = V2Error(exc.code, str(exc), retcode=exc.retcode, status=exc.status, business_code=exc.business_code,
                                trace_id=exc.trace_id, retry_after=exc.retry_after, phase=phase, http_status=exc.http_status, operation_id=op_id, details=details)
                self.store.finish(robot, op_id, outcome, error=error.as_dict())
                raise error from None
            except Exception:
                self.store.finish(robot, op_id, "unknown" if attempted else "not_sent")
                raise V2Error("send_state_failure", "Send state could not be finalized; inspect the retained operation before retrying.",
                              status=503, phase="result_unknown" if attempted else "not_sent", operation_id=op_id) from None
        except sqlite3.Error:
            self.storage_failed = True
            raise V2Error("send_state_failure", "Message storage failed; restore it and inspect this operation before retrying.",
                          status=503, phase="result_unknown" if attempted else "not_sent", operation_id=op_id) from None
        finally:
            if prepared:
                prepared.close()
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
