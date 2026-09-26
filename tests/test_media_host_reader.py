"""Real host file materialization, without downloading or delegating Base64."""
import asyncio
import base64
import hashlib
import os
from contextlib import asynccontextmanager

import pytest
from astrbot.core.utils import media_utils as host

from v2.errors import V2Error
from v2.media import io


@pytest.mark.parametrize("reference", ["absolute", "relative", "uri"])
@pytest.mark.parametrize("legacy_roots", ["absent", "empty", "unrelated", "windows"])
async def test_host_file_context_reads_bounded_chunks_and_keeps_source(tmp_path, monkeypatch, reference, legacy_roots):
    path = tmp_path / "中文 空格.bin"
    content = b"byte sample" * 15000
    path.write_bytes(content)
    pool = io.BlobPool(tmp_path / "spool")
    opened, refs, sizes = [], [], []
    assert io.MediaResolver is host.MediaResolver and io.file_uri_to_path is host.file_uri_to_path
    original = host.MediaResolver.open
    @asynccontextmanager
    async def recording(resolver, mode="rb", **kwargs):
        refs.append((resolver.media_ref, resolver.media_type, mode))
        async with original(resolver, mode, **kwargs) as reader:
            opened.append(reader)
            read = reader.read
            def bounded(size=-1):
                assert 0 < size <= io.CHUNK
                sizes.append(size)
                return read(size)
            monkeypatch.setattr(reader, "read", bounded)
            yield reader
    monkeypatch.setattr(host.MediaResolver, "open", recording)
    try:
        monkeypatch.chdir(tmp_path)
        value = {"absolute": str(path), "relative": path.name, "uri": path.as_uri()}[reference]
        options = {} if legacy_roots == "absent" else {"roots": {"empty": [], "unrelated": [str(tmp_path / "missing")], "windows": [r"Z:\old\media"]}[legacy_roots]}
        blob = await pool.load(value, max_bytes=1_000_000, **options)
        assert b"".join([chunk async for chunk in blob.chunks()]) == content
        assert refs == [(path.as_uri(), "file", "rb")]
        assert len(sizes) >= 3 and all(f.closed for f in opened)
        assert blob.hashes()["sha256"] == hashlib.sha256(content).hexdigest()
        blob.close()
        assert pool.used == 0 and path.read_bytes() == content
    finally:
        pool.close()


@pytest.mark.parametrize("ref", ["https://example.test/a", "HTTP://example.test/a", "file://server/share/a", "ftp://example.test/a", "file:///tmp/a?query=1"])
async def test_invalid_local_reference_never_enters_resolver(tmp_path, monkeypatch, ref):
    def forbidden(*args, **kwargs):
        raise AssertionError("Invalid/remote references cannot enter the host resolver")
    monkeypatch.setattr(io, "MediaResolver", forbidden)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        with pytest.raises(V2Error):
            await pool.load(ref, roots=[str(tmp_path)], max_bytes=100)
        assert not pool.blobs
    finally:
        pool.close()


async def test_base64_is_not_guessed_or_decoded_by_host(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("V2 handles bounded Base64 without a resolver temp copy")
    monkeypatch.setattr(io, "MediaResolver", forbidden)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        blob = await pool.load("base64://" + base64.b64encode(b"bytes").decode(), roots=[], max_bytes=100)
        assert blob.size == 5
        blob.close()
        with pytest.raises(V2Error):
            await pool.load(base64.b64encode(b"not a local file").decode(), roots=[str(tmp_path)], max_bytes=100)
    finally:
        pool.close()


async def test_hardlinks_and_parent_paths_read_regular_targets(tmp_path):
    directory = tmp_path / "files"
    directory.mkdir()
    path = directory / "sample"
    path.write_bytes(b"file")
    alias = directory / "alias"
    os.link(path, alias)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        for value in (path, alias, directory / ".." / "files" / "sample"):
            blob = await pool.load(str(value), max_bytes=100)
            assert blob.read(0, 100) == b"file"
            blob.close()
        assert path.read_bytes() == alias.read_bytes() == b"file"
        assert not pool.blobs and pool.used == 0
    finally:
        pool.close()


async def test_nonregular_missing_empty_and_oversize_never_enter_host(tmp_path, monkeypatch):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    alias = tmp_path / "alias"
    alias.symlink_to(fifo)
    empty, large = tmp_path / "empty", tmp_path / "large"
    empty.touch()
    large.write_bytes(b"x" * 101)
    def forbidden(*args, **kwargs):
        raise AssertionError("Special or invalid files must be rejected before synchronous host open")
    monkeypatch.setattr(io, "MediaResolver", forbidden)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        for value in (tmp_path, fifo, alias, tmp_path / "missing", empty, large, "/dev/null"):
            with pytest.raises(V2Error):
                await pool.load(str(value), max_bytes=100)
            assert not pool.blobs and pool.used == 0
    finally:
        pool.close()


@pytest.mark.parametrize("failure", ["mutation", "read_error", "cancel"])
async def test_host_failure_and_cancel_close_stream_and_blob(tmp_path, monkeypatch, failure):
    path = tmp_path / "sample"
    path.write_bytes(b"x" * 500000)
    pool = io.BlobPool(tmp_path / "spool")
    original, opened = host.MediaResolver.open, []
    entered = asyncio.Event()
    @asynccontextmanager
    async def recording(resolver, mode="rb", **kwargs):
        async with original(resolver, mode, **kwargs) as reader:
            opened.append(reader)
            entered.set()
            if failure == "read_error":
                def broken(size):
                    raise OSError("fixture read failed")
                monkeypatch.setattr(reader, "read", broken)
            yield reader
    monkeypatch.setattr(host.MediaResolver, "open", recording)
    append = io.Blob.append
    changed = False
    def mutate(blob, chunk):
        nonlocal changed
        append(blob, chunk)
        if not changed:
            changed = True
            with path.open("ab") as writer:
                writer.write(b"changed")
    if failure == "mutation":
        monkeypatch.setattr(io.Blob, "append", mutate)
    try:
        task = asyncio.create_task(pool.load(str(path), roots=[str(tmp_path)], max_bytes=1_000_000))
        await asyncio.wait_for(entered.wait(), 2)
        if failure == "cancel":
            task.cancel()
        with pytest.raises(asyncio.CancelledError if failure == "cancel" else V2Error):
            await task
        assert opened and all(f.closed for f in opened)
        assert not pool.blobs and pool.used == 0
    finally:
        pool.close()


async def test_file_replaced_before_host_open_is_detected(tmp_path, monkeypatch):
    path, replacement = tmp_path / "sample", tmp_path / "new"
    path.write_bytes(b"authorized bytes")
    replacement.write_bytes(b"changed bytes")
    original = host.MediaResolver.open
    @asynccontextmanager
    async def swapped(resolver, mode="rb", **kwargs):
        replacement.replace(path)
        async with original(resolver, mode, **kwargs) as reader:
            yield reader
    monkeypatch.setattr(host.MediaResolver, "open", swapped)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        with pytest.raises(V2Error) as error:
            await pool.load(str(path), roots=[str(tmp_path)], max_bytes=100)
        assert error.value.code == "media_changed" and pool.used == 0
    finally:
        pool.close()


async def test_missing_unrelated_root_does_not_block_authorized_input(tmp_path):
    path = tmp_path / "file"
    path.write_bytes(b"allowed")
    pool = io.BlobPool(tmp_path / "spool")
    try:
        blob = await pool.load(str(path), roots=[str(tmp_path / "missing"), str(tmp_path)], max_bytes=100)
        blob.close()
        assert pool.used == 0
    finally:
        pool.close()


@pytest.mark.parametrize("linked", [False, True])
async def test_path_identity_change_during_copy_releases_bytes(tmp_path, monkeypatch, linked):
    source, replacement = tmp_path / "source", tmp_path / "replacement"
    source.write_bytes(b"a" * (io.CHUNK + 1))
    replacement.write_bytes(b"b" * (io.CHUNK + 1))
    path = tmp_path / "link" if linked else source
    if linked:
        path.symlink_to(source)
    original, changed = io.Blob.append, False
    def swapped(blob, chunk):
        nonlocal changed
        original(blob, chunk)
        if not changed:
            changed = True
            if linked:
                path.unlink()
                path.symlink_to(replacement)
            else:
                replacement.replace(path)
    monkeypatch.setattr(io.Blob, "append", swapped)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        with pytest.raises(V2Error) as error:
            await pool.load(str(path), max_bytes=1_000_000)
        assert error.value.code == "media_changed"
        assert pool.used == 0 and not pool.blobs
        assert path.read_bytes() == b"b" * (io.CHUNK + 1)
    finally:
        pool.close()


async def test_local_files_share_total_pool_budget(tmp_path):
    path = tmp_path / "sample"
    path.write_bytes(b"bytes")
    pool = io.BlobPool(tmp_path / "spool", total_bytes=9)
    try:
        first = await pool.load(str(path), max_bytes=10)
        with pytest.raises(V2Error) as error:
            await pool.load(str(path), max_bytes=10)
        assert error.value.code == "media_too_large"
        assert pool.used == 5 and list(pool.blobs.values()) == [first]
        first.close()
        assert pool.used == 0 and not pool.blobs and path.read_bytes() == b"bytes"
    finally:
        pool.close()


async def test_link_count_change_during_copy_does_not_change_file_content(tmp_path, monkeypatch):
    path = tmp_path / "file"
    content = b"x" * (io.CHUNK + 1)
    path.write_bytes(content)
    alias = tmp_path / "alias"
    original = io.Blob.append
    def linked(blob, chunk):
        original(blob, chunk)
        if not alias.exists():
            os.link(path, alias)
    monkeypatch.setattr(io.Blob, "append", linked)
    pool = io.BlobPool(tmp_path / "spool")
    try:
        blob = await pool.load(str(path), max_bytes=len(content))
        assert blob.hashes()["sha256"] == hashlib.sha256(content).hexdigest()
        blob.close()
        assert path.read_bytes() == alias.read_bytes() == content
        assert pool.used == 0 and not pool.blobs
    finally:
        pool.close()
