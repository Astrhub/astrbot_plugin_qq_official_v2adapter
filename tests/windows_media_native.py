"""Native Windows-only media checks against staged source and disposable samples."""
import ast
import asyncio
import base64
import copy
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

STAGE = Path(__file__).parent.resolve()
CORE = Path(sys.argv.pop(2))
sys.path[:0] = [str(STAGE / "source"), sys.argv.pop(1), str(CORE)]
os.chdir(STAGE)
os.environ.update(ASTRBOT_ROOT=str(STAGE / "host_root"), TESTING="true", ASTRBOT_TEST_MODE="true", NO_COLOR="1")
sys.dont_write_bytecode = True
assert os.name == "nt", "Run with Windows Python, not an emulated os.name"


def no_external_network(event, args):
    if event in {"socket.connect", "socket.bind"}:
        address = args[1]
        if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError("Native media checks cannot access external networks")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        if path.is_relative_to(CORE) and not path.is_relative_to(CORE / "astrbot"):
            raise AssertionError("Only host source may be read from core")
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND) and not path.is_relative_to(STAGE):
            raise AssertionError("Test writes must remain in the disposable root")
    if event in {"os.mkdir", "os.remove", "os.rmdir"} and not Path(args[0]).absolute().is_relative_to(STAGE):
        raise AssertionError("Filesystem changes must remain in the disposable root")
    if event == "socket.getaddrinfo" and args[0] not in {"localhost", "127.0.0.1", "::1"}:
        raise AssertionError("Native media checks cannot resolve external names")


sys.addaudithook(no_external_network)


def load_host_modules():
    import astrbot
    from astrbot.core.utils import media_utils as host_media

    from v2.errors import V2Error
    from v2.media import io
    from v2.settings import DEFAULTS, validate_settings

    assert Path(host_media.__file__).resolve().is_relative_to(CORE.resolve())
    assert os.environ["ASTRBOT_ROOT"].startswith(str(STAGE))
    return astrbot, host_media, V2Error, io, DEFAULTS, validate_settings


astrbot, host_media, V2Error, io, DEFAULTS, validate_settings = load_host_modules()


class NativeMedia(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="sample-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.allowed = self.base / "Media 中文"
        self.allowed.mkdir()
        self.path = self.allowed / "Nested" / "test 空格.bin"
        self.path.parent.mkdir()
        self.content = b"authorized sample only" * 10000
        self.path.write_bytes(self.content)
        self.pool = io.BlobPool(self.base / "spool")
        self.addCleanup(self.pool.close)

    async def load(self, value=None, *, maximum=1_000_000, roots=None):
        return await self.pool.load(str(value or self.path), roots=roots, max_bytes=maximum)

    async def test_authorized_file(self):
        blob = await self.load()
        self.assertEqual(blob.size, len(self.content))
        self.assertEqual(blob.hashes()["sha256"], hashlib.sha256(self.content).hexdigest())
        blob.close()
        self.assertEqual(self.pool.used, 0)

    async def test_unused_missing_root_does_not_block_authorized_file(self):
        blob = await self.load(roots=[str(self.base / "not-created"), str(self.allowed)])
        self.assertEqual(blob.size, len(self.content))
        blob.close()

    async def test_real_host_uri_and_case_handling(self):
        self.assertIs(io.MediaResolver, host_media.MediaResolver)
        self.assertIs(io.file_uri_to_path, host_media.file_uri_to_path)
        calls, opened = [], []
        original = host_media.MediaResolver.open
        @asynccontextmanager
        async def recorded(resolver, mode="rb", **kwargs):
            calls.append((resolver.media_ref, resolver.media_type, mode))
            async with original(resolver, mode, **kwargs) as stream:
                opened.append(stream)
                yield stream
        with patch.object(host_media.MediaResolver, "open", recorded):
            for value in (self.path.as_uri(), str(self.path).upper(), str(self.path).replace("\\", "/")):
                blob = await self.load(value)
                self.assertEqual(blob.hashes()["md5"], hashlib.md5(self.content).hexdigest())
                blob.close()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(uri.startswith("file:///") and kind == "file" and mode == "rb" for uri, kind, mode in calls))
        self.assertTrue(all(stream.closed for stream in opened))
        self.assertTrue(self.path.exists())

    async def test_legacy_roots_do_not_restrict_local_files(self):
        neighbor = self.base / "Media 中文-other"
        neighbor.mkdir()
        outside = neighbor / "other.bin"
        outside.write_bytes(b"outside")
        for value, roots in ((outside, []), (self.path, [self.path.anchor]), (self.path, ["Z:\\Allowed"]),
                             (self.allowed / ".." / neighbor.name / outside.name, [str(self.allowed)])):
            with self.subTest(value=str(value)):
                blob = await self.load(value, roots=roots)
                self.assertEqual(blob.hashes()["sha256"], hashlib.sha256(value.read_bytes()).hexdigest())
                blob.close()
        self.assertEqual(self.pool.used, 0)

    async def test_network_device_and_alternate_stream_inputs(self):
        for value in (r"\\server\share\file.bin", r"\\?\C:\file.bin", r"\\.\NUL", str(self.allowed / "NUL"),
                      str(self.path) + ":hidden", "file://server/share/file.bin", self.path.as_uri() + "?query=1"):
            with self.subTest(value=value):
                with self.assertRaises(V2Error):
                    await self.load(value)
        self.assertFalse(self.pool.blobs)

    async def test_directory_empty_and_oversize(self):
        empty = self.allowed / "empty.bin"
        empty.touch()
        for value in (self.path.parent, empty):
            with self.assertRaises(V2Error):
                await self.load(value)
        with self.assertRaises(V2Error):
            await self.load(maximum=10)
        self.assertFalse(self.pool.blobs)

    async def test_hardlinks_read_unchanged(self):
        alias = self.allowed / "hard.bin"
        os.link(self.path, alias)
        for value in (self.path, alias):
            blob = await self.load(value)
            self.assertEqual(blob.hashes()["sha256"], hashlib.sha256(self.content).hexdigest())
            blob.close()
        self.assertEqual(self.pool.used, 0)

    async def test_junction_reads_local_file(self):
        other = self.base / "outside"
        other.mkdir()
        (other / "file.bin").write_bytes(b"not authorized")
        junction = self.allowed / "junction"
        made = await asyncio.to_thread(subprocess.run, [r"C:\Windows\System32\cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(other)], capture_output=True)
        self.assertEqual(made.returncode, 0, "Could not create own temporary junction")
        try:
            self.assertTrue(junction.is_junction())
            blob = await self.load(junction / "file.bin")
            self.assertEqual(blob.read(0, 100), b"not authorized")
            blob.close()
        finally:
            junction.rmdir()

    async def test_file_symlink_reads_local_file(self):
        link = self.allowed / "link.bin"
        try:
            link.symlink_to(self.path)
        except OSError as exc:
            if exc.winerror == 1314:
                self.skipTest("Windows user lacks symlink privilege; junction and Linux symlink cases remain tested")
            raise
        blob = await self.load(link)
        self.assertEqual(blob.hashes()["sha256"], hashlib.sha256(self.content).hexdigest())
        blob.close()

    async def test_directory_symlink_reads_local_file(self):
        link = self.allowed / "linked-dir"
        try:
            link.symlink_to(self.path.parent, target_is_directory=True)
        except OSError as exc:
            if exc.winerror == 1314:
                self.skipTest("Windows user lacks symlink privilege; actual junction covers directory links")
            raise
        blob = await self.load(link / self.path.name)
        self.assertEqual(blob.hashes()["sha256"], hashlib.sha256(self.content).hexdigest())
        blob.close()

    async def test_content_mutation_detected_and_reader_closed(self):
        original, changed = io.Blob.append, False
        def append(blob, chunk):
            nonlocal changed
            original(blob, chunk)
            if not changed:
                changed = True
                with self.path.open("ab") as writer:
                    writer.write(b"changed")
        with patch.object(io.Blob, "append", append):
            with self.assertRaises(V2Error):
                await self.load()
        self.assertTrue(changed)
        self.assertFalse(self.pool.blobs)
        self.path.rename(self.allowed / "after-failure.bin")

    async def test_read_error_closes_host_reader(self):
        original, opened = host_media.MediaResolver.open, []
        @asynccontextmanager
        async def failing(resolver, mode="rb", **kwargs):
            async with original(resolver, mode, **kwargs) as reader:
                opened.append(reader)
                with patch.object(reader, "read", side_effect=OSError("fixture read error")):
                    yield reader
        with patch.object(host_media.MediaResolver, "open", failing):
            with self.assertRaises(V2Error):
                await self.load()
        self.assertTrue(opened and all(reader.closed for reader in opened))
        self.assertFalse(self.pool.blobs)

    async def test_cancellation_closes_host_reader_and_blob(self):
        original, opened = host_media.MediaResolver.open, []
        entered = asyncio.Event()
        @asynccontextmanager
        async def recorded(resolver, mode="rb", **kwargs):
            async with original(resolver, mode, **kwargs) as reader:
                opened.append(reader)
                entered.set()
                yield reader
        with patch.object(host_media.MediaResolver, "open", recorded):
            task = asyncio.create_task(self.load())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(opened and all(reader.closed for reader in opened))
        self.assertFalse(self.pool.blobs)

    async def test_base64_bypasses_host_materialization(self):
        with patch.object(host_media.MediaResolver, "open", side_effect=AssertionError("Base64 must remain bounded V2 decoding")):
            blob = await self.load("base64://" + base64.b64encode(self.content).decode())
            self.assertEqual(blob.size, len(self.content))
            blob.close()

    async def test_urls_never_enter_host_downloader(self):
        with patch.object(host_media.MediaResolver, "open", side_effect=AssertionError("URL must not enter resolver")):
            for value in ("https://example.test/a", "HTTP://example.test/a", "file://server/share/a"):
                with self.assertRaises(V2Error):
                    await self.load(value)
        self.assertFalse(self.pool.blobs)

    async def test_settings_ignore_legacy_roots(self):
        config = copy.deepcopy(DEFAULTS)
        self.assertNotIn("media_roots", config["extensions"])
        for root in (self.path.anchor, r"\\server\share\files", str(self.allowed / "NUL"), r"Z:\missing"):
            config["extensions"]["media_roots"] = [root]
            self.assertEqual(validate_settings(config), DEFAULTS)


if __name__ == "__main__":
    print("NATIVE", sys.version, "os.name=", os.name, "dir_fd=", os.open in os.supports_dir_fd, "O_NOFOLLOW=", hasattr(os, "O_NOFOLLOW"), flush=True)
    print("HOST", astrbot.__version__, "module=", host_media.__file__, flush=True)
    selected = {"file_uri_to_path", "MediaResolver", "ResolvedMediaFile", "_materialize_media_ref"}
    tree = ast.parse(Path(host_media.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if getattr(node, "name", None) in selected:
            print("HOST_AST", node.name, hashlib.sha256(ast.dump(node, include_attributes=False).encode()).hexdigest(), flush=True)
    unittest.main(verbosity=2)
