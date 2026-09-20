"""Events retain their source generation and original official payload."""

import copy

from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .errors import unsupported


class V2MessageEvent(AstrMessageEvent):
    def __init__(self, message, meta, client, route):
        self.bot = client.bind(route, source=getattr(message, "v2_source", None))
        self.delivery_finished = lambda: None
        self.qq = self.bot.qq
        self.route = route
        self.raw_data = copy.deepcopy(message.raw_message)
        super().__init__(message.message_str, message, meta, route.encode())
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

    async def send_streaming(self, generator, use_fallback=False):
        self.bot.check()
        raise unsupported("Streaming and fallback sending are not implemented.")

    async def send_typing(self):
        self.bot.check()
        raise unsupported("Typing is not implemented.")
