"""Host-independent in-process QQ V2 SDK contracts."""

from ..errors import V2Error
from .events import EventBus, EventContext, NativeEvent
from .types import CallOptions, ProfileResult

__all__ = ("V2Error", "EventBus", "EventContext", "NativeEvent", "CallOptions", "ProfileResult", "NativeUploadHandle")


def __getattr__(name):
    if name == "NativeUploadHandle":
        from .api.media import NativeUploadHandle
        return NativeUploadHandle
    raise AttributeError(name)
