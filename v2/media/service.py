"""Validated media upload receipts, scoped to one robot, scene, target and type."""
import asyncio
import hashlib
import math
import struct
from dataclasses import dataclass
from urllib.parse import quote
from uuid import uuid4

from ..errors import V2Error, unsupported
from ..extensions.state import digest
from ..protocol import RequestSpec
from .io import MAX_BYTES, UploadTransfer, bad, media_url
from .types import MediaInput

FILE_TYPES = {"image": 1, "video": 2, "record": 3, "file": 4}
SOFT_LIMITS = {"image": 20_000_000, "video": 30_000_000, "record": 20_000_000, "file": MAX_BYTES}


def inspect_media(blob, kind):
    if kind == "file":
        blob.mime = "application/octet-stream"
        return
    header = blob.read(0, min(blob.size, 65536))
    mime, width, height = None, None, None
    if header.startswith(b"\x89PNG\r\n\x1a\n") and len(header) >= 33 and header[12:16] == b"IHDR":
        width, height = struct.unpack(">II", header[16:24])
        if blob.size < 45 or b"IEND" not in blob.read(max(0, blob.size - 12), 12):
            bad("invalid_media_format", "PNG media is truncated.")
        mime = "image/png"
    elif header.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 4 <= len(header):
            if header[pos] != 255:
                break
            marker = header[pos + 1]
            if marker == 255:
                pos += 1
                continue
            length = int.from_bytes(header[pos + 2:pos + 4], "big")
            if length < 2 or pos + 2 + length > len(header):
                break
            if marker in {0xC0, 0xC1, 0xC2} and length >= 8:
                height, width = struct.unpack(">HH", header[pos + 5:pos + 9])
                mime = "image/jpeg"
                break
            pos += 2 + length
        if blob.read(max(0, blob.size - 2), 2) != b"\xff\xd9":
            bad("invalid_media_format", "JPEG media is truncated.")
    elif len(header) >= 16 and header[4:8] == b"ftyp" and header[8:12] in {b"isom", b"iso2", b"mp41", b"mp42", b"avc1", b"M4V ", b"dash"}:
        if not 16 <= int.from_bytes(header[:4], "big") <= blob.size:
            bad("invalid_media_format", "MP4 container header is invalid.")
        mime = "video/mp4"
    elif header.startswith((b"#!SILK_V3", b"\x02#!SILK_V3")):
        mime = "audio/silk"
    if width is not None and (not width or not height or width > 16384 or height > 16384 or width * height > 40_000_000):
        bad("image_dimensions_exceeded", "Image dimensions exceed the bounded image policy.")
    expected = {"image": {"image/png", "image/jpeg"}, "video": {"video/mp4"}, "record": {"audio/silk"}}
    if kind != "file" and mime not in expected[kind]:
        bad("media_type_mismatch", "Actual bytes do not match PNG/JPEG, MP4 or SILK for the requested media type.")
    blob.mime, blob.width, blob.height = mime or "application/octet-stream", width, height


@dataclass
class PreparedMedia:
    binding: tuple
    input: MediaInput
    blob: object
    kind: str
    release: object = lambda blob: None
    closed: bool = False

    def descriptor(self):
        result = {"kind": self.kind, "requested_kind": self.input.kind, "name": self.input.name}
        if self.blob is None:
            return {**result, "source": "url", "url_digest": hashlib.sha256(self.input.value.encode()).hexdigest()}
        return {**result, "source": "bytes", "sha256": self.blob.hashes()["sha256"], "size": self.blob.size}

    def close(self):
        self.closed = True
        if self.blob is not None:
            self.blob.close()
            self.release(self.blob)


class MediaService:
    def __init__(self, identity, http, state, pool, *, settings=lambda: {}, guard=lambda: None, transfer=None, sleep=asyncio.sleep):
        self.identity, self.http, self.state, self.pool = identity, http, state, pool
        self.settings, self.guard = settings, guard
        self.sleep = sleep
        self.transfer = transfer or UploadTransfer(pool)
        self.tasks, self.cache = set(), {}
        self.blobs = set()
        self.closed = False

    def check(self, route):
        self.guard()
        if self.closed or route.robot != self.identity.robot:
            raise V2Error("stale_generation", "Media operation is not owned by this live instance.", status=409)
        self.http.check()

    def binding(self, route):
        policy = self.settings()
        return self.identity.platform_id, self.identity.generation, route.robot, route.scene, route.target, digest([policy.get("media_roots", []), policy.get("media_max_bytes", 32_000_000)])

    async def prepare(self, route, value):
        self.check(route)
        value.validate()
        if len(self.tasks) >= 8:
            bad("media_capacity", "Too many pending media inputs.", status=429)
        if route.scene in {"channel", "dm"} and value.kind != "image":
            raise unsupported("Channel/DM has no verified file_info mapping for this media type.")
        remote = value.value.lower().startswith(("http:", "https:"))
        if remote:
            media_url(value.value)
            return PreparedMedia(self.binding(route), value, None, value.kind)
        if route.scene == "dm":
            raise unsupported("DM documents JSON image URLs, not local file_image multipart uploads.")
        task = asyncio.current_task()
        self.tasks.add(task)
        blob = None
        try:
            policy = self.settings()
            binding = self.binding(route)
            maximum = min(MAX_BYTES, policy.get("media_max_bytes", 32_000_000))
            blob = await asyncio.wait_for(self.pool.load(value.value, roots=policy.get("media_roots", []), max_bytes=maximum), timeout=30)
            inspect_media(blob, value.kind)
            kind = value.kind
            if blob.size > SOFT_LIMITS[kind]:
                if not value.allow_file_fallback or route.scene not in {"group", "c2c"}:
                    bad("media_soft_limit", "The media would become a file; explicit file fallback is required.", status=413)
                kind = "file"
            self.check(route)
            if binding != self.binding(route):
                bad("media_policy_changed", "Media authorization changed while reading; nothing was uploaded.", status=409)
            self.blobs.add(blob)
            return PreparedMedia(binding, value, blob, kind, self.blobs.discard)
        except BaseException:
            if blob:
                blob.close()
            raise
        finally:
            self.tasks.discard(task)

    def _receipt(self, data):
        if not isinstance(data, dict) or not isinstance(data.get("file_info"), str) or not 1 <= len(data["file_info"]) <= 16384 or type(data.get("ttl")) is not int or not 0 <= data["ttl"] <= 31536000:
            bad("invalid_upload_response", "QQ did not return a usable upload receipt.", phase="result_unknown", status=502)
        result = {"file_info": data["file_info"], "ttl": data["ttl"],
                  "expires_at": self.state.messages.now() + min(data["ttl"] or 86400, 86400)}
        if isinstance(data.get("file_uuid"), str) and len(data["file_uuid"]) <= 512:
            result["file_uuid"] = data["file_uuid"]
        return result

    async def _post(self, route, path, body, op_id, *, validate, check, kind):
        for attempt in range(2):
            delay = self.state.rate_delay(route.robot, kind, 50 if path.endswith("/files") else 10, 1)
            if delay:
                await self.sleep(delay)
            check()
            try:
                return await self.state.execute(self.http, RequestSpec(route.robot.environment, "POST", path, json_body=body),
                    op_id=f"{op_id}:{attempt}", kind=kind, validate=validate, before_send=check,
                    rate=(kind, 50 if path.endswith("/files") else 10, 1))
            except V2Error as exc:
                if exc.business_code == 40093002:
                    raise V2Error("upload_daily_capacity", "QQ daily file capacity is exhausted.", status=429, business_code=exc.business_code,
                                  trace_id=exc.trace_id, http_status=exc.http_status, phase=exc.phase, operation_id=exc.operation_id) from None
                if exc.business_code != 40093001 or exc.phase != "rejected" or attempt:
                    raise
                await self.sleep(1)
        raise AssertionError("upload retry bound")

    async def upload(self, route, prepared, *, operation_id=None, check=lambda: None):
        self.check(route)
        if route.scene not in {"group", "c2c"}:
            raise unsupported("Only group/C2C file_info upload endpoints exist in this contract.")
        if prepared.binding != self.binding(route):
            bad("media_scope_mismatch", "Prepared media cannot cross instances, generations or targets.", status=409)
        if prepared.closed or prepared.blob is not None and (prepared.blob.closed or self.pool.blobs.get(prepared.blob.handle) is not prepared.blob):
            bad("invalid_media_handle", "Upload requires an owned open media resource.")
        if len(self.tasks) >= 8:
            bad("media_capacity", "Too many pending uploads.", status=429)
        task = asyncio.current_task()
        self.tasks.add(task)
        parent_op = operation_id or uuid4().hex
        op = "media-" + hashlib.sha256(parent_op.encode()).hexdigest()[:40]
        key = digest([route.robot.appid, route.robot.environment, route.scene, route.target, prepared.descriptor(),
                      parent_op if prepared.blob is None else None])
        now = self.state.messages.now()
        self.cache = {k: v for k, v in self.cache.items() if v[0] > now}
        prefix = ("/v2/groups/" if route.scene == "group" else "/v2/users/") + quote(route.target, safe="")
        def validate_source():
            self.check(route)
            if prepared.binding != self.binding(route):
                bad("media_policy_changed", "Media authorization changed before upload.", status=409)
            check()
        try:
            validate_source()
            if key in self.cache:
                return dict(self.cache[key][1])
            async with asyncio.timeout(180):
                body = {"file_type": FILE_TYPES[prepared.kind], "srv_send_msg": False, "file_name": prepared.input.name}
                if prepared.blob is None:
                    body["url"] = prepared.input.value
                else:
                    response_holder = []
                    def capture(data):
                        if not isinstance(data, dict) or not isinstance(data.get("upload_id"), str) or not 1 <= len(data["upload_id"]) <= 512:
                            bad("invalid_upload_response", "Upload preparation did not return a task ID.", phase="result_unknown", status=502)
                        response_holder.append(data)
                        return {"upload_id": data["upload_id"]}
                    hashes = prepared.blob.hashes()
                    await self._post(route, prefix + "/upload_prepare", {"file_type": FILE_TYPES[prepared.kind], "file_name": prepared.input.name,
                        "file_size": str(prepared.blob.size), **{k: hashes[k] for k in ("md5", "sha1", "md5_10m")}}, op + ":prepare",
                        validate=capture, check=validate_source, kind="upload_prepare")
                    if not response_holder:
                        bad("upload_ticket_expired", "Upload URLs are never persisted or replayed; create a new explicit operation.")
                    data = response_holder[0]
                    block = data.get("block_size")
                    if not isinstance(block, str) or not block.isdecimal() or not 1 <= int(block) <= MAX_BYTES:
                        bad("invalid_upload_response", "Upload block size is invalid.")
                    block = int(block)
                    parts = data.get("parts")
                    count = math.ceil(prepared.blob.size / block)
                    if not isinstance(parts, list) or not 1 <= count <= 1024 or len(parts) != count:
                        bad("invalid_upload_response", "Upload part count does not match the actual bytes.")
                    config = data.get("upload_config", {})
                    if not isinstance(config, dict):
                        bad("invalid_upload_response", "Upload configuration is invalid.")
                    for field in ("concurrency", "retry_timeout", "retry_delay"):
                        if field in config and (type(config[field]) is not int or config[field] < 0):
                            bad("invalid_upload_response", "Upload scheduling fields are invalid.")
                    # Live QQ responses also use one-based indices; preserve them in acknowledgements.
                    index_base = parts[0].get("index") if isinstance(parts[0], dict) else None
                    if type(index_base) is not int or index_base not in (0, 1):
                        bad("invalid_upload_response", "Upload part index base is invalid.")
                    for index, part in enumerate(parts):
                        actual = min(block, prepared.blob.size - index * block)
                        if not isinstance(part, dict) or type(part.get("index")) is not int or part["index"] != index + index_base or not isinstance(part.get("block_size"), str) or part["block_size"] not in {str(actual), str(block)}:
                            bad("invalid_upload_response", "Upload part indices or sizes are inconsistent.")
                        media_url(part.get("presigned_url"), upload=True)
                    for index, part in enumerate(parts):
                        validate_source()
                        actual = min(block, prepared.blob.size - index * block)
                        put_id = op + ":put:" + str(index)
                        fresh, _ = self.state.begin(route.robot, put_id, "media_put", digest([key, data["upload_id"], part["index"], part["presigned_url"]]),
                            context={"scene": route.scene, "target": route.target, "parent_operation_id": parent_op, "part_index": part["index"]})
                        if not fresh:
                            bad("operation_already_attempted", "Upload parts cannot be replayed.")
                        self.state.attempt(route.robot, put_id)
                        try:
                            await self.transfer.put(part["presigned_url"], blob=prepared.blob, offset=index * block, count=actual)
                            self.state.finish(route.robot, put_id, "succeeded", result={"size": actual})
                        except BaseException as exc:
                            phase = getattr(exc, "phase", "result_unknown")
                            self.state.finish(route.robot, put_id, "not_sent" if phase == "not_sent" else "unknown")
                            if isinstance(exc, V2Error):
                                raise V2Error(exc.code, str(exc), status=exc.status, phase=phase, operation_id=put_id) from None
                            raise
                        md5 = hashlib.md5()
                        async for chunk in prepared.blob.chunks(index * block, actual):
                            md5.update(chunk)
                        await self._post(route, prefix + "/upload_part_finish", {"upload_id": data["upload_id"], "part_index": part["index"], "block_size": str(actual), "md5": md5.hexdigest()},
                            op + ":finish:" + str(index), validate=lambda d: None if d in (None, {}) else bad("invalid_upload_response", "Part completion response is not empty."), check=validate_source, kind="upload_part_finish")
                    body["upload_id"] = data["upload_id"]
                result = await self._post(route, prefix + "/files", body, op + ":files", validate=self._receipt, check=validate_source, kind="upload_files")
                validate_source()
                result = {**result, "kind": prepared.kind, "requested_kind": prepared.input.kind, "operation_id": op}
                until = result["expires_at"]
                if until <= self.state.messages.now():
                    bad("upload_ticket_expired", "This retained upload receipt expired; a new explicit operation is required.")
                if len(self.cache) >= 64:
                    self.cache.pop(next(iter(self.cache)))
                self.cache[key] = until, result
                return dict(result)
        finally:
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.transfer.close()
        self.cache.clear()
        for blob in tuple(self.blobs):
            blob.close()
        self.blobs.clear()
