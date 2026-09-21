"""A callback command projection is not a chat observation or a synthetic QQ message."""
import copy

from astrbot.core.message.components import Plain
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType

from ..event import V2MessageEvent
from ..errors import V2Error
from .events import EventReplySource


class CommandProjection(V2MessageEvent):
    def __init__(self, adapter, event, ticket, tickets):
        self.ticket, self.tickets = ticket, tickets
        self.command_admitted = False
        self.projection_error = None
        route = event.route(adapter.identity, isolated=adapter.session_isolated)
        source = EventReplySource(route, adapter.identity.generation, event.event_id, event.interaction_id, event.sent_at, event.received_at)
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE if route.scene == "group" else MessageType.FRIEND_MESSAGE
        message.session_id, message.self_id, message.message_id = route.encode(), adapter.bot_id, ""
        message.sender = MessageMember(event.actor, None)
        message.group_id = route.target if route.scene == "group" else ""
        message.message, message.message_str = [Plain(ticket["command"])], ticket["command"]
        message.raw_message = {**copy.deepcopy(event.payload), "derived_from_interaction": True}
        message.timestamp, message.v2_source = int(event.sent_at), source
        super().__init__(message, adapter.meta(), adapter.client, route)
        self.set_extra("qq_interaction", event.metadata())
        self.set_extra("qq_source_kind", "interaction_projection")
        self.should_call_llm(False)

    def should_call_llm(self, call_llm):
        super().should_call_llm(False)

    def set_extra(self, key, value):
        if key == "activated_handlers":
            try:
                self.tickets.check_contract(self.ticket)
                selected = [handler for handler in value if handler.handler_full_name == self.ticket["handler"]]
                if len(selected) != 1:
                    raise V2Error("command_not_activated", "The normal host command/permission filters did not admit this command.", status=403)
                value = selected
                self.command_admitted = True
            except V2Error as exc:
                self.projection_error = exc.code
                value = []
                self.stop_event()
        super().set_extra(key, value)
