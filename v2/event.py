"""Events retain their source generation and original official payload."""

import copy

from astrbot.core.platform.astr_message_event import AstrMessageEvent

from .errors import unsupported


class V2MessageEvent(AstrMessageEvent):
    def __init__(self, message, meta, client, route):
        self.bot = client.bind(route)
        self.qq = self.bot.qq
        self.route = route
        self.raw_data = copy.deepcopy(message.raw_message)
        super().__init__(message.message_str, message, meta, route.encode())

    async def send(self, message):
        await self.bot.send(self.route, message)

    async def send_streaming(self, generator, use_fallback=False):
        self.bot.check()
        raise unsupported("Streaming and fallback sending are not implemented.")

    async def send_typing(self):
        self.bot.check()
        raise unsupported("Typing is not implemented.")
