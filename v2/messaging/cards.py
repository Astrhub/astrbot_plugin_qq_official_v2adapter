"""Validate atomic QQ group/C2C cards before the shared send pipeline."""
import copy
import json
from urllib.parse import urlsplit

from astrbot.core.message.components import Json

from ..errors import unsupported
from ..media.types import MediaInput, native_media_input
from ..models import text_id
from .outbound import build_body, invalid


def _fields(value, required=(), optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        invalid("Card contains missing or unsupported fields.")


def _text(value, limit=512):
    if not isinstance(value, str) or not 1 <= len(value) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        invalid("Card field must be bounded nonempty text.")
    return value


def keyboard_body(route, keyboard, callbacks=None, *, operation_id=None):
    if route.scene not in {"group", "c2c"}:
        raise unsupported("Keyboards are supported only for group/C2C messages.")
    _fields(keyboard, optional={"id", "content"})
    if ("id" in keyboard) == ("content" in keyboard):
        invalid("Keyboard template ID and custom content are mutually exclusive.")
    if "id" in keyboard:
        _text(keyboard["id"])
        return copy.deepcopy(keyboard), []
    content = keyboard["content"]
    _fields(content, {"rows"})
    rows = content["rows"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 5:
        invalid("Keyboard rows exceed the supported layout.")
    seen, tokens = set(), []
    for row in rows:
        _fields(row, {"buttons"})
        buttons = row["buttons"]
        if not isinstance(buttons, list) or not 1 <= len(buttons) <= 5:
            invalid("Keyboard buttons exceed the supported layout.")
        for button in buttons:
            _fields(button, {"id", "render_data", "action"}, {"group_id"})
            button_id = _text(button["id"])
            if button_id in seen:
                invalid("Button IDs must be unique in one keyboard.")
            seen.add(button_id)
            render, action = button["render_data"], button["action"]
            _fields(render, {"label", "style"}, {"visited_label"})
            if not isinstance(render["label"], str) or not 1 <= len(render["label"]) <= 10 or type(render["style"]) is not int or render["style"] not in {0, 1, 3, 4}:
                invalid("Button label or style is invalid.")
            if "visited_label" in render and (not isinstance(render["visited_label"], str) or not 1 <= len(render["visited_label"]) <= 10):
                invalid("Button visited label is invalid.")
            _fields(action, {"type", "permission", "data"}, {"enter", "reply", "anchor", "modal", "unsupport_tips"})
            kind = action["type"]
            if type(kind) is not int or kind not in {0, 1, 2}:
                invalid("Button action type is invalid.")
            data = _text(action["data"], 1024)
            _fields(action["permission"], {"type"}, {"specify_user_ids"})
            permission = action["permission"]
            if type(permission["type"]) is not int or permission["type"] not in {0, 1, 2}:
                invalid("Only QQ group/C2C button permissions are supported.")
            if permission["type"] == 0:
                users = permission.get("specify_user_ids")
                if not isinstance(users, list) or not 1 <= len(users) <= 20 or any(not isinstance(u, str) for u in users) or len(set(users)) != len(users):
                    invalid("Specific-user buttons require distinct OpenIDs.")
                for user in users:
                    text_id(user)
            elif "specify_user_ids" in permission:
                invalid("Specific users require permission type 0.")
            if "group_id" in button:
                if kind != 1:
                    invalid("Button group_id applies only to callbacks.")
                _text(button["group_id"])
            for flag in ("enter", "reply"):
                if flag in action and (kind != 2 or type(action[flag]) is not bool or flag == "enter" and route.scene == "group" and action[flag]):
                    invalid("Command button flags do not apply in this scene.")
            if "anchor" in action and (kind != 2 or route.scene != "c2c" or action["anchor"] != 1 or type(action["anchor"]) is not int or action.get("enter", False)):
                invalid("Command anchor requires C2C and cannot auto-enter.")
            if "modal" in action:
                modal = action["modal"]
                _fields(modal, {"content"}, {"confirm_text", "cancel_text"})
                if (not isinstance(modal["content"], str) or not 1 <= len(modal["content"]) <= 40
                        or any(mark in modal["content"].lower() for mark in ("http://", "https://", "www."))):
                    invalid("Button confirmation must be a short non-URL prompt.")
                for label in ("confirm_text", "cancel_text"):
                    if label in modal and (not isinstance(modal[label], str) or len(modal[label]) > 4):
                        invalid("Button confirmation label is too long.")
            if "unsupport_tips" in action:
                _text(action["unsupport_tips"], 100)
            if kind == 0:
                try:
                    url = urlsplit(data)
                except ValueError:
                    invalid("Jump URL is malformed.")
                if url.scheme not in {"http", "https", "mqqapi", "weapp"} or not url.netloc:
                    invalid("Jump buttons require a supported URL or mini-program scheme.")
            if kind == 1:
                if callbacks is None:
                    raise unsupported("Callback buttons require an attached, enabled callback service.")
                callbacks.validate(route, data, permission, allow_published=operation_id is not None, operation_id=operation_id)
                if data in tokens:
                    invalid("A callback ticket cannot be repeated in one card.")
                tokens.append(data)
    try:
        body = copy.deepcopy(keyboard)
        if len(json.dumps(body, ensure_ascii=False).encode()) > 32 * 1024:
            invalid("Keyboard exceeds the card size limit.")
    except (TypeError, ValueError, RecursionError):
        invalid("Keyboard must contain only JSON-compatible fields.")
    return body, tokens


def card_body(route, payload, store, callbacks=None, *, operation_id=None):
    if type(payload) is not dict:
        invalid("A QQ card must be a JSON object.")
    _fields(payload, {"msg_type", "keyboard"}, {"content", "markdown", "media", "message_reference"})
    kind = payload["msg_type"]
    if type(kind) is not int or kind not in {0, 2, 7}:
        invalid("Card message type must be text, Markdown or owned media.")
    media = None
    if kind == 0:
        if "markdown" in payload or "media" in payload:
            invalid("Text card cannot mix Markdown or media.")
        _fields(payload, {"msg_type", "keyboard", "content"}, {"message_reference"})
        if type(payload["content"]) is not str:
            invalid()
        body = build_body(route, [("text", payload["content"])], False, store)
    elif kind == 2:
        if "content" in payload or "media" in payload:
            invalid("Markdown card cannot mix text or media.")
        _fields(payload, {"msg_type", "keyboard", "markdown"}, {"message_reference"})
        _fields(payload["markdown"], {"content"}, {"force_verify_image_resource"})
        if type(payload["markdown"]["content"]) is not str:
            invalid()
        body = build_body(route, [("text", payload["markdown"]["content"])], True, store)
        if "force_verify_image_resource" in payload["markdown"]:
            if type(payload["markdown"]["force_verify_image_resource"]) is not bool:
                invalid()
            body["markdown"]["force_verify_image_resource"] = payload["markdown"]["force_verify_image_resource"]
    else:
        if "content" in payload or "markdown" in payload:
            raise unsupported("Media cards cannot combine text or Markdown in one verified request.")
        _fields(payload, {"msg_type", "keyboard", "media"}, {"message_reference"})
        if type(payload["media"]) is not MediaInput:
            raise unsupported("Media cards require the owned MediaInput constructor, not external file_info.")
        media = payload["media"].validate()
        body = {"msg_type": 7}
    if "message_reference" in payload:
        _fields(payload["message_reference"], {"message_id"})
        body["message_reference"] = {"message_id": store.reference(route, text_id(payload["message_reference"]["message_id"]))}
    body["keyboard"], tokens = keyboard_body(route, payload["keyboard"], callbacks, operation_id=operation_id)
    return body, media, tokens

def media_card(media, keyboard, *, reference=None):
    """Return one host-native Json with an AstrBot component or owned MediaInput."""
    payload = {"msg_type": 7, "media": native_media_input(media), "keyboard": keyboard}
    if reference is not None:
        payload["message_reference"] = {"message_id": text_id(reference)}
    return Json(payload)
