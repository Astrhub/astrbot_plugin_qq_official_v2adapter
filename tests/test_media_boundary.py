"""Media input protocols and byte budgets remain bounded."""
import base64
import hashlib

import pytest

from v2.errors import V2Error

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhX8AAAAASUVORK5CYII=")


@pytest.mark.parametrize("url", ["ftp://media.test/a", "https://user:pass@media.test/a", "https:///missing", "https://media.test:0/a", "https://media.test/\n", "https://media.test\\evil/a", "https://media.test/\ud800"])
def test_media_url_syntax_rejects_ambiguous_or_credential_inputs(url):
    from v2.media.io import media_url
    with pytest.raises(V2Error) as error:
        media_url(url)
    assert error.value.code == "invalid_media_url"


@pytest.mark.parametrize("url", ["http://127.0.0.1/a", "https://[::1]/a", "https://198.18.0.89/a?sign=private", "https://media.test:8443/a#part"])
def test_media_urls_have_no_public_ip_or_port_gate(url):
    from v2.media.io import media_url
    assert media_url(url) == url


async def test_local_paths_and_symlinks_ignore_legacy_roots(tmp_path):
    from v2.media.io import BlobPool
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    original = allowed / "image.png"
    original.write_bytes(PNG)
    outside = tmp_path / "secret.png"
    outside.write_bytes(PNG)
    (allowed / "alias.png").symlink_to(outside)
    (allowed / "dir").symlink_to(tmp_path, target_is_directory=True)
    pool = BlobPool(tmp_path / "spool", total_bytes=1024)
    try:
        for value in (original, outside, allowed / "alias.png", allowed / "dir" / "secret.png"):
            good = await pool.load(str(value), roots=[str(allowed)], max_bytes=1024)
            assert good.size == len(PNG) and good.read(0, 1024) == PNG
            assert good.hashes()["md5"] == hashlib.md5(PNG).hexdigest()
            good.close()
            assert value.read_bytes() == PNG
        assert pool.used == 0 and not pool.blobs
    finally:
        pool.close()


async def test_base64_and_actual_file_bytes_have_independent_bounds(tmp_path):
    from v2.media.io import BlobPool
    pool = BlobPool(tmp_path / "spool", total_bytes=100)
    try:
        with pytest.raises(V2Error):
            await pool.load("base64://" + base64.b64encode(b"x" * 101).decode(), roots=[], max_bytes=200)
        with pytest.raises(V2Error):
            await pool.load("base64://broken!", roots=[], max_bytes=100)
        assert pool.used == 0
        blob = await pool.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
        assert blob.size == len(PNG)
        blob.close()
        assert pool.used == 0
    finally:
        pool.close()


async def test_md5_prefix_is_exactly_10002432_not_ten_mib(tmp_path):
    from v2.media.io import BlobPool
    content = b"a" * 10002432 + b"different tail" * 5000
    path = tmp_path / "large.bin"
    path.write_bytes(content)
    pool = BlobPool(tmp_path / "spool")
    try:
        blob = await pool.load(str(path), roots=[str(tmp_path)], max_bytes=20_000_000)
        values = blob.hashes()
        assert values["md5_10m"] == hashlib.md5(content[:10002432]).hexdigest()
        assert values["sha1"] == hashlib.sha1(content).hexdigest()
        assert values["md5_10m"] != hashlib.md5(content[:10 * 1024 * 1024]).hexdigest()
    finally:
        pool.close()
