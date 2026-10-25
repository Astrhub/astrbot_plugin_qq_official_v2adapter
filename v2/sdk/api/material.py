"""Platform material images: registered uploads, listing and deletion."""

from ...errors import V2Error
from ..identifiers import text_id
from .media import filename
from .messages import fields, segment

IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def hex_digest(value, name, width):
    if not isinstance(value, str) or len(value) != width or any(c not in "0123456789abcdefABCDEF" for c in value):
        raise V2Error("invalid_hash", f"Invalid {name} checksum.")
    return value


class NativeMaterialMixin:
    async def post_media_image_prepare(self, file_name: str, file_size: int, content_type: str,
                                       md5: str, sha1: str, *, operation_id: str | None = None) -> dict:
        """Register one platform image upload; the presigned PUT travels outside QQ APIs."""
        filename(file_name)
        if type(file_size) is not int or not 1 <= file_size <= MAX_IMAGE_BYTES:
            raise V2Error("invalid_file_size", "Platform image size must be 1..10485760 bytes.")
        if content_type not in IMAGE_CONTENT_TYPES:
            raise V2Error("invalid_media_type", "Platform images accept png, jpeg, gif or webp content types.")
        hex_digest(md5, "md5", 32)
        hex_digest(sha1, "sha1", 40)
        body = fields(file_name=file_name, file_size=file_size, content_type=content_type, md5=md5, sha1=sha1)
        return await self._native_write("POST", "/v2/media-images/prepare", body, kind="media_image_prepare",
                                        operation_id=operation_id, allow_message=True)

    async def post_media_image_complete(self, upload_id: str, md5: str, *, operation_id: str | None = None) -> dict:
        """Finish one upload and receive the qqbot-media reference; preview_url stays transient."""
        text_id(upload_id)
        hex_digest(md5, "md5", 32)
        body = fields(upload_id=upload_id, md5=md5)
        return await self._native_write("POST", "/v2/media-images/complete", body, kind="media_image_complete",
                                        operation_id=operation_id, allow_message=True)

    async def delete_media_image(self, image_id: str, *, operation_id: str | None = None) -> dict:
        """Delete one image and release its quota slot."""
        text_id(image_id)
        return await self._native_write("DELETE", f"/v2/media-images/{segment(image_id)}/delete", None,
                                        kind="media_image_delete", operation_id=operation_id, allow_message=True)
