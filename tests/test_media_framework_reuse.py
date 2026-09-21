"""QQ judges media formats; local preparation only owns unchanged bounded bytes."""
import base64
import hashlib
from io import BytesIO

import pytest
from PIL import Image
from test_media_upload import media as media
from test_media_upload import sending_core

from v2.errors import V2Error
from v2.media.types import MediaInput


def large_metadata_jpeg():
    with BytesIO() as output:
        Image.new("RGB", (16, 16)).save(output, format="JPEG")
        original = output.getvalue()
    segment = b"\xff\xef" + (60002).to_bytes(2, "big") + b"A" * 60000
    data = original[:2] + segment * 2 + original[2:]
    with Image.open(BytesIO(data)) as decoded:
        decoded.load()
        assert decoded.format == "JPEG" and decoded.size == (16, 16)
        with Image.open(BytesIO(original)) as source:
            assert decoded.tobytes() == source.tobytes()
    assert len(data) == 120639
    return data


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE", "AT_MESSAGE_CREATE"])
@pytest.mark.parametrize("source", ["file", "base64"])
async def test_large_app15_jpeg_passes_unchanged_end_to_end(media, tmp_path, monkeypatch, event, source):
    data = large_metadata_jpeg()
    path = tmp_path / "image with metadata.jpg"
    path.write_bytes(data)
    value = path.as_uri() if source == "file" else "base64://" + base64.b64encode(data).decode()
    def no_reencode(*args, **kwargs):
        raise AssertionError("Local preparation must not decode or transcode the caller's media")
    monkeypatch.setattr(Image, "open", no_reencode)
    media.modes[:] = [{"block_size": 65536}]
    core, chat = sending_core(media, event)
    descriptors, part_types = [], []
    prepare, request = media.service.prepare, media.http.request
    async def tracked_prepare(route, value):
        prepared = await prepare(route, value)
        descriptors.append(prepared.descriptor())
        return prepared
    async def tracked_request(spec, **kwargs):
        if spec.multipart:
            part_types.append(spec.multipart["file_image"].mime)
        return await request(spec, **kwargs)
    monkeypatch.setattr(media.service, "prepare", tracked_prepare)
    monkeypatch.setattr(media.http, "request", tracked_request)
    try:
        result = await core.send(chat.route, [MediaInput("image", value, path.name)], source=chat.source, operation_id="large-jpeg")
        assert result["message_id"].startswith("actual-")
        assert descriptors == [{"kind": "image", "requested_kind": "image", "name": path.name,
                                "source": "bytes", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}]
        if event == "AT_MESSAGE_CREATE":
            assert media.calls[0][1]["file_image"] == data
            assert part_types == ["application/octet-stream"] and not media.puts
        else:
            assert b"".join(media.puts) == data and not part_types
            body = media.calls[0][1]
            assert body == {"file_type": 1, "file_size": str(len(data)), "file_name": path.name,
                            "md5": hashlib.md5(data).hexdigest(), "sha1": hashlib.sha1(data).hexdigest(),
                            "md5_10m": hashlib.md5(data).hexdigest()}
            assert [body["part_index"] for path, body in media.calls if path.endswith("upload_part_finish")] == [0, 1]
        assert path.read_bytes() == data
        assert media.store.operation(chat.route.robot, "large-jpeg")["state"] == "sent"
        assert media.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
        assert not media.pool.blobs and media.pool.used == 0
    finally:
        await core.close()


@pytest.mark.parametrize("kind,data,wire_type", [
    ("image", b"\xff\xd8no frame in local preview", 1),
    ("image", b"\x89PNG\r\n\x1a\nno tail", 1),
    ("image", b"unrecognized image container", 1),
    ("video", b"\x00\x00\x00\x10ftyphevc\x00\x00\x00\x00", 2),
    ("record", b"unrecognized audio container", 3),
])
async def test_no_local_container_allowlist_or_false_validation(media, kind, data, wire_type):
    core, chat = sending_core(media)
    try:
        result = await core.send(chat.route, [MediaInput(kind, "base64://" + base64.b64encode(data).decode())], source=chat.source)
        assert result["message_id"] == "actual-media-message"
        assert media.calls[0][1]["file_type"] == wire_type
        assert media.calls[0][1]["md5"] == hashlib.md5(data).hexdigest()
        assert b"".join(media.puts) == data
        assert media.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
        assert not media.pool.blobs
    finally:
        await core.close()


async def test_local_container_rejection_is_upstream_not_transcode_or_retry(media):
    core, chat = sending_core(media)
    data = b"not accepted by synthetic upstream"
    media.modes[:] = ["files_rejected"]
    value = MediaInput("image", "base64://" + base64.b64encode(data).decode())
    try:
        with pytest.raises(V2Error) as error:
            await core.send(chat.route, [value], source=chat.source, operation_id="rejected-image")
        failure = error.value
        assert failure.code == "qq_api_error" and failure.business_code == 850026
        assert failure.http_status == 400 and failure.trace_id == "fixture-files" and failure.retry_after == "3"
        assert failure.phase == "not_sent" and failure.details["media_phase"] == "rejected"
        assert media.state.operation(chat.route.robot, failure.details["media_operation_id"])["state"] == "rejected"
        assert media.store.operation(chat.route.robot, "rejected-image")["state"] == "not_sent"
        assert media.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
        assert b"".join(media.puts) == data and not any(p.endswith("/messages") for p, _ in media.calls)
        before = len(media.calls)
        with pytest.raises(V2Error):
            await core.send(chat.route, [value], source=chat.source, operation_id="rejected-image")
        assert len(media.calls) == before and not media.pool.blobs and not core.tasks
    finally:
        await core.close()
