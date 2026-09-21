"""Private local byte storage and credential-free official upload PUTs."""
import asyncio
import base64
import binascii
import hashlib
import os
import stat
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp
from astrbot.core.utils.http_ssl import build_ssl_context_with_certifi
from astrbot.core.utils.media_utils import MediaResolver, file_uri_to_path

from ..errors import V2Error

CHUNK = 65536
MD5_PREFIX = 10002432
MAX_BYTES = 200_000_000
BASE64_BYTES = 8 * 1024 * 1024


def bad(code, message, *, phase="not_sent", status=400):
    raise V2Error(code, message, status=status, phase=phase) from None


def media_url(value, *, upload=False):
    try:
        if not isinstance(value, str) or not value or len(value.encode()) > 8192 or any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
            raise ValueError
        parsed = urlsplit(value)
        if parsed.scheme not in ({"https"} if upload else {"http", "https"}) or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535 or upload and parsed.fragment:
            raise ValueError
        parsed.hostname.encode("idna")
        return value
    except (ValueError, UnicodeError):
        bad("invalid_media_url", "Media requires a bounded HTTP(S) URL without credentials; upload tickets require HTTPS.")


class Blob:
    def __init__(self, pool, maximum):
        self.pool, self.maximum = pool, maximum
        self.fp = tempfile.TemporaryFile(mode="w+b", dir=pool.directory)
        self.handle = uuid4().hex
        self.size = 0
        self.closed = False
        self._md5, self._sha1, self._prefix = hashlib.md5(), hashlib.sha1(), hashlib.md5()
        self._sha256 = hashlib.sha256()

    def append(self, chunk):
        if self.closed or self.pool.closed:
            bad("media_closed", "Media storage is closed.", status=503)
        if self.size + len(chunk) > self.maximum or self.pool.used + len(chunk) > self.pool.total_bytes:
            bad("media_too_large", "Actual media bytes exceed the file or shared disk budget.", status=413)
        self.fp.seek(self.size)
        self.fp.write(chunk)
        self._md5.update(chunk)
        self._sha1.update(chunk)
        self._sha256.update(chunk)
        if self.size < MD5_PREFIX:
            self._prefix.update(chunk[:MD5_PREFIX - self.size])
        self.size += len(chunk)
        self.pool.used += len(chunk)

    def read(self, offset, count):
        if self.closed or type(offset) is not int or type(count) is not int or offset < 0 or not 0 <= count <= CHUNK:
            bad("invalid_media_slice", "Media slices must be bounded and open.")
        self.fp.seek(offset)
        return self.fp.read(count)

    async def chunks(self, offset=0, count=None):
        remaining = self.size - offset if count is None else count
        if offset < 0 or remaining < 0 or offset + remaining > self.size:
            bad("invalid_media_slice", "Media slice is outside the validated file.")
        while remaining:
            chunk = self.read(offset, min(CHUNK, remaining))
            if not chunk:
                bad("media_truncated", "Media bytes changed during transfer.")
            yield chunk
            remaining -= len(chunk)
            offset += len(chunk)
            await asyncio.sleep(0)

    def hashes(self):
        return {"md5": self._md5.hexdigest(), "sha1": self._sha1.hexdigest(), "md5_10m": self._prefix.hexdigest(), "sha256": self._sha256.hexdigest()}

    def close(self):
        if not self.closed:
            self.closed = True
            self.fp.close()
            self.pool.used -= self.size
            self.pool.blobs.pop(self.handle, None)


def local_path(value, roots):
    if not isinstance(value, str) or len(value) > 4096 or "\x00" in value:
        bad("unsafe_media_path", "Invalid local media path.")
    uri = urlsplit(value)
    if uri.scheme in {"http", "https", "data", "base64"} or "://" in value and uri.scheme != "file":
        bad("invalid_media_input", "Only explicit local files enter the host resolver.")
    if uri.scheme == "file" and (uri.query or uri.fragment or uri.netloc and uri.netloc.lower() != "localhost" and not (os.name == "nt" and len(uri.netloc) == 2 and uri.netloc[1] == ":")):
        bad("unsafe_media_path", "Local file URI cannot name a network host, query or fragment.")
    path = Path(file_uri_to_path(value)).absolute()
    if path.drive.startswith("\\\\") or path.is_reserved() or path.drive and any(":" in p for p in path.parts[1:]):
        bad("unsafe_media_path", "Network, device and alternate-stream paths are not local media inputs.")
    allowed = [Path(root) for root in roots if Path(root).is_absolute() and Path(root) != Path(Path(root).anchor) and ".." not in Path(root).parts]
    allowed = [root for root in allowed if path.is_relative_to(root) and path != root]
    if not allowed:
        bad("media_path_not_authorized", "Local media requires an explicitly authorized directory.", status=403)
    # These are ordinary path checks, not an atomic sandbox against local writers.
    for part in (*reversed(path.parents), path):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            bad("unsafe_media_path", "Media paths cannot traverse links or reparse points.", status=403)
    path = path.resolve(strict=True)
    for root in allowed:
        try:
            root = root.resolve(strict=True)
            relative = path.relative_to(root)
            if relative.parts and os.path.samefile(root, path.parents[len(relative.parts) - 1]):
                return path
        except (OSError, ValueError):
            continue
    bad("media_path_not_authorized", "The resolved file is outside its authorized directory.", status=403)


def check_file(info, maximum):
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or not 0 < info.st_size <= maximum:
        bad("unsafe_media_file", "Media must be one regular, nonempty, size-bounded file without hard links.")


@asynccontextmanager
async def local_reader(value, roots, maximum):
    try:
        path = local_path(value, roots)
        original = path.stat()
        check_file(original, maximum)
        async with MediaResolver(path.as_uri(), media_type="file").open("rb") as reader:
            before = os.fstat(reader.fileno())
            check_file(before, maximum)
            if not os.path.samestat(original, before):
                bad("media_changed", "The local file changed before the host opened it.")
            yield reader, before
            current = local_path(value, roots).stat()
            check_file(current, maximum)
            if not os.path.samestat(before, current):
                bad("media_changed", "The local path changed while being copied.")
    except (OSError, ValueError):
        bad("unsafe_media_path", "The authorized local file could not be read through the host.", status=403)


class BlobPool:
    def __init__(self, directory, *, total_bytes=384_000_000, capacity=32):
        self.directory = Path(directory)
        if self.directory.is_symlink():
            bad("unsafe_media_path", "Media spool cannot be a symbolic link.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.total_bytes, self.capacity, self.used = total_bytes, capacity, 0
        self.blobs, self.closed = {}, False

    def create(self, maximum=MAX_BYTES):
        if self.closed or len(self.blobs) >= self.capacity:
            bad("media_capacity", "Media resources are closed or at capacity.", status=429)
        if type(maximum) is not int or not 1 <= maximum <= MAX_BYTES:
            bad("invalid_media_limit", "Invalid media byte limit.")
        blob = Blob(self, maximum)
        self.blobs[blob.handle] = blob
        return blob

    async def load(self, value, *, roots, max_bytes):
        blob = self.create(max_bytes)
        try:
            if not isinstance(value, str):
                bad("invalid_media_input", "Media input must be a file URI or encoded string.")
            if value.startswith("base64://") or value.startswith("data:"):
                if value.startswith("base64://"):
                    encoded = value[9:]
                else:
                    prefix, sep, encoded = value.partition(",")
                    if not sep or not prefix.endswith(";base64"):
                        bad("invalid_media_input", "Only base64 data URIs are supported.")
                limit = min(BASE64_BYTES, max_bytes)
                if len(encoded) > ((limit + 2) // 3) * 4 or "=" in encoded[:-2] or len(encoded) % 4:
                    bad("media_too_large", "Base64 input exceeds its separate byte budget or has invalid padding.", status=413)
                for start in range(0, len(encoded), CHUNK):
                    blob.append(base64.b64decode(encoded[start:start + CHUNK], validate=True))
                    if blob.size > limit:
                        bad("media_too_large", "Decoded media exceeds the base64 budget.", status=413)
                    await asyncio.sleep(0)
            else:
                async with local_reader(value, roots, max_bytes) as (reader, before):
                    while chunk := reader.read(CHUNK):
                        blob.append(chunk)
                        await asyncio.sleep(0)
                    after = os.fstat(reader.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ino, before.st_nlink) != (after.st_size, after.st_mtime_ns, after.st_ino, after.st_nlink) or blob.size != before.st_size:
                        bad("media_changed", "Local media changed while being copied.")
            if not blob.size:
                bad("empty_media", "Empty media is not uploaded.")
            return blob
        except (binascii.Error, UnicodeError, ValueError):
            blob.close()
            bad("invalid_base64", "Media base64 encoding is invalid.")
        except BaseException:
            blob.close()
            raise

    def close(self):
        self.closed = True
        for blob in tuple(self.blobs.values()):
            blob.close()


class UploadTransfer:
    def __init__(self, pool, *, session_factory=None, timeout=30, max_tasks=8):
        self.pool = pool
        self.session_factory = session_factory or self._session
        self.timeout, self.max_tasks = timeout, max_tasks
        self.tasks, self.closed = set(), False

    @staticmethod
    def _session():
        return aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=build_ssl_context_with_certifi(), force_close=True, limit=1),
            trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=30), headers={"Accept-Encoding": "identity"})

    async def put(self, url, *, blob, offset=0, count=None):
        media_url(url, upload=True)
        if self.closed or len(self.tasks) >= self.max_tasks:
            bad("media_transfer_unavailable", "Media upload is closed or at capacity.", status=429)
        if blob.closed or self.pool.blobs.get(blob.handle) is not blob:
            bad("invalid_media_handle", "PUT requires an owned local media resource.")
        length = blob.size - offset if count is None else count
        if type(offset) is not int or type(length) is not int or offset < 0 or length <= 0 or offset + length > blob.size:
            bad("invalid_media_slice", "Upload slice is outside the owned file.")
        task = asyncio.current_task()
        self.tasks.add(task)
        started = False
        try:
            async with asyncio.timeout(self.timeout):
                headers = {"Accept-Encoding": "identity", "Content-Length": str(length), "Content-Type": "application/octet-stream"}
                async with self.session_factory() as session:
                    started = True
                    async with session.request("PUT", url, headers=headers, data=blob.chunks(offset, length), allow_redirects=False) as response:
                        if 300 <= response.status < 400:
                            bad("media_redirect_rejected", "Upload redirects are not permitted.", phase="result_unknown")
                        if not 200 <= response.status < 300:
                            bad("media_http_error", "Upload peer rejected the transfer.", status=502, phase="result_unknown")
                        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                            bad("media_encoding_rejected", "Compressed upload responses are not accepted.")
                        declared = response.headers.get("Content-Length")
                        if declared is not None and (not declared.isdecimal() or int(declared) > 65536):
                            bad("media_too_large", "Upload response exceeds its byte limit.", status=413)
                        size = 0
                        async for chunk in response.content.iter_chunked(CHUNK):
                            size += len(chunk)
                            if size > 65536:
                                bad("media_too_large", "Actual upload response exceeds its byte limit.", status=413)
                        if declared is not None and size != int(declared):
                            bad("media_truncated", "Upload response was truncated.")
        except asyncio.CancelledError as exc:
            exc.phase = "result_unknown" if started else "not_sent"
            raise
        except V2Error as exc:
            if started:
                raise V2Error(exc.code, str(exc), status=exc.status, phase="result_unknown") from None
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError) as exc:
            phase = "result_unknown" if started and not isinstance(exc, aiohttp.ClientConnectorError) else "not_sent"
            bad("media_network_failure", "Upload failed; inspect its operation before retrying.", status=502, phase=phase)
        finally:
            self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
