"""Media URLs are delegated to QQ; only local bytes enter the upload pipeline."""
import asyncio
import hashlib

import pytest
from test_media_upload import media as media
from test_media_upload import sending_core

from v2.errors import V2Error
from v2.media.service import PreparedMedia
from v2.media.types import MediaInput


async def test_prepared_media_default_release_is_instance_callback(media):
    value = MediaInput("file", "base64://eA==")
    blob = await media.pool.load(value.value, roots=[], max_bytes=1)
    prepared = PreparedMedia((), value, blob, "file")
    assert prepared.release is vars(prepared)["release"]
    prepared.close()
    prepared.close()
    assert prepared.closed and blob.closed and not media.pool.blobs and media.pool.used == 0


@pytest.fixture
async def no_local_media_io(media, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("URL media must not allocate blobs, resolve DNS or contact its origin")
    from PIL import Image

    from v2.media import io
    monkeypatch.setattr(Image, "open", forbidden)
    monkeypatch.setattr(media.pool, "load", forbidden)
    monkeypatch.setattr(io, "MediaResolver", forbidden)
    monkeypatch.setattr(media.pool, "create", forbidden)
    monkeypatch.setattr(media.service.transfer, "session_factory", forbidden)
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", forbidden)
    if hasattr(media.service.transfer, "guard"):
        monkeypatch.setattr(media.service.transfer.guard, "resolve", forbidden)


@pytest.mark.parametrize("event,prefix", [("GROUP_AT_MESSAGE_CREATE", "/v2/groups/group-one"), ("C2C_MESSAGE_CREATE", "/v2/users/user-one")])
@pytest.mark.parametrize("kind,file_type", [("image", 1), ("video", 2), ("record", 3), ("file", 4)])
async def test_external_url_is_qq_only_without_local_bytes(media, no_local_media_io, event, prefix, kind, file_type):
    m = media
    core, chat = sending_core(m, event)
    url = "http://198.18.0.89:8080/asset?signature=private%2Fkey&x=1&x=2+3#view"
    m.service.settings = lambda: {"media_max_bytes": 1}
    result = await core.send(chat.route, [MediaInput(kind, url, "named.bin")], source=chat.source, operation_id="external")
    assert m.calls[0] == (prefix + "/files", {"file_type": file_type, "file_name": "named.bin", "srv_send_msg": False, "url": url})
    assert m.calls[1][0] == prefix + "/messages" and m.calls[1][1]["media"] == {"file_info": "actual-file-receipt"}
    assert [c[:2] for c in m.http.session.calls] == [("POST", "https://api.bot.qq.com/app/getAppAccessToken"), ("POST", "https://api.bot.qq.com" + prefix + "/files"), ("POST", "https://api.bot.qq.com" + prefix + "/messages")]
    assert result["message_id"] == "actual-media-message" and result["media"]["source"] == "url"
    assert not {"size", "mime", "sha256", "md5"} & result["media"].keys()
    assert not m.transfers and not m.puts and not m.pool.blobs
    dump = "\n".join(m.store.db.iterdump())
    assert "private%2Fkey" not in dump and url not in dump
    await core.close()


@pytest.mark.parametrize("event,prefix", [("AT_MESSAGE_CREATE", "/channels/group-one"), ("DIRECT_MESSAGE_CREATE", "/dms/group-one")])
@pytest.mark.parametrize("scheme", ["http", "https"])
async def test_channel_dm_image_url_is_json_not_multipart(media, no_local_media_io, event, prefix, scheme):
    core, chat = sending_core(media, event)
    url = scheme + "://assets.test/image?key=a%2Bb&v=1&v=2"
    await core.send(chat.route, [MediaInput("image", url)], source=chat.source)
    assert media.calls == [(prefix + "/messages", {"image": url, "msg_id": "msg-one"})]
    assert media.http.session.calls[-1][:2] == ("POST", "https://api.bot.qq.com" + prefix + "/messages")
    assert not media.transfers and not media.pool.blobs
    assert url not in "\n".join(media.store.db.iterdump())
    await core.close()


async def test_url_operations_bind_source_not_assumed_remote_content(media, no_local_media_io):
    m = media
    core, chat = sending_core(m)
    url = "https://assets.test/image?token=private"
    value = MediaInput("image", url)
    first = await core.send(chat.route, [value], source=chat.source, operation_id="one")
    before = len(m.calls)
    assert await core.send(chat.route, [value], source=chat.source, operation_id="one") == first
    assert len(m.calls) == before
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, [MediaInput("image", url + "-changed")], source=chat.source, operation_id="one")
    assert error.value.code == "operation_conflict" and len(m.calls) == before
    await core.send(chat.route, [value], source=chat.source, operation_id="two")
    assert sum(p.endswith("/files") for p, _ in m.calls) == 2
    prepared = await m.service.prepare(chat.route, value)
    assert prepared.blob is None
    assert prepared.descriptor() == {"kind": "image", "requested_kind": "image", "name": "upload", "source": "url", "url_digest": hashlib.sha256(url.encode()).hexdigest()}
    prepared.close()
    await core.close()


@pytest.mark.parametrize("mode,code", [("unknown", "invalid_upload_response"), ("expired", "reply_expired")])
async def test_url_failures_never_download_or_replay(media, no_local_media_io, mode, code):
    core, chat = sending_core(media)
    media.modes[:] = [mode]
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, [MediaInput("image", "https://assets.test/a")], source=chat.source, operation_id="url-failed")
    assert error.value.code == code and error.value.phase == "not_sent"
    before = len(media.calls)
    with pytest.raises(V2Error):
        await core.send(chat.route, [MediaInput("image", "https://assets.test/a")], source=chat.source, operation_id="url-failed")
    assert len(media.calls) == before == 1 and not media.transfers
    await core.close()


async def test_removed_mode_is_not_silently_accepted(media):
    core, chat = sending_core(media)
    with pytest.raises(V2Error):
        await core.send(chat.route, [{"type": "image", "data": {"file": "https://assets.test/a", "upload_mode": "url"}}], source=chat.source)
    assert not media.calls and not media.transfers
    await core.close()


async def test_url_platform_rejection_keeps_business_status_and_no_fallback(media, no_local_media_io):
    core, chat = sending_core(media)
    media.modes[:] = ["files_rejected"]
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, [MediaInput("image", "https://assets.test/fails?sign=private")], source=chat.source)
    failure = error.value
    assert failure.business_code == 850026 and failure.http_status == 400 and failure.trace_id == "fixture-files"
    assert failure.retry_after == "3" and failure.phase == "not_sent" and failure.details["media_phase"] == "rejected"
    assert len(media.calls) == 1 and not media.transfers and "private" not in str(failure)
    await core.close()


async def test_url_cancel_preserves_unknown_upload_and_does_not_replay(media, no_local_media_io):
    core, chat = sending_core(media)
    media.modes[:] = ["wait_files"]
    task = asyncio.create_task(core.send(chat.route, [MediaInput("image", "https://assets.test/a")], source=chat.source, operation_id="cancel-url"))
    await asyncio.wait_for(media.entered.wait(), 2)
    await core.close()
    with pytest.raises(asyncio.CancelledError):
        await task
    operation = media.state.recent(chat.route.robot)[0]
    assert operation["state"] == "unknown" and operation["kind"] == "upload_files"
    assert media.store.operation(chat.route.robot, "cancel-url")["state"] == "not_sent"
    assert media.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    assert len(media.calls) == 1 and not media.transfers and not media.service.tasks
    with pytest.raises(V2Error):
        prepared = await media.service.prepare(chat.route, MediaInput("image", "https://assets.test/a"))
        try:
            await media.service.upload(chat.route, prepared, operation_id="cancel-url")
        finally:
            prepared.close()
    assert len(media.calls) == 1


async def test_url_instance_rotation_after_files_blocks_message_and_identity_is_unchanged(media, no_local_media_io, monkeypatch):
    core, chat = sending_core(media)
    before = list(media.store.db.execute("SELECT * FROM identities"))
    request = media.http.request
    def stopped():
        raise V2Error("stale_generation", "fixture stopped")
    async def rotate(spec, **kwargs):
        response = await request(spec, **kwargs)
        if spec.path.endswith("/files"):
            media.service.guard = stopped
        return response
    monkeypatch.setattr(media.http, "request", rotate)
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, [MediaInput("image", "https://assets.test/a")], source=chat.source)
    assert error.value.code == "stale_generation" and error.value.phase == "not_sent"
    assert len(media.calls) == 1 and not media.transfers
    assert list(media.store.db.execute("SELECT * FROM identities")) == before
    assert media.state.recent(chat.route.robot)[0]["state"] == "succeeded"
    await core.close()


async def test_removed_native_mode_and_positional_policy_fail_before_io():
    from v2.client import NativeView
    with pytest.raises(TypeError):
        await NativeView(None).send_file("group", "g", "https://assets.test/a", upload_mode="url")
    with pytest.raises(TypeError):
        MediaInput("image", "https://assets.test/a", "upload", "chunks")


@pytest.mark.parametrize("event", ["AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
@pytest.mark.parametrize("kind", ["record", "video", "file"])
async def test_simplified_urls_do_not_expand_channel_dm_media(media, no_local_media_io, event, kind):
    core, chat = sending_core(media, event)
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, [MediaInput(kind, "https://assets.test/a")], source=chat.source)
    assert error.value.code == "unsupported" and not media.calls
    await core.close()


@pytest.mark.parametrize("form", ["array", "cq", "component"])
async def test_media_url_query_survives_public_message_representations(media, no_local_media_io, form):
    from astrbot.core.message.components import Image
    from astrbot.core.message.message_event_result import MessageChain

    from v2.client import V2Client
    core, chat = sending_core(media)
    url = "https://assets.test/image?x=one,two&x=a%2Bb&name=%E4%B8%AD"
    if form == "component":
        message = MessageChain([Image.fromURL(url)])
    elif form == "array":
        message = [{"type": "image", "data": {"file": url}}]
    else:
        encoded = url.replace("&", "&amp;").replace(",", "&#44;")
        message = "[CQ:image,file=" + encoded + "]"
    client = V2Client(media.identity)
    client._state.http, client._state.sender = media.http, core
    client = client.bind(chat.route, source=chat.source)
    try:
        if form == "component":
            await client.qq.send("group", chat.route.target, message)
        else:
            await client.api.call_action("send_group_msg", group_id=chat.route.target, message=message)
        assert media.calls[0][1]["url"] == url and media.calls[0][0] == "/v2/groups/group-one/files"
        assert not media.transfers and not media.pool.blobs
    finally:
        await core.close()
