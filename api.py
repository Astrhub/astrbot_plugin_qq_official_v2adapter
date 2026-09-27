"""Public QQ V2 plugin button helpers."""
import inspect

from astrbot.api.event import filter

from .v2.media.types import MediaInput
from .v2.messaging.cards import media_card


class _CallbackOnly(filter.CustomFilter):
    def filter(self, event, cfg):
        return event.get_extra("qq_source_kind") == "interaction_callback"


def button_callback(name):
    """Register an async Star method for owned QQ V2 button interactions."""
    if not isinstance(name, str) or not 1 <= len(name) <= 64 or not all(c.isascii() and (c.isalnum() or c in "_-") for c in name):
        raise ValueError("Callback name must be a short ASCII identifier.")

    def decorate(function):
        if not (inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function)):
            raise TypeError("Button callback must be an async Star handler.")
        function.__qq_v2_callback_name__ = name
        return filter.custom_filter(_CallbackOnly)(function)

    return decorate


__all__ = ["button_callback", "media_card", "MediaInput"]
