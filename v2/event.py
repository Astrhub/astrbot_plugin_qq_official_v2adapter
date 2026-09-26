"""Events retain their source generation and original official payload."""

import copy

from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .errors import V2Error, unsupported


class V2MessageEvent(AstrMessageEvent):
    def __init__(self, message, meta, client, route):
        self.bot = client.bind(route, source=getattr(message, "v2_source", None))
        self.delivery_finished = lambda: None
        self.qq = self.bot.qq
        self.route = route
        self.raw_data = copy.deepcopy(message.raw_message)
        if message.type != route.message_type:
            raise V2Error("invalid_session", "Message type does not match its QQ route.")
        session = route.public_session(meta.id, sender=message.sender.user_id)
        message.session_id = session.session_id
        super().__init__(message.message_str, message, meta, session.session_id)
        # A public ID alone may match several QQ scenes; this binding stays event-local.
        self.session._qq_v2_route = (client.identity, route, session.session_id)
        self.is_at_or_wake_command = self.raw_data.get("t") in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"} and not self.raw_data.get("derived_from_interaction")

    def cleanup_temporary_local_files(self):
        try:
            super().cleanup_temporary_local_files()
        finally:
            self.delivery_finished()

    async def send(self, message):
        result = await self.bot.send(self.route, message)
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
            raise
        self.set_extra("qq_send_result", result)
        await super().send(chain)

    async def send_streaming(self, generator, use_fallback=False):
        self.bot.check()
        if self.bot._state.streaming is None:
            raise unsupported("Streaming service is not attached.")
        self.set_extra("qq_stream_mode", self.bot._state.streaming.mode(self.route, use_fallback))
        result = await self.bot.stream(self.route, generator, use_fallback=use_fallback)
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
