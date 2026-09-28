"""Explicit input policy; a chat string alone never authorizes a local read."""
from dataclasses import dataclass, field
import inspect

from astrbot.core.message.components import File, Image, Record, Video

from ..errors import V2Error


@dataclass(frozen=True)
class MediaInput:
    kind: str
    value: object
    name: str = "upload"
    allow_file_fallback: bool = field(default=True, kw_only=True)

    def validate(self):
        if not isinstance(self.kind, str) or self.kind not in {"image", "record", "video", "file"} or type(self.allow_file_fallback) is not bool:
            raise V2Error("invalid_media_input", "Unsupported media kind or local file fallback policy.")
        if isinstance(self.value, str):
            valid = bool(self.value) and len(self.value) <= 12 * 1024 * 1024
        elif isinstance(self.value, (bytes, bytearray, memoryview)):
            valid = 0 < len(self.value) <= 200_000_000
        else:
            valid = callable(getattr(self.value, "read", None)) and not inspect.iscoroutinefunction(self.value.read)
        if not valid:
            raise V2Error("invalid_media_input", "Media input is missing or too large.")
        if not isinstance(self.name, str) or not 1 <= len(self.name.encode()) <= 255 or any(c in self.name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in self.name) or self.name in {".", ".."}:
            raise V2Error("invalid_media_name", "Use a bounded filename without path separators or control characters.")
        return self


def native_media_input(value):
    """Map AstrBot components to the same validated media input used by ordinary sends."""
    if type(value) is MediaInput:
        return value.validate()
    kind = {Image: "image", Record: "record", Video: "video", File: "file"}.get(type(value))
    if kind is None:
        raise V2Error("invalid_media_input", "Use an AstrBot media component or MediaInput.")
    source = (value.file_ if type(value) is File else value.file) or value.url
    return MediaInput(kind, source, getattr(value, "name", None) or "upload").validate()

@dataclass(frozen=True)
class FilePart:
    blob: object
    name: str
    mime: str
    check: object = lambda: None

    def payload(self):
        import aiohttp

        async def chunks():
            async for chunk in self.blob.chunks():
                self.check()
                yield chunk
        value = aiohttp.payload.AsyncIterablePayload(chunks(), content_type=self.mime)
        value._size = self.blob.size
        return value
