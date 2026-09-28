"""Native message and upload declarations retain the official JSON body and one write ledger."""

import copy
from urllib.parse import quote
from uuid import uuid4

from ...errors import V2Error, not_ready
from ...protocol import RequestSpec
from ..identifiers import text_id


def segment(value):
    return quote(text_id(value), safe="")


def fields(**values):
    return {key: copy.deepcopy(value) for key, value in values.items() if value is not None}


def source_fields(body):
    if body.get("msg_id") is not None and body.get("event_id") is not None:
        raise V2Error("invalid_source", "msg_id and event_id are mutually exclusive.")
    if body.get("is_wakeup") is True and (body.get("msg_id") is not None or body.get("event_id") is not None):
        raise V2Error("invalid_source", "Wakeup messages cannot borrow a passive source.")
    for name in ("msg_id", "event_id"):
        if name in body:
            text_id(body[name])
    if "msg_seq" in body and (type(body["msg_seq"]) is not int or not 1 <= body["msg_seq"] <= 2**31 - 1):
        raise V2Error("invalid_sequence", "msg_seq must be a positive integer.")
    if "is_wakeup" in body and type(body["is_wakeup"]) is not bool:
        raise V2Error("invalid_request", "is_wakeup must be a boolean.")
    return body


class NativeMessageMixin:
    async def _native_send(self, scene, target, path, body, *, operation_id=None, file_image=None):
        self._check()
        sender = self._client._state.sender
        if sender is None:
            raise not_ready()
        source_fields(body)
        route = self._client.route_for(scene, target)
        prepared = None
        if file_image is not None:
            from ...media.types import MediaInput
            if sender.media is None:
                raise not_ready()
            prepared = await sender.media.prepare(route, MediaInput("image", file_image, "image"))
        try:
            return await sender.send_native(route, path, body, operation_id=self._operation_id(operation_id),
                                            prepared=prepared, guard=self._check)
        except BaseException:
            if prepared is not None:
                prepared.close()
            raise

    async def _native_write(self, method, path, body, *, kind, operation_id=None, scene=None, target=None,
                            params=None, allow_message=False):
        self._check()
        state = self._client._state
        if state.extension_state is None or state.http is None or state.sender is None:
            raise not_ready()
        if not allow_message:
            manager = state.management
            if manager is None:
                raise not_ready()
            manager.check(write=True)
        route = self._client.route_for(scene, target) if scene is not None else None
        if route is not None:
            state.sender.check(route, None)
            state.sender.connected(route)
        op_id = self._operation_id(operation_id) or uuid4().hex
        op_id = text_id(op_id)
        captured = []
        def validate(result):
            captured.append(result)
            if kind == "create_dms":
                if not isinstance(result, dict) or not isinstance(result.get("guild_id"), str):
                    raise V2Error("invalid_native_response", "QQ returned no usable DM guild ID.", phase="result_unknown", status=502)
                return {"guild_id": result["guild_id"]}
            if kind == "upload_prepare":
                if not isinstance(result, dict) or not isinstance(result.get("upload_id"), str) or not result["upload_id"]:
                    raise V2Error("invalid_upload_response", "QQ returned no upload task ID.", phase="result_unknown", status=502)
                return {"upload_id": result["upload_id"]}
            if kind == "upload_files":
                if (not isinstance(result, dict) or not isinstance(result.get("file_info"), str) or not result["file_info"]
                        or type(result.get("ttl")) is not int or result["ttl"] < 0):
                    raise V2Error("invalid_upload_response", "QQ did not return a usable file_info.", phase="result_unknown", status=502)
                return {"file_uuid": result.get("file_uuid") if isinstance(result.get("file_uuid"), str) else None, "ttl": result["ttl"]}
            if result not in (None, {}) and not isinstance(result, dict):
                raise V2Error("invalid_native_response", "QQ returned an invalid mutation result.", phase="result_unknown", status=502)
            return {"state": "succeeded"}
        def before_send():
            self._check()
            if not allow_message:
                state.management.check(write=True)
            if route is not None:
                state.sender.check(route, None)
                state.sender.connected(route)
        await state.extension_state.execute(state.http,
            RequestSpec(self._client.identity.robot.environment, method, path, params=params, json_body=body),
            op_id=op_id, kind=kind, validate=validate, before_send=before_send,
            context={"scene": scene, "target": target} if scene is not None else {})
        if not captured:
            raise V2Error("operation_result_not_retained", "The native mutation result is not safely reconstructible; do not replay.",
                          status=410, operation_id=op_id)
        return captured[0]

    async def post_group_message(self, group_openid: str, msg_type: int = 0, content: str | None = None,
                                 embed: dict | None = None, ark: dict | None = None, message_reference: dict | None = None,
                                 media: dict | None = None, msg_id: str | None = None, msg_seq: int | None = None,
                                 event_id: str | None = None, markdown: dict | None = None, keyboard: dict | None = None,
                                 *, operation_id: str | None = None) -> dict:
        """Send an exact group body; caller-supplied sources are resolved by QQ."""
        if type(msg_type) is not int or msg_type not in (0, 2, 7):
            raise V2Error("invalid_message", "Group msg_type must be 0, 2 or 7.")
        body = fields(msg_type=msg_type, content=content, embed=embed, ark=ark, message_reference=message_reference,
                      media=media, msg_id=msg_id, msg_seq=msg_seq, event_id=event_id, markdown=markdown, keyboard=keyboard)
        return await self._native_send("group", group_openid,
            f"/v2/groups/{segment(group_openid)}/messages", body, operation_id=operation_id)

    async def post_c2c_message(self, openid: str, msg_type: int = 0, content: str | None = None,
                               embed: dict | None = None, ark: dict | None = None, message_reference: dict | None = None,
                               media: dict | None = None, msg_id: str | None = None, msg_seq: int | None = None,
                               event_id: str | None = None, markdown: dict | None = None, keyboard: dict | None = None,
                               *, is_wakeup: bool | None = None, input_notify: dict | None = None,
                               operation_id: str | None = None) -> dict:
        """Send exact C2C fields including input status and wakeup semantics."""
        if type(msg_type) is not int or msg_type not in (0, 2, 6, 7):
            raise V2Error("invalid_message", "C2C msg_type must be 0, 2, 6 or 7.")
        body = fields(msg_type=msg_type, content=content, embed=embed, ark=ark, message_reference=message_reference,
                      media=media, msg_id=msg_id, msg_seq=msg_seq, event_id=event_id, markdown=markdown,
                      keyboard=keyboard, is_wakeup=is_wakeup, input_notify=input_notify)
        return await self._native_send("c2c", openid, f"/v2/users/{segment(openid)}/messages", body, operation_id=operation_id)

    async def post_c2c_stream_message(self, openid: str, content_raw: str, index: int,
                                      *, input_mode: str = "append", input_state: int = 1, content_type: str = "text",
                                      msg_id: str | None = None, event_id: str | None = None, msg_seq: int | None = None,
                                      stream_msg_id: str | None = None, is_wakeup: bool | None = None,
                                      operation_id: str | None = None) -> dict:
        """Write exactly one native fragment; partial streams are never restarted."""
        if type(index) is not int or index < 0 or type(input_state) is not int or input_state not in (1, 10):
            raise V2Error("invalid_stream", "Stream index or state is invalid.")
        if input_mode not in ("append", "replace") or content_type not in ("text", "markdown") or not isinstance(content_raw, str):
            raise V2Error("invalid_stream", "Native stream mode, format or content is invalid.")
        if index > 0 and stream_msg_id is None or index == 0 and stream_msg_id is not None:
            raise V2Error("invalid_stream", "Only continuation fragments carry a stream_msg_id.")
        if stream_msg_id is not None:
            text_id(stream_msg_id)
        body = fields(input_mode=input_mode, input_state=input_state, index=index, content_type=content_type,
                      content_raw=content_raw, msg_id=msg_id, event_id=event_id, msg_seq=msg_seq,
                      stream_msg_id=stream_msg_id, is_wakeup=is_wakeup)
        return await self._native_send("c2c", openid,
            f"/v2/users/{segment(openid)}/stream_messages", body, operation_id=operation_id)

    async def post_message(self, channel_id: str, content: str | None = None, embed: dict | None = None,
                           ark: dict | None = None, message_reference: dict | None = None, image: str | None = None,
                           file_image=None, msg_id: str | None = None, event_id: str | None = None,
                           markdown: dict | None = None, keyboard: dict | None = None,
                           *, operation_id: str | None = None) -> dict:
        """Send a channel JSON body or one owned multipart image."""
        if image is not None:
            from ...media.io import media_url
            media_url(image)
        body = fields(content=content, embed=embed, ark=ark, message_reference=message_reference, image=image,
                      msg_id=msg_id, event_id=event_id, markdown=markdown, keyboard=keyboard)
        if image is not None and file_image is not None:
            raise V2Error("invalid_message", "Choose one image source.")
        return await self._native_send("channel", channel_id,
            f"/channels/{segment(channel_id)}/messages", body, operation_id=operation_id, file_image=file_image)

    async def post_keyboard_message(self, channel_id: str, keyboard: dict | None = None,
                                    markdown: dict | None = None, *, operation_id: str | None = None) -> dict:
        """Send an explicit channel keyboard payload."""
        body = fields(keyboard=keyboard, markdown=markdown)
        return await self._native_send("channel", channel_id,
            f"/channels/{segment(channel_id)}/messages", body, operation_id=operation_id)

    async def on_interaction_result(self, interaction_id: str, code: int, *, operation_id: str | None = None):
        """ACK one non-managed native interaction through the shared owner."""
        self._check()
        if self._client._state.extensions is None:
            raise not_ready()
        return await self._client._state.extensions.reply_interaction(interaction_id, code,
            operation_id=self._operation_id(operation_id), guard=self._check)


    async def create_dms(self, guild_id: str, user_id: str, *, operation_id: str | None = None) -> dict:
        """Create a DM conversation for one explicit shared-guild member."""
        return await self._native_write("POST", "/users/@me/dms",
            {"recipient_id": text_id(user_id), "source_guild_id": text_id(guild_id)},
            kind="create_dms", operation_id=operation_id)

    async def post_dms(self, guild_id: str, content: str | None = None, embed: dict | None = None,
                       ark: dict | None = None, message_reference: dict | None = None, image: str | None = None,
                       file_image=None, msg_id: str | None = None, event_id: str | None = None,
                       markdown: dict | None = None, keyboard: dict | None = None,
                       *, operation_id: str | None = None) -> dict:
        """Send into a known DM guild ID without inferring a user OpenID."""
        if file_image is not None:
            raise V2Error("unsupported_media", "DM supports documented image URLs, not local multipart images.")
        if image is not None:
            from ...media.io import media_url
            media_url(image)
        body = fields(content=content, embed=embed, ark=ark, message_reference=message_reference, image=image,
                      msg_id=msg_id, event_id=event_id, markdown=markdown, keyboard=keyboard)
        return await self._native_send("dm", guild_id, f"/dms/{segment(guild_id)}/messages", body, operation_id=operation_id)

    async def recall_group_message(self, group_openid: str, message_id: str, *, operation_id: str | None = None):
        """Recall an explicitly scoped group message."""
        return await self._native_write("DELETE", f"/v2/groups/{segment(group_openid)}/messages/{segment(message_id)}",
                                        None, kind="recall_group_message", operation_id=operation_id, scene="group", target=group_openid)

    async def recall_c2c_message(self, openid: str, message_id: str, *, operation_id: str | None = None):
        """Recall an explicitly scoped C2C message."""
        return await self._native_write("DELETE", f"/v2/users/{segment(openid)}/messages/{segment(message_id)}",
                                        None, kind="recall_c2c_message", operation_id=operation_id, scene="c2c", target=openid)

    async def recall_message(self, channel_id: str, message_id: str, hidetip: bool = False,
                             *, operation_id: str | None = None):
        """Recall a channel message with the official hidetip switch."""
        if type(hidetip) is not bool:
            raise V2Error("invalid_params", "hidetip must be a boolean.")
        return await self._native_write("DELETE", f"/channels/{segment(channel_id)}/messages/{segment(message_id)}",
            None, kind="recall_message", operation_id=operation_id, scene="channel", target=channel_id,
            params={"hidetip": str(hidetip).lower()})

    async def recall_dms(self, guild_id: str, message_id: str, hidetip: bool = False,
                         *, operation_id: str | None = None):
        """Recall a DM message without guessing its original target."""
        if type(hidetip) is not bool:
            raise V2Error("invalid_params", "hidetip must be a boolean.")
        return await self._native_write("DELETE", f"/dms/{segment(guild_id)}/messages/{segment(message_id)}",
            None, kind="recall_dms", operation_id=operation_id, scene="dm", target=guild_id,
            params={"hidetip": str(hidetip).lower()})

    async def patch_guild_message(self, channel_id: str, patch_msg_id: str, msg_id: str | None = None,
                                  event_id: str | None = None, markdown: dict | None = None,
                                  keyboard: dict | None = None, *, operation_id: str | None = None):
        """Patch a channel message using the SDK 1.2.1 compatible endpoint."""
        body = source_fields(fields(msg_id=msg_id, event_id=event_id, markdown=markdown, keyboard=keyboard))
        return await self._native_write("PATCH", f"/channels/{segment(channel_id)}/messages/{segment(patch_msg_id)}",
            body, kind="patch_guild_message", operation_id=operation_id, scene="channel", target=channel_id)
