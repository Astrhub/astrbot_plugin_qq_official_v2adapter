"""Validated media upload receipts, scoped to one robot, scene, target and type."""
import asyncio
import hashlib
import math
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from uuid import uuid4

from ..errors import V2Error, unsupported
from ..extensions.state import digest
from ..protocol import RequestSpec
from .io import MAX_BYTES, UploadTransfer, bad, media_url
from .types import MediaInput

FILE_TYPES = {"image": 1, "video": 2, "record": 3, "file": 4}
SOFT_LIMITS = {"image": 20_000_000, "video": 30_000_000, "record": 20_000_000, "file": MAX_BYTES}
MAX_UPLOAD_ATTEMPTS = 16


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
    def __init__(self, identity, http, state, pool, *, settings=lambda: {}, guard=lambda: None, transfer=None, sleep=asyncio.sleep, clock=time.monotonic):
        self.identity, self.http, self.state, self.pool = identity, http, state, pool
        self.settings, self.guard = settings, guard
        self.sleep, self.clock = sleep, clock
        self.transfer = transfer or UploadTransfer(pool)
        self._put_slots = asyncio.Semaphore(self.transfer.max_tasks)
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
            kind = value.kind
            if blob.size > SOFT_LIMITS[kind]:
                if not value.allow_file_fallback or route.scene not in {"group", "c2c"}:
                    bad("media_soft_limit", "This media exceeds its soft limit and file fallback is disabled.", status=413)
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
        if not isinstance(data, dict) or not isinstance(data.get("file_info"), str) or not 1 <= len(data["file_info"]) <= 16384 or type(data.get("ttl")) is not int or data["ttl"] < 0:
            bad("invalid_upload_response", "QQ did not return a usable upload receipt.", phase="result_unknown", status=502)
        result = {"file_info": data["file_info"], "ttl": data["ttl"],
                  "expires_at": self.state.messages.now() + data["ttl"] if data["ttl"] else None}
        if isinstance(data.get("file_uuid"), str) and len(data["file_uuid"]) <= 512:
            result["file_uuid"] = data["file_uuid"]
        return result

    async def _wait_retry(self, error, deadline, delay):
        try:
            suggested = float(error.retry_after or 0)
        except (ValueError, TypeError):
            try:
                suggested = parsedate_to_datetime(error.retry_after).timestamp() - self.state.messages.now()
            except (ValueError, TypeError, AttributeError, OverflowError):
                suggested = 0
        if math.isfinite(suggested) and suggested >= 0:
            delay = max(delay, suggested)
        if self.clock() + delay >= deadline:
            return False
        await self.sleep(delay)
        return self.clock() < deadline

    async def _post(self, route, path, body, op_id, *, validate, check, kind, retry_timeout=300, retry_delay=1, deadline=None):
        deadline = self.clock() + retry_timeout if deadline is None else deadline
        attempt = 0
        last_error = None
        while True:
            if attempt and self.clock() >= deadline:
                raise last_error from None
            check()
            try:
                return await self.state.execute(self.http, RequestSpec(route.robot.environment, "POST", path, json_body=body),
                    op_id=f"{op_id}:{attempt}", kind=kind, validate=validate, before_send=check)
            except V2Error as exc:
                if exc.business_code == 40093002:
                    raise V2Error("upload_daily_capacity", "QQ daily file capacity is exhausted.", status=429, business_code=exc.business_code,
                                  trace_id=exc.trace_id, retry_after=exc.retry_after, http_status=exc.http_status, phase=exc.phase, operation_id=exc.operation_id) from None
                retryable = exc.business_code == 40093001 and exc.phase == "rejected" or exc.phase == "not_sent" and exc.code == "connect_failed"
                if not retryable or attempt + 1 >= MAX_UPLOAD_ATTEMPTS or retry_timeout <= 0 or not await self._wait_retry(exc, deadline, retry_delay):
                    raise
                last_error = exc
                attempt += 1

    async def _put_part(self, route, prepared, part, *, index, block, actual, key, op, parent_op, upload_id, config, deadline, check):
        attempt = 0
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                bad("upload_retry_timeout", "The upload part exhausted its retry window.", status=504)
            try:
                await asyncio.wait_for(self._put_slots.acquire(), remaining)
            except TimeoutError:
                bad("upload_retry_timeout", "The upload part could not acquire a transfer slot in time.", status=504)
            error = None
            try:
                check()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    bad("upload_retry_timeout", "The upload part exhausted its retry window.", status=504)
                put_id = op + ":put:" + str(index) + (f":retry:{attempt}" if attempt else "")
                fresh, _ = self.state.begin(route.robot, put_id, "media_put", digest([key, upload_id, part["index"], part["presigned_url"]]),
                    context={"scene": route.scene, "target": route.target, "parent_operation_id": parent_op, "part_index": part["index"]})
                if not fresh:
                    bad("operation_already_attempted", "Upload parts cannot be replayed.")
                self.state.attempt(route.robot, put_id)
                try:
                    await self.transfer.put(part["presigned_url"], blob=prepared.blob, offset=index * block, count=actual, request_seconds=remaining)
                    self.state.finish(route.robot, put_id, "succeeded", result={"size": actual})
                    return
                except BaseException as exc:
                    phase = getattr(exc, "phase", "result_unknown")
                    self.state.finish(route.robot, put_id, "not_sent" if phase == "not_sent" else "unknown")
                    if not isinstance(exc, V2Error):
                        raise
                    error = V2Error(exc.code, str(exc), status=exc.status, phase=phase, operation_id=put_id)
            finally:
                self._put_slots.release()
            if error.phase != "not_sent" or error.code != "media_network_failure" or attempt + 1 >= MAX_UPLOAD_ATTEMPTS or config["retry_timeout"] <= 0 or not await self._wait_retry(error, deadline, config["retry_delay"]):
                raise error from None
            attempt += 1

    async def _upload_parts(self, route, prepared, data, *, key, op, parent_op, prefix, check):
        block = data.get("block_size")
        if not isinstance(block, str) or not block.isdecimal() or not 1 <= int(block) <= MAX_BYTES:
            bad("invalid_upload_response", "Upload block size is invalid.")
        block = int(block)
        parts = data.get("parts")
        count = math.ceil(prepared.blob.size / block)
        if not isinstance(parts, list) or not 1 <= count <= 1024 or len(parts) != count:
            bad("invalid_upload_response", "Upload part count does not match the actual bytes.")
        supplied = data.get("upload_config", {})
        if not isinstance(supplied, dict):
            bad("invalid_upload_response", "Upload configuration is invalid.")
        config = {"concurrency": 1, "retry_timeout": 300, "retry_delay": 1}
        for field in config:
            if field in supplied:
                if type(supplied[field]) is not int or supplied[field] < 0:
                    bad("invalid_upload_response", "Upload scheduling fields are invalid.")
                config[field] = supplied[field]
        # Live QQ responses also use one-based indices; preserve them in acknowledgements.
        index_base = parts[0].get("index") if isinstance(parts[0], dict) else None
        if type(index_base) is not int or index_base not in (0, 1):
            bad("invalid_upload_response", "Upload part index base is invalid.")
        for index, part in enumerate(parts):
            actual = min(block, prepared.blob.size - index * block)
            if not isinstance(part, dict) or type(part.get("index")) is not int or part["index"] != index + index_base or not isinstance(part.get("block_size"), str) or part["block_size"] not in {str(actual), str(block)}:
                bad("invalid_upload_response", "Upload part indices or sizes are inconsistent.")
            media_url(part.get("presigned_url"), upload=True)
        pending = iter(enumerate(parts))

        async def worker():
            for index, part in pending:
                check()
                actual = min(block, prepared.blob.size - index * block)
                deadline = self.clock() + (config["retry_timeout"] or self.transfer.timeout)
                await self._put_part(route, prepared, part, index=index, block=block, actual=actual, key=key, op=op, parent_op=parent_op,
                    upload_id=data["upload_id"], config=config, deadline=deadline, check=check)
                md5 = hashlib.md5()
                async for chunk in prepared.blob.chunks(index * block, actual):
                    md5.update(chunk)
                await self._post(route, prefix + "/upload_part_finish", {"upload_id": data["upload_id"], "part_index": part["index"], "block_size": str(actual), "md5": md5.hexdigest()},
                    op + ":finish:" + str(index), validate=lambda d: None if d in (None, {}) else bad("invalid_upload_response", "Part completion response is not empty."),
                    check=check, kind="upload_part_finish", retry_timeout=config["retry_timeout"], retry_delay=config["retry_delay"], deadline=deadline)

        concurrency = max(1, min(config["concurrency"], self.transfer.max_tasks, len(parts)))
        try:
            async with asyncio.TaskGroup() as workers:
                for _ in range(concurrency):
                    workers.create_task(worker())
        except* V2Error as errors:
            raise errors.exceptions[0] from None
        return config

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
            body = {"file_type": FILE_TYPES[prepared.kind], "srv_send_msg": False, "file_name": prepared.input.name}
            config = {"retry_timeout": 300, "retry_delay": 1}
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
                config = await self._upload_parts(route, prepared, data, key=key, op=op, parent_op=parent_op, prefix=prefix, check=validate_source)
                body["upload_id"] = data["upload_id"]
            transient = {}
            def receipt(data):
                result = self._receipt(data)
                if prepared.blob is not None and prepared.kind in {"image", "video", "record"} and isinstance(data.get("raw_url"), str) and data["raw_url"]:
                    transient["raw_url"] = data["raw_url"]
                return result
            result = await self._post(route, prefix + "/files", body, op + ":files", validate=receipt, check=validate_source, kind="upload_files",
                retry_timeout=config["retry_timeout"], retry_delay=config["retry_delay"])
            validate_source()
            result = {**result, **transient, "kind": prepared.kind, "requested_kind": prepared.input.kind, "operation_id": op}
            expires = result["expires_at"]
            now = self.state.messages.now()
            if expires is not None and expires <= now:
                bad("upload_ticket_expired", "This retained upload receipt expired; a new explicit operation is required.")
            if len(self.cache) >= 64:
                self.cache.pop(next(iter(self.cache)))
            # Bound the memory cache independently of QQ's receipt lifetime.
            until = min(now + 86400, expires) if expires is not None else now + 86400
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
