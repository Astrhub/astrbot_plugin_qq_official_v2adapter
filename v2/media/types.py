"""Explicit input policy; a chat string alone never authorizes a local read."""
from dataclasses import dataclass, field

from ..errors import V2Error


@dataclass(frozen=True)
class MediaInput:
    kind: str
    value: str
    name: str = "upload"
    allow_file_fallback: bool = field(default=True, kw_only=True)

    def validate(self):
        if not isinstance(self.kind, str) or self.kind not in {"image", "record", "video", "file"} or type(self.allow_file_fallback) is not bool:
            raise V2Error("invalid_media_input", "Unsupported media kind or local file fallback policy.")
        if not isinstance(self.value, str) or not self.value or len(self.value) > 12 * 1024 * 1024:
            raise V2Error("invalid_media_input", "Media input is missing or too large.")
        if not isinstance(self.name, str) or not 1 <= len(self.name.encode()) <= 255 or any(c in self.name for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in self.name) or self.name in {".", ".."}:
            raise V2Error("invalid_media_name", "Use a bounded filename without path separators or control characters.")
        return self


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
