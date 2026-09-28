"""Explicit native files and owned multipart handles share existing media and write ledgers."""

import hashlib
import math
from uuid import uuid4

from ...errors import V2Error, not_ready
from ..identifiers import child_operation_id, text_id
from .messages import fields, segment


def file_type(value):
    if type(value) is not int or value not in (1, 2, 3, 4):
        raise V2Error("invalid_media_type", "QQ file_type must be 1..4.")
    return value


def filename(value):
    if not isinstance(value, str) or not 1 <= len(value.encode()) <= 255 or any(c in value for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise V2Error("invalid_media_name", "Use a bounded filename without path separators.")
    return value


def file_receipt(data):
    if (not isinstance(data, dict) or not isinstance(data.get("file_info"), str) or not data["file_info"]
            or type(data.get("ttl")) is not int or data["ttl"] < 0):
        raise V2Error("invalid_upload_response", "QQ did not return usable file_info.", status=502, phase="result_unknown")


class NativeUploadHandle:
    """An owned local blob and one QQ upload task; close also on cancellation."""

    def __init__(self, view, route, prepared, parent_id, response):
        self.view, self.route, self.prepared = view, route, prepared
        self.parent_id, self.response = parent_id, response
        self.media = view._client._state.sender.media
        self.upload_id = text_id(response.get("upload_id"))
        block = response.get("block_size")
        if not isinstance(block, str) or not block.isdecimal() or not 1 <= int(block) <= 200_000_000:
            raise V2Error("invalid_upload_response", "QQ returned an invalid block size.", phase="result_unknown", status=502)
        self.block = int(block)
        parts = response.get("parts")
        count = math.ceil(prepared.blob.size / self.block)
        if not isinstance(parts, list) or not 1 <= count <= 1024 or len(parts) != count:
            raise V2Error("invalid_upload_response", "QQ returned an incomplete part list.", phase="result_unknown", status=502)
        from ...media.io import media_url
        base = parts[0].get("index") if isinstance(parts[0], dict) else None
        if type(base) is not int or base not in (0, 1):
            raise V2Error("invalid_upload_response", "QQ part index must start at 0 or 1.", phase="result_unknown", status=502)
        for position, part in enumerate(parts):
            actual = min(self.block, prepared.blob.size - position * self.block)
            if (not isinstance(part, dict) or type(part.get("index")) is not int or part["index"] != base + position
                    or part.get("block_size") not in (str(self.block), str(actual))):
                raise V2Error("invalid_upload_response", "QQ part indices or sizes are inconsistent.", phase="result_unknown", status=502)
            media_url(part.get("presigned_url"), upload=True)
        self.parts = parts
        self.index_base = base
        self.put_done, self.finish_done = set(), set()
        self.closed = False

    def _position(self, index):
        self.view._check()
        self.media.check(self.route)
        if self.closed or self.prepared.closed or self.prepared.binding != self.media.binding(self.route):
            raise V2Error("stale_generation", "The owned upload task is no longer valid.", status=409)
        if type(index) is not int or not self.index_base <= index < self.index_base + len(self.parts):
            raise V2Error("invalid_part_index", "Use a server-issued upload part index.")
        return index - self.index_base

    async def put_part(self, index: int):
        """PUT one server-issued part without QQ authentication or redirect following."""
        position = self._position(index)
        if index in self.put_done:
            raise V2Error("operation_already_attempted", "This part was already uploaded.")
        part = self.parts[position]
        actual = min(self.block, self.prepared.blob.size - position * self.block)
        from ...extensions.state import digest
        key = digest([self.route.robot.appid, self.route.scene, self.route.target, self.parent_id, self.upload_id])
        op = "media-" + hashlib.sha256(self.parent_id.encode()).hexdigest()[:40]
        def check():
            self._position(index)
        try:
            await self.media._put_part(self.route, self.prepared, part, index=position, block=self.block,
                actual=actual, key=key, op=op, parent_op=self.parent_id, upload_id=self.upload_id,
                config={"retry_timeout": 300, "retry_delay": 1}, deadline=self.media.clock() + 300, check=check)
            self.put_done.add(index)
        except BaseException:
            self.close()
            raise

    async def finish_part(self, index: int):
        """Confirm exactly the original server index after its PUT."""
        position = self._position(index)
        if index not in self.put_done or index in self.finish_done:
            raise V2Error("invalid_part_state", "A part must be PUT once before confirmation.")
        actual = min(self.block, self.prepared.blob.size - position * self.block)
        md5 = hashlib.md5()
        try:
            async for chunk in self.prepared.blob.chunks(position * self.block, actual):
                md5.update(chunk)
            method = self.view.post_group_upload_part_finish if self.route.scene == "group" else self.view.post_c2c_upload_part_finish
            await method(self.route.target, self.upload_id, index, str(actual), md5.hexdigest(),
                         operation_id=child_operation_id(self.parent_id, f"finish:{index}"))
            self.finish_done.add(index)
        except BaseException:
            self.close()
            raise

    async def complete(self, *, srv_send_msg=False):
        """Merge the fully confirmed upload and optionally record one real message."""
        self.media.check(self.route)
        if len(self.finish_done) != len(self.parts):
            raise V2Error("invalid_part_state", "All parts must be PUT and confirmed before merge.")
        method = self.view.post_group_file if self.route.scene == "group" else self.view.post_c2c_file
        try:
            return await method(self.route.target, file_type(self.media_kind), None, srv_send_msg,
                                file_name=self.prepared.input.name, upload_id=self.upload_id,
                                operation_id=child_operation_id(self.parent_id, "files"))
        finally:
            self.close()

    @property
    def media_kind(self):
        return {"image": 1, "video": 2, "record": 3, "file": 4}[self.prepared.kind]

    def close(self):
        if not self.closed:
            self.closed = True
            self.response = None
            self.parts = ()
            self.prepared.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()


class NativeMediaMixin:
    async def _native_file(self, scene, target, value, url, srv_send_msg, *, file_name=None,
                           upload_id=None, operation_id=None):
        from ...media.io import media_url
        file_type(value)
        if type(srv_send_msg) is not bool:
            raise V2Error("invalid_request", "srv_send_msg must be a boolean.")
        if upload_id is None and url in (None, "") or upload_id is not None and url not in (None, ""):
            raise V2Error("invalid_request", "Supply a QQ URL or upload_id; upload merge may send an empty URL.")
        if url not in (None, ""):
            media_url(url)
        if upload_id is not None:
            text_id(upload_id)
        if file_name is not None:
            filename(file_name)
        path = ("/v2/groups/" if scene == "group" else "/v2/users/") + segment(target) + "/files"
        body = fields(file_type=value, url=url, srv_send_msg=srv_send_msg, file_name=file_name, upload_id=upload_id)
        if srv_send_msg:
            self._check()
            sender = self._client._state.sender
            if sender is None:
                raise not_ready()
            return await sender.send_native(self._client.route_for(scene, target), path, body,
                                            operation_id=self._operation_id(operation_id), response_check=file_receipt,
                                            guard=self._check)
        return await self._native_write("POST", path, body, kind="upload_files", operation_id=operation_id,
                                        scene=scene, target=target, allow_message=True)

    async def post_group_file(self, group_openid: str, file_type: int, url: str | None = None,
                              srv_send_msg: bool = False, *, file_name: str | None = None,
                              upload_id: str | None = None, operation_id: str | None = None) -> dict:
        """Upload QQ group media; srv_send_msg also uses the send ledger."""
        return await self._native_file("group", group_openid, file_type, url, srv_send_msg,
                                       file_name=file_name, upload_id=upload_id, operation_id=operation_id)

    async def post_c2c_file(self, openid: str, file_type: int, url: str | None = None,
                            srv_send_msg: bool = False, *, file_name: str | None = None,
                            upload_id: str | None = None, operation_id: str | None = None) -> dict:
        """Upload QQ C2C media without fetching external URLs locally."""
        return await self._native_file("c2c", openid, file_type, url, srv_send_msg,
                                       file_name=file_name, upload_id=upload_id, operation_id=operation_id)

    async def _native_prepare(self, scene, target, value, file_size, file_name, md5, sha1, md5_10m, *, operation_id=None):
        file_type(value)
        filename(file_name)
        if (isinstance(file_size, bool) or not isinstance(file_size, (int, str)) or not str(file_size).isdecimal()
                or not 1 <= int(file_size) <= 200_000_000):
            raise V2Error("invalid_file_size", "QQ file_size must be 1..200000000 bytes.")
        for name, digest, width in (("md5", md5, 32), ("sha1", sha1, 40), ("md5_10m", md5_10m, 32)):
            if not isinstance(digest, str) or len(digest) != width or any(c not in "0123456789abcdefABCDEF" for c in digest):
                raise V2Error("invalid_hash", f"Invalid {name} checksum.")
        path = ("/v2/groups/" if scene == "group" else "/v2/users/") + segment(target) + "/upload_prepare"
        body = {"file_type": value, "file_size": str(file_size), "file_name": file_name,
                "md5": md5, "sha1": sha1, "md5_10m": md5_10m}
        return await self._native_write("POST", path, body, kind="upload_prepare", operation_id=operation_id,
                                        scene=scene, target=target, allow_message=True)

    async def post_group_upload_prepare(self, group_id: str, file_type: int, file_size: str,
                                        file_name: str, md5: str, sha1: str, md5_10m: str,
                                        *, operation_id: str | None = None) -> dict:
        """Obtain a group upload task and transient presigned parts."""
        return await self._native_prepare("group", group_id, file_type, file_size, file_name, md5, sha1, md5_10m,
                                          operation_id=operation_id)

    async def post_c2c_upload_prepare(self, user_id: str, file_type: int, file_size: str,
                                      file_name: str, md5: str, sha1: str, md5_10m: str,
                                      *, operation_id: str | None = None) -> dict:
        """Obtain a C2C upload task and transient presigned parts."""
        return await self._native_prepare("c2c", user_id, file_type, file_size, file_name, md5, sha1, md5_10m,
                                          operation_id=operation_id)

    async def _native_part_finish(self, scene, target, upload_id, part_index, block_size, md5, *, operation_id=None):
        text_id(upload_id)
        if type(part_index) is not int or part_index < 0 or isinstance(block_size, bool) or not isinstance(block_size, (int, str)) or not str(block_size).isdecimal() or int(block_size) <= 0:
            raise V2Error("invalid_part_index", "Use a server-issued nonnegative part index and actual byte count.")
        if not isinstance(md5, str) or len(md5) != 32 or any(c not in "0123456789abcdefABCDEF" for c in md5):
            raise V2Error("invalid_hash", "Part MD5 is invalid.")
        path = ("/v2/groups/" if scene == "group" else "/v2/users/") + segment(target) + "/upload_part_finish"
        body = {"upload_id": upload_id, "part_index": part_index, "block_size": str(block_size), "md5": md5}
        return await self._native_write("POST", path, body, kind="upload_part_finish", operation_id=operation_id,
                                        scene=scene, target=target, allow_message=True)

    async def post_group_upload_part_finish(self, group_id: str, upload_id: str, part_index: int,
                                            block_size: str, md5: str, *, operation_id: str | None = None):
        """Acknowledge one original group part index."""
        return await self._native_part_finish("group", group_id, upload_id, part_index, block_size, md5,
                                              operation_id=operation_id)

    async def post_c2c_upload_part_finish(self, user_id: str, upload_id: str, part_index: int,
                                          block_size: str, md5: str, *, operation_id: str | None = None):
        """Acknowledge one original C2C part index."""
        return await self._native_part_finish("c2c", user_id, upload_id, part_index, block_size, md5,
                                              operation_id=operation_id)

    async def begin_upload(self, scene: str, target: str, file: str, *, kind="image", name="upload",
                           operation_id: str | None = None) -> NativeUploadHandle:
        """Own one local file and return a task with explicit PUT/finish steps."""
        from ...media.types import MediaInput
        self._check()
        if scene not in {"group", "c2c"} or self._client._state.sender is None or self._client._state.sender.media is None:
            raise not_ready()
        route = self._client.route_for(scene, target)
        parent_id = text_id(self._operation_id(operation_id) or uuid4().hex)
        media = self._client._state.sender.media
        prepared = await media.prepare(route, MediaInput(kind, file, name))
        try:
            if prepared.blob is None:
                raise V2Error("invalid_media_input", "External URLs use the direct files endpoint; no local download is attempted.")
            work = self.with_options(owner=self._options.owner)
            hashes = prepared.blob.hashes()
            method = work.post_group_upload_prepare if scene == "group" else work.post_c2c_upload_prepare
            response = await method(target, file_type({"image": 1, "video": 2, "record": 3, "file": 4}[prepared.kind]),
                                    str(prepared.blob.size), prepared.input.name, hashes["md5"], hashes["sha1"],
                                    hashes["md5_10m"], operation_id=child_operation_id(parent_id, "prepare"))
            return NativeUploadHandle(work, route, prepared, parent_id, response)
        except BaseException:
            prepared.close()
            raise
