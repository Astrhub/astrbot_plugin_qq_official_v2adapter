"""Media input protocols, byte budgets, path policies, format passthrough, and part indices."""

import base64
import copy
import hashlib
import json
from dataclasses import replace
from io import BytesIO

import pytest
from PIL import Image

from v2.errors import V2Error
from v2.media.io import Blob, BlobPool, media_url
from v2.media.types import MediaInput
from v2.models import SessionRoute
from v2.settings import DEFAULTS, SettingsStore, merge_patch, validate_settings

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhX8AAAAASUVORK5CYII=")

from test_media_upload import media as media  # noqa: E402
from test_media_upload import sending_core  # noqa: E402


def test_media_url_syntax_and_no_ip_or_port_gate():
    for bad in (
        "ftp://media.test/a", "https://user:pass@media.test/a", "https:///missing",
        "https://media.test:0/a", "https://media.test/\n", "https://media.test\\evil/a",
        "https://media.test/\ud800",
    ):
        with pytest.raises(V2Error) as error:
            media_url(bad)
        assert error.value.code == "invalid_media_url"

    for good in (
        "http://127.0.0.1/a", "https://[::1]/a",
        "https://198.18.0.89/a?sign=private", "https://media.test:8443/a#part",
    ):
        assert media_url(good) == good


async def test_local_paths_symlinks_base64_bounds_and_md5_10m_prefix(tmp_path):
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

    pool2 = BlobPool(tmp_path / "spool2", total_bytes=100)
    try:
        with pytest.raises(V2Error):
            await pool2.load("base64://" + base64.b64encode(b"x" * 101).decode(), roots=[], max_bytes=200)
        with pytest.raises(V2Error):
            await pool2.load("base64://broken!", roots=[], max_bytes=100)
        assert pool2.used == 0
        blob = await pool2.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
        assert blob.size == len(PNG)
        blob.close()
        assert pool2.used == 0
    finally:
        pool2.close()

    content = b"a" * 10002432 + b"different tail" * 5000
    large_path = tmp_path / "large.bin"
    large_path.write_bytes(content)
    pool3 = BlobPool(tmp_path / "spool3")
    try:
        blob3 = await pool3.load(str(large_path), roots=[str(tmp_path)], max_bytes=20_000_000)
        values = blob3.hashes()
        assert values["md5_10m"] == hashlib.md5(content[:10002432]).hexdigest()
        assert values["sha1"] == hashlib.sha1(content).hexdigest()
        assert values["md5_10m"] != hashlib.md5(content[:10 * 1024 * 1024]).hexdigest()
    finally:
        pool3.close()


async def test_local_media_path_policy_capacity_revocation_and_scope_ownership(media, tmp_path, monkeypatch):
    path = tmp_path / "本地 文件.png"
    path.write_bytes(PNG)
    for idx, policy in enumerate(({}, {"media_roots": []}, {"media_roots": ["/missing", r"Z:\old\media"]})):
        for kind in ("image", "file"):
            route = SessionRoute(media.identity.robot, "group", f"target-{idx}-{kind}")
            media.puts.clear()
            media.service.settings = lambda _p=policy: _p
            prepared = await media.service.prepare(route, MediaInput(kind, str(path), path.name))
            try:
                assert b"".join([chunk async for chunk in prepared.blob.chunks()]) == PNG
                receipt = await media.service.upload(route, prepared)
                assert receipt["kind"] == kind and b"".join(media.puts) == PNG
            finally:
                prepared.close()
    route = SessionRoute(media.identity.robot, "group", "target-revoke")
    assert not media.pool.blobs and media.pool.used == 0

    original_append = Blob.append
    for change in ("roots", "capacity"):
        media.puts.clear()
        policy = {"media_roots": [str(tmp_path)], "media_max_bytes": 1000}
        media.service.settings = lambda _p=policy: _p

        def changed(blob, chunk, _c=change, _p=policy):
            original_append(blob, chunk)
            _p.update({"media_roots": [r"Z:\missing"]} if _c == "roots" else {"media_max_bytes": 500})

        monkeypatch.setattr(Blob, "append", changed)
        if change == "capacity":
            with pytest.raises(V2Error) as error:
                await media.service.prepare(route, MediaInput("file", str(path)))
            assert error.value.code == "media_policy_changed"
        else:
            prepared = await media.service.prepare(route, MediaInput("file", str(path)))
            try:
                policy["media_roots"] = None
                await media.service.upload(route, prepared)
                assert b"".join(media.puts) == PNG
            finally:
                prepared.close()
    monkeypatch.setattr(Blob, "append", original_append)

    media.calls.clear()
    media.transfers.clear()
    orig_identity = media.service.identity
    for change in ("platform", "generation", "robot", "scene", "target", "capacity"):
        media.service.identity = orig_identity
        media.service.settings = lambda: {"media_max_bytes": 32_000_000}
        cur_route = SessionRoute(media.identity.robot, "group", "target")
        prepared = await media.service.prepare(cur_route, MediaInput("image", "https://assets.test/a.png"))
        try:
            if change in {"platform", "generation"}:
                field = "platform_id" if change == "platform" else "generation"
                media.service.identity = replace(orig_identity, **{field: "other"})
            elif change == "robot":
                cur_route = replace(cur_route, robot=replace(cur_route.robot, appid="other"))
            elif change == "scene":
                cur_route = replace(cur_route, scene="c2c")
            elif change == "target":
                cur_route = replace(cur_route, target="other")
            else:
                media.service.settings = lambda: {"media_max_bytes": 1000}
            with pytest.raises(V2Error) as error:
                await media.service.upload(cur_route, prepared)
            assert error.value.code in {"media_scope_mismatch", "stale_generation"}
        finally:
            prepared.close()
    media.service.identity = orig_identity


def test_legacy_roots_normalization_and_settings_store_compatibility(tmp_path):
    for legacy in ([], ["/missing"], [r"Z:\old\media"], ["/", "../old", "\u0000"], None, "obsolete", {"invalid": True}):
        value = copy.deepcopy(DEFAULTS)
        value["extensions"]["media_roots"] = legacy
        before = copy.deepcopy(value)
        normalized = validate_settings(value)
        assert "media_roots" not in normalized["extensions"]
        assert normalized["extensions"]["media_max_bytes"] == 32_000_000
        assert value == before

    for patch in (
        {"extensions": {"media_roots": [], "unknown": True}},
        {"extensions": {"media_roots": [], "media_max_bytes": 0}},
        {"extensions": {"media_roots": [], "media_max_bytes": 200_000_001}},
        {"extensions": {"media_roots": "x" * (256 * 1024)}},
    ):
        with pytest.raises(V2Error) as error:
            validate_settings(merge_patch(DEFAULTS, patch))
        assert error.value.code == "invalid_settings"

    path = tmp_path / "settings.db"
    store = SettingsStore(path)
    old = copy.deepcopy(DEFAULTS)
    old["title"] = "retained"
    old["extensions"].update(media_roots=[r"Z:\missing", "/", "../obsolete"], media_max_bytes=12345, typing_enabled=True)
    encoded = json.dumps(old)
    try:
        store.db.execute("INSERT INTO settings VALUES(?,?,?,?,?,?)", ("key", 4, encoded, 3, encoded, "old-editor"))
        store.db.execute("INSERT INTO versions VALUES(?,?,?)", ("key", 2, encoded))
        store.db.commit()
        current = store.get("key")
        assert "media_roots" not in current["draft"]["extensions"] and "media_roots" not in current["applied"]["extensions"]
        saved = store.mutate("key", 4, "new", operation="save", patch={"extensions": {"media_roots": {"obsolete": True}, "media_max_bytes": 23456}})
        assert saved["draft"]["extensions"]["media_max_bytes"] == 23456
        applied = store.mutate("key", 5, "new", operation="apply")
        assert applied["applied"] == saved["draft"]
        restored = store.mutate("key", 5, "new", operation="restore", restore_revision=2)
        assert restored["draft"]["extensions"]["media_max_bytes"] == 12345
    finally:
        store.close()


def _large_metadata_jpeg():
    with BytesIO() as output:
        Image.new("RGB", (16, 16)).save(output, format="JPEG")
        original = output.getvalue()
    segment = b"\xff\xef" + (60002).to_bytes(2, "big") + b"A" * 60000
    return original[:2] + segment * 2 + original[2:]


@pytest.mark.parametrize("event,source", [
    ("GROUP_AT_MESSAGE_CREATE", "file"),
    ("C2C_MESSAGE_CREATE", "base64"),
    ("AT_MESSAGE_CREATE", "file"),
])
async def test_large_app15_jpeg_and_opaque_containers_pass_unchanged(media, tmp_path, monkeypatch, event, source):
    data = _large_metadata_jpeg()
    path = tmp_path / "image with metadata.jpg"
    path.write_bytes(data)
    value = path.as_uri() if source == "file" else "base64://" + base64.b64encode(data).decode()

    def no_reencode(*args, **kwargs):
        raise AssertionError("Local preparation must not decode or transcode the caller's media")

    monkeypatch.setattr(Image, "open", no_reencode)
    media.modes[:] = [{"block_size": 65536}]
    core, chat = sending_core(media, event)
    try:
        result = await core.send(chat.route, [MediaInput("image", value, path.name)], source=chat.source, operation_id="large-jpeg")
        assert result["message_id"].startswith("actual-")
        if event == "AT_MESSAGE_CREATE":
            assert media.calls[0][1]["file_image"] == data
        else:
            assert b"".join(media.puts) == data
        assert not media.pool.blobs and media.pool.used == 0
    finally:
        await core.close()


async def test_opaque_containers_and_upstream_rejection_never_transcode_or_retry(media):
    core, chat = sending_core(media)
    try:
        for idx, (kind, data, wire_type) in enumerate((
            ("image", b"\xff\xd8no frame in local preview", 1),
            ("image", b"unrecognized image container", 1),
            ("video", b"\x00\x00\x00\x10ftyphevc\x00\x00\x00\x00", 2),
            ("record", b"unrecognized audio container", 3),
        )):
            media.calls.clear()
            media.puts.clear()
            result = await core.send(chat.route, [MediaInput(kind, "base64://" + base64.b64encode(data).decode())],
                                     source=chat.source, operation_id=f"opaque-{idx}")
            assert result["message_id"] == "actual-media-message"
            assert media.calls[0][1]["file_type"] == wire_type
            assert b"".join(media.puts) == data

        media.calls.clear()
        media.puts.clear()
        bad_data = b"not accepted by synthetic upstream"
        media.modes[:] = ["files_rejected"]
        value = MediaInput("image", "base64://" + base64.b64encode(bad_data).decode())
        with pytest.raises(V2Error) as error:
            await core.send(chat.route, [value], source=chat.source, operation_id="rejected-image")
        failure = error.value
        assert failure.code == "qq_api_error" and failure.business_code == 850026
        assert failure.phase == "not_sent" and failure.details["media_phase"] == "rejected"
        before = len(media.calls)
        with pytest.raises(V2Error):
            await core.send(chat.route, [value], source=chat.source, operation_id="rejected-image")
        assert len(media.calls) == before
    finally:
        await core.close()


async def test_media_part_indices_echo_0_or_1_base_and_reject_invalid_sequences(media):
    m = media
    for base in (0, 1):
        route = SessionRoute(m.identity.robot, "group", f"g-base-{base}")
        m.calls.clear()
        m.puts.clear()
        m.modes[:] = [{"part_indices": [base, base + 1]}]
        prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
        try:
            result = await m.service.upload(route, prepared, operation_id=f"base-{base}")
            assert result["file_info"] == "actual-file-receipt"
            assert b"".join(m.puts) == PNG
            finishes = [body for path, body in m.calls if path.endswith("upload_part_finish")]
            assert [body["part_index"] for body in finishes] == [base, base + 1]
        finally:
            prepared.close()
    route = SessionRoute(m.identity.robot, "group", "g-invalid")

    for idx, indices in enumerate(([True, 2], ["1", 2], [2, 3], [-1, 0], [1, 1], [1, 3], [0, 2], [2, 1], [None, 1])):
        m.calls.clear()
        m.puts.clear()
        m.modes[:] = [{"part_indices": indices}]
        prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
        try:
            with pytest.raises(V2Error) as error:
                await m.service.upload(route, prepared, operation_id=f"invalid-indices-{idx}")
            assert error.value.code == "invalid_upload_response" and not m.puts
        finally:
            prepared.close()
