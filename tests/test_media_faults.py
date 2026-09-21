import asyncio
import base64
from dataclasses import replace

import pytest
from aiohttp import web
from test_media_boundary import PNG
from test_media_upload import AssetSession
from test_media_upload import media as media
from test_messaging_state import NOW, chat_payload
from test_transport_http import upstream

from v2.errors import V2Error
from v2.media.io import BlobPool, UploadTransfer
from v2.media.service import MediaService
from v2.media.types import MediaInput
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore
from v2.models import SessionRoute
from v2.protocol import RawEnvelope


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_upload_never_follows_redirects(tmp_path, status):
    calls = []
    async def handle(request):
        assert request.method == "PUT" and "Authorization" not in request.headers and "Cookie" not in request.headers
        await request.read()
        return web.Response(status=status, headers={"Location": "https://other.test/leak?secret=private"})
    pool = BlobPool(tmp_path / "spool")
    blob = await pool.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
    async with upstream(handle) as base:
        transfer = UploadTransfer(pool, session_factory=lambda: AssetSession(base, calls))
        try:
            with pytest.raises(V2Error) as error:
                await transfer.put("https://cos.test/part?sign=private", blob=blob)
            assert error.value.code == "media_redirect_rejected" and error.value.phase == "result_unknown"
            assert "private" not in str(error.value) and len(calls) == 1
        finally:
            await transfer.close()
            pool.close()


async def test_upload_close_releases_session_but_not_other_owner_bytes(tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    async def handle(request):
        await request.read()
        entered.set()
        await release.wait()
        return web.Response(status=200)
    pool = BlobPool(tmp_path / "spool")
    blob = await pool.load("base64://" + base64.b64encode(PNG).decode(), roots=[], max_bytes=100)
    async with upstream(handle) as base:
        sessions = []
        def factory():
            session = AssetSession(base, [])
            sessions.append(session)
            return session
        transfer = UploadTransfer(pool, session_factory=factory)
        task = asyncio.create_task(transfer.put("https://cos.test/part", blob=blob))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await transfer.close()
            with pytest.raises(asyncio.CancelledError) as error:
                await task
            assert error.value.phase == "result_unknown" and not transfer.tasks
            assert all(s.inner.closed for s in sessions) and not blob.closed
        finally:
            release.set()
            await transfer.close()
            pool.close()


async def test_media_close_only_releases_its_own_prepared_files(media):
    m = media
    route = SessionRoute(m.identity.robot, "group", "target")
    other_identity = replace(m.identity, platform_id="other-instance", generation="other-generation")
    other = MediaService(other_identity, m.http, m.state, m.pool)
    original = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    second = await other.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    try:
        with pytest.raises(V2Error):
            await other.upload(route, original)
        await m.service.close()
        assert original.blob.closed and not second.blob.closed and m.pool.used == len(PNG)
    finally:
        await other.close()
    assert second.blob.closed and m.pool.used == 0


@pytest.mark.parametrize("kind,data,wire_type", [
    ("record", b"\x02#!SILK_V3\x03\x00abc", 3),
    ("video", b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00isommp42", 2),
    ("file", b"arbitrary file bytes", 4),
])
async def test_all_named_media_segments_use_shared_sender(media, kind, data, wire_type):
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), NOW))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, media=m.service)
    try:
        result = await sender.send(chat.route, [{"type": "_qq_file" if kind == "file" else kind, "data": {"file": "base64://" + base64.b64encode(data).decode()}}], source=chat.source, onebot=True)
        assert result["message_id"] == "actual-media-message" and result["media"]["kind"] == kind
        assert m.calls[0][1]["file_type"] == wire_type
        assert m.calls[-1][1]["msg_type"] == 7 and m.calls[-1][1]["msg_seq"] == 1
        assert m.pool.used == 0
    finally:
        await sender.close()


async def test_media_policy_rotation_before_upload_is_zero_wire(media):
    m = media
    route = SessionRoute(m.identity.robot, "group", "target")
    prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    m.service.settings = lambda: {"media_max_bytes": 1}
    try:
        with pytest.raises(V2Error):
            await m.service.upload(route, prepared)
        assert not m.calls and not m.transfers
    finally:
        prepared.close()


async def test_expired_url_receipt_is_not_renewed_by_cached_operation(media):
    m = media
    route = SessionRoute(m.identity.robot, "group", "target")
    prepared = await m.service.prepare(route, MediaInput("image", "https://assets.test/image"))
    try:
        first = await m.service.upload(route, prepared, operation_id="same-operation")
        m.clock[0] += 6
        with pytest.raises(V2Error) as error:
            await m.service.upload(route, prepared, operation_id="same-operation")
        assert error.value.code == "upload_ticket_expired"
        assert len(m.calls) == 1 and first["expires_at"] < m.clock[0]
    finally:
        prepared.close()


async def test_upload_receipt_rechecked_after_message_token_wait(media, monkeypatch):
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), NOW))
    m.store.observe(chat)
    original = m.http.token
    async def token(**kwargs):
        result = await original(**kwargs)
        if m.calls and m.calls[-1][0].endswith("/files"):
            m.clock[0] += 6
        return result
    monkeypatch.setattr(m.http, "token", token)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, media=m.service)
    try:
        with pytest.raises(V2Error) as error:
            await sender.send(chat.route, [MediaInput("image", "base64://" + base64.b64encode(PNG).decode())], source=chat.source)
        assert error.value.code == "upload_ticket_expired" and error.value.phase == "not_sent"
        assert not any(path.endswith("/messages") for path, _ in m.calls)
        assert m.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0 and m.pool.used == 0
    finally:
        await sender.close()


async def test_sender_and_media_shutdown_together_keep_put_unknown_and_close_io(media):
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), NOW))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, media=m.service)
    sessions = []
    factory = m.service.transfer.session_factory
    def tracked():
        session = factory()
        sessions.append(session)
        return session
    m.service.transfer.session_factory = tracked
    m.modes[:] = ["wait_put"]
    task = asyncio.create_task(sender.send(chat.route, [MediaInput("image", "base64://" + base64.b64encode(PNG).decode())], source=chat.source, operation_id="active-media"))
    await asyncio.wait_for(m.entered.wait(), 2)
    await asyncio.wait_for(asyncio.gather(sender.close(), m.service.close()), 2)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert all(s.inner.closed for s in sessions) and m.pool.used == 0
    assert not sender.tasks and not m.service.tasks and not m.service.transfer.tasks
    assert m.store.operation(m.identity.robot, "active-media")["state"] == "not_sent"
    unknown = next(row for row in m.state.recent(m.identity.robot) if row["state"] == "unknown")
    assert unknown["kind"] == "media_put" and unknown["context"]["parent_operation_id"] == "active-media"
    assert not any(path.endswith("/messages") for path, _ in m.calls)


async def test_delete_window_starts_at_message_wire_not_media_preparation(media):
    from v2.extensions.management import Management
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), NOW))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, media=m.service)
    management = Management(m.identity, m.http, m.state, m.store, settings=lambda: {"management_writes": True})
    m.modes[:] = ["slow_upload"]
    try:
        result = await sender.send(chat.route, [MediaInput("image", "base64://" + base64.b64encode(PNG).decode())], source=chat.source)
        assert result["wire_started"] == NOW + 121
        await management.delete_message(result["message_id"])
        assert m.http.session.calls[-1][0:2] == ("DELETE", "https://api.bot.qq.com/v2/groups/group-one/messages/actual-media-message")
        before = len(m.calls)
        m.clock[0] += 121
        with pytest.raises(V2Error) as error:
            await management.delete_message(result["message_id"])
        assert error.value.code == "delete_expired" and len(m.calls) == before
    finally:
        await management.close()
        await sender.close()
