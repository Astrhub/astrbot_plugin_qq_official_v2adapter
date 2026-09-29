"""Events retain their source generation and original official payload."""

import copy
from dataclasses import dataclass
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import At, AtAll
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.astrbot_message import Group

from .errors import V2Error, unsupported
from .media.service import FILE_TYPES
from .media.types import MediaInput
from .messaging.diagnostics import log_send_failure
from .models import text_id
from .sdk.identifiers import child_operation_id


@dataclass(frozen=True)
class V2MediaReceipt:
    file_info: str
    ttl: int
    file_uuid: str | None
    operation_id: str
    send_result: dict | None = None


class V2MessageEvent(AstrMessageEvent):
    def __init__(self, message, meta, client, route):
        self.bot = client.bind(route, source=getattr(message, "v2_source", None))
        self.delivery_finished = lambda: None
        self.qq = self.bot.qq
        self.qq._actor = message.sender.user_id
        self.route = route
        self.raw_data = copy.deepcopy(message.raw_message)
        if message.type != route.message_type:
            raise V2Error("invalid_session", "Message type does not match its QQ route.")
        session = route.public_session(meta.id, sender=message.sender.user_id)
        message.session_id = session.session_id
        super().__init__(message.message_str, message, meta, session.session_id)
        # A public ID alone may match several QQ scenes; this binding stays event-local.
        self.session._qq_v2_route = (client.identity, route, session.session_id, getattr(message, "v2_source", None))
        self.is_at_or_wake_command = self.raw_data.get("t") in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"} and not self.raw_data.get("derived_from_interaction")

    def get_message_outline(self):
        """Hide only this event's self mention in the host summary view."""
        self_id = self.get_self_id()
        if not self_id:
            return super().get_message_outline()
        # Trace calls this during BaseEvent construction; never swap the shared chain.
        view = copy.copy(self)
        view.message_obj = copy.copy(self.message_obj)
        view.message_obj.message = [part for part in self.get_messages()
                                    if not (isinstance(part, At) and not isinstance(part, AtAll) and str(part.qq) == self_id)]
        return AstrMessageEvent.get_message_outline(view)

    def cleanup_temporary_local_files(self):
        try:
            super().cleanup_temporary_local_files()
        finally:
            self.delivery_finished()

    async def get_group(self, group_id: str | None = None, **kwargs) -> Group | None:
        """Resolve a group or channel against QQ, retaining only event-bound fallback data."""
        self.bot.check()
        if kwargs:
            raise unsupported("get_group has no extra QQ options.")
        target = group_id if group_id is not None else self.message_obj.group_id
        if not target:
            return None
        target = text_id(target)
        bound = self.route.scene in {"group", "channel"} and target == self.route.target
        group = copy.copy(self.message_obj.group) if bound and self.message_obj.group else Group(group_id=target)
        try:
            if self.route.scene == "group":
                info = await self.qq.get_group_info(target)
                if info.get("group_openid") != target:
                    raise V2Error("invalid_management_response", "QQ group identity differs from the requested group.", status=502)
                if isinstance(info.get("group_name"), str):
                    group.group_name = info["group_name"]
                if type(info.get("group_member_num")) is int and info["group_member_num"] >= 0:
                    group.member_count = info["group_member_num"]
            elif self.route.scene == "channel":
                info = await self.qq.get_channel(target)
                if info.get("id") != target:
                    raise V2Error("invalid_management_response", "QQ channel identity differs from the requested channel.", status=502)
                if isinstance(info.get("name"), str):
                    group.group_name = info["name"]
                guild_id = info.get("guild_id") or self.raw_data.get("d", {}).get("guild_id") if bound else info.get("guild_id")
                if isinstance(guild_id, str) and guild_id:
                    guild = await self.qq.get_guild(guild_id)
                    if guild.get("id") == guild_id:
                        group.group_avatar = guild.get("icon") or group.group_avatar
                        group.group_owner = guild.get("owner_id") or group.group_owner
                        if type(guild.get("member_count")) is int:
                            group.member_count = guild["member_count"]
            elif not bound:
                raise unsupported("An unbound group scene cannot be inferred from a private event.")
        except V2Error as exc:
            if not bound or exc.phase not in {"rejected", "not_sent"}:
                raise
        return group


    async def send(self, message):
        try:
            result = await self.bot.send(self.route, message)
        except V2Error as exc:
            log_send_failure(logger, exc, boundary="event.send")
            raise
        self.set_extra("qq_send_result", result)
        await super().send(message)

    async def send_card(self, text, keyboard):
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        from .errors import V2Error
        chain = MessageChain([Plain(text)]).use_markdown(True)
        try:
            result = await self.bot.send(self.route, chain, keyboard=keyboard)
        except V2Error as exc:
            if exc.phase in {"not_sent", "rejected"}:
                keyboard.revoke()
            log_send_failure(logger, exc, boundary="event.send_card")
            raise
        self.set_extra("qq_send_result", result)
        await super().send(chain)

    def _media_route(self, targets):
        if targets.keys() - {"openid", "group_openid"} or ("openid" in targets) == ("group_openid" in targets):
            raise V2Error("invalid_media_target", "Supply exactly one C2C or group OpenID.")
        scene = "c2c" if "openid" in targets else "group"
        target = text_id(targets["openid"] if scene == "c2c" else targets["group_openid"])
        return scene, target, self.bot.route_for(scene, target)

    async def upload_group_and_c2c_image(self, image_base64: str, file_type: int, **kwargs) -> V2MediaReceipt:
        """Upload bounded base64 image bytes through the instance's existing media ledger."""
        if type(file_type) is not int or file_type != 1 or not isinstance(image_base64, str) or not image_base64:
            raise V2Error("invalid_media_input", "This helper accepts a base64 image and file_type=1.")
        operation_id = kwargs.pop("operation_id", None)
        return await self.upload_group_and_c2c_media("base64://" + image_base64, file_type,
            operation_id=operation_id, **kwargs)

    async def upload_group_and_c2c_media(self, file_source: str, file_type: int, srv_send_msg: bool = False,
                                         file_name: str | None = None, *, operation_id: str | None = None,
                                         **kwargs) -> V2MediaReceipt:
        """Upload a QQ URL or host-readable local file; optional sending uses the send ledger."""
        self.bot.check()
        if type(srv_send_msg) is not bool or type(file_type) is not int or file_type not in FILE_TYPES.values():
            raise V2Error("invalid_media_input", "Use a supported QQ file type and boolean send option.")
        scene, target, route = self._media_route(kwargs)
        media = self.bot._state.sender.media if self.bot._state.sender else None
        if media is None:
            raise unsupported("This instance has no media service.")
        kind = next(kind for kind, code in FILE_TYPES.items() if code == file_type)
        name = file_name or (Path(file_source).name if isinstance(file_source, str) and not file_source.startswith(("http:", "https:", "base64:", "data:")) else "upload")
        if operation_id is not None:
            operation_id = text_id(operation_id)
        send_id = child_operation_id(operation_id, "send") if srv_send_msg and operation_id is not None else None
        prepared = await media.prepare(route, MediaInput(kind, file_source, name))
        try:
            receipt = await media.upload(route, prepared, operation_id=operation_id, check=self.bot.check)
        finally:
            prepared.close()
        sent = None
        if srv_send_msg:
            sender = self.qq.post_group_message if scene == "group" else self.qq.post_c2c_message
            sent = await sender(target, msg_type=7, media={"file_info": receipt["file_info"]},
                                operation_id=send_id)
        return V2MediaReceipt(receipt["file_info"], receipt["ttl"], receipt.get("file_uuid"),
                              receipt["operation_id"], sent)

    async def post_c2c_message(self, openid: str, msg_type: int = 0, content: str | None = None,
                               embed: dict | None = None, ark: dict | None = None, message_reference: dict | None = None,
                               media: dict | None = None, msg_id: str | None = None, msg_seq: int | None = None,
                               event_id: str | None = None, markdown: dict | None = None, keyboard: dict | None = None,
                               stream: dict | None = None, *, is_wakeup: bool | None = None,
                               input_notify: dict | None = None, operation_id: str | None = None) -> dict:
        """Forward a native C2C request without old SDK replay or channel substitution."""
        if stream is not None:
            raise unsupported("Use post_c2c_stream_message for native C2C fragments.")
        return await self.qq.post_c2c_message(openid, msg_type, content, embed, ark, message_reference,
            media, msg_id, msg_seq, event_id, markdown, keyboard, is_wakeup=is_wakeup,
            input_notify=input_notify, operation_id=operation_id)


    async def send_streaming(self, generator, use_fallback=False):
        self.bot.check()
        if self.bot._state.streaming is None:
            raise unsupported("Streaming service is not attached.")
        self.set_extra("qq_stream_mode", self.bot._state.streaming.mode(self.route, use_fallback))
        try:
            result = await self.bot.stream(self.route, generator, use_fallback=use_fallback)
        except V2Error as exc:
            log_send_failure(logger, exc, boundary="event.send_streaming")
            raise
        self.set_extra("qq_send_result", result)
        await super().send_streaming(generator, use_fallback)

    async def send_typing(self):
        typing = self.bot._state.typing
        if self.route.scene != "c2c" or typing is None or not typing.settings().get("typing_enabled", False):
            return
        return await self.bot.qq.typing(self.route.scene, self.route.target)

    async def stop_typing(self):
        if self.bot._state.typing and self.bot._source is not None:
            await self.bot._state.typing.stop(self.route, source=self.bot._source)
