import asyncio
import base64
import hashlib
from types import SimpleNamespace
from urllib.parse import urlsplit

import aiohttp
import pytest
from aiohttp import web
from test_media_boundary import PNG
from test_messaging_state import NOW
from test_transport_http import MappedSession, upstream

from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.media.io import BlobPool, UploadTransfer
from v2.media.service import MediaService
from v2.media.types import MediaInput
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, SessionRoute
from v2.transport.http import HTTPTransport


class AssetSession:
    def __init__(self, base, calls):
        self.base, self.calls = base, calls
        self.inner = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.inner.close()

    def request(self, method, url, **kwargs):
        p = urlsplit(url)
        assert method == "PUT" and p.scheme == "https" and p.hostname == "cos.test" and p.port in (None, 443)
        assert not any(k.lower() in {"authorization", "cookie", "x-union-appid"} for k in kwargs["headers"])
        assert kwargs["allow_redirects"] is False
        self.calls.append((method, url))
        return self.inner.request(method, self.base + p.path + ("?" + p.query if p.query else ""), **kwargs)


@pytest.fixture
async def media(config, tmp_path):
    identity = InstanceKey.from_config(config)
    clock = [NOW]
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    state, pool = ExtensionStore(store), BlobPool(tmp_path / "spool")
    calls, transfers, puts, modes = [], [], [], []
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.path.startswith("/part/"):
            assert "Authorization" not in request.headers
            puts.append(await request.read())
            if modes and modes[0] == "wait_put":
                entered.set()
                await release.wait()
            return web.Response(status=200)
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "media-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot media-fixture"
        if request.content_type == "multipart/form-data":
            fields = {}
            reader = await request.multipart()
            while part := await reader.next():
                fields[part.name] = bytes(await part.read()) if part.filename else await part.text()
            calls.append((request.path, fields))
            if modes and modes[0] == "send401":
                modes.pop(0)
                return web.json_response({"code": 11244}, status=401)
            if modes and type(modes[0]) is int:
                return web.json_response({"code": modes.pop(0)}, status=400)
            return web.json_response({"id": "actual-channel-message"})
        if request.method == "DELETE":
            calls.append((request.path, None))
            return web.Response(status=204)
        body = await request.json()
        calls.append((request.path, body))
        if request.path.endswith("/messages"):
            if modes and type(modes[0]) is int:
                return web.json_response({"code": modes.pop(0)}, status=400)
            return web.json_response({"id": "actual-media-message"})
        if modes and modes[0] == "prepare_retry" and request.path.endswith("upload_prepare"):
            modes.pop(0)
            return web.json_response({"code": 40093001})
        if modes and modes[0] == "daily" and request.path.endswith("upload_part_finish"):
            return web.json_response({"code": 40093002}, headers={"Retry-After": "60"})
        if request.path.endswith("upload_prepare"):
            size = int(body["file_size"])
            block = modes[0].get("block_size", 40) if modes and isinstance(modes[0], dict) else 40
            parts = [{"index": i, "block_size": str(min(block, size - i * block)), "presigned_url": f"https://cos.test/part/{i}?sign=do-not-store"}
                     for i in range((size + block - 1) // block)]
            if modes and isinstance(modes[0], dict) and "part_indices" in modes[0]:
                for part, index in zip(parts, modes[0]["part_indices"], strict=True):
                    part["index"] = index
            return web.json_response({"upload_id": "fixture-upload", "block_size": str(block), "parts": parts, "upload_config": {"concurrency": 1, "retry_timeout": 30, "retry_delay": 0}})
        if request.path.endswith("upload_part_finish"):
            return web.json_response({})
        if request.path.endswith("files"):
            if modes and modes[0] == "files_http_500":
                return web.json_response({"code": 40034128}, status=500)
            if modes and modes[0] == "files_rejected":
                return web.json_response({"code": 850026, "message": "private-url-must-not-leak"}, status=400, headers={"X-Tps-Trace-Id": "fixture-files", "Retry-After": "3"})
            if modes and modes[0] == "wait_files":
                entered.set()
                await release.wait()
            assert body["srv_send_msg"] is False
            if modes and modes[0] == "slow_upload":
                clock[0] += 121
            if modes and modes[0] == "expired":
                clock[0] += 301
            if modes and modes[0] == "unknown":
                return web.json_response({})
            return web.json_response({"file_info": "actual-file-receipt", "file_uuid": "file-uuid", "ttl": 5,
                                      "raw_url": "https://cos.test/private?sign=must-not-persist"})
        raise AssertionError(request.path)
    async def advance(seconds):
        clock[0] += seconds
    async with upstream(handler) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        transfer = UploadTransfer(pool, session_factory=lambda: AssetSession(base, transfers))
        service = MediaService(identity, http, state, pool, transfer=transfer, settings=lambda: {"media_roots": [str(tmp_path)]}, sleep=advance)
        try:
            yield SimpleNamespace(identity=identity, clock=clock, store=store, state=state, pool=pool, http=http, service=service,
                                  calls=calls, transfers=transfers, puts=puts, modes=modes, entered=entered, release=release)
        finally:
            release.set()
            await service.close()
            await state.close()
            await http.close()
            pool.close()
            store.close()


@pytest.mark.parametrize("scene,prefix", [("group", "/v2/groups/g%20one"), ("c2c", "/v2/users/g%20one")])
async def test_real_chunk_upload_hashes_destinations_and_no_secret_persistence(media, scene, prefix):
    m = media
    route = SessionRoute(m.identity.robot, scene, "g one")
    prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    try:
        result = await m.service.upload(route, prepared, operation_id="explicit-op")
        assert result["file_info"] == "actual-file-receipt" and result["kind"] == "image"
        # Server request.path is decoded; original destination is independently checked below.
        assert [p.rsplit("/", 1)[1] for p, _ in m.calls] == ["upload_prepare", "upload_part_finish", "upload_part_finish", "files"]
        api_calls = [call for call in m.http.session.calls if not call[1].endswith("getAppAccessToken")]
        assert api_calls[0][1] == "https://api.bot.qq.com" + prefix + "/upload_prepare"
        first = m.calls[0][1]
        assert first == {"file_type": 1, "file_size": str(len(PNG)), "file_name": "upload", "md5": hashlib.md5(PNG).hexdigest(), "sha1": hashlib.sha1(PNG).hexdigest(), "md5_10m": hashlib.md5(PNG).hexdigest()}
        assert b"".join(m.puts) == PNG
        assert m.calls[-1][1] == {"file_type": 1, "file_name": "upload", "srv_send_msg": False, "upload_id": "fixture-upload"}
        dumped = "\n".join(m.store.db.iterdump())
        assert "do-not-store" not in dumped and "must-not-persist" not in dumped and "media-fixture" not in dumped
        count = len(m.calls)
        assert await m.service.upload(route, prepared, operation_id="another-explicit") == result
        assert len(m.calls) == count
        m.clock[0] += 6
        await m.service.upload(route, prepared, operation_id="after-ttl")
        assert len(m.calls) == count * 2
    finally:
        prepared.close()
    assert m.pool.used == 0


async def test_url_upload_does_not_download_or_allocate_bytes(media):
    m = media
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("image", "https://assets.test/image"))
    try:
        await m.service.upload(route, prepared)
        assert not m.transfers and not m.pool.blobs and prepared.blob is None
        assert m.calls == [("/v2/groups/g/files", {"file_type": 1, "srv_send_msg": False, "file_name": "upload", "url": "https://assets.test/image"})]
    finally:
        prepared.close()


@pytest.mark.parametrize("mode,expected", [("prepare_retry", None), ("daily", "upload_daily_capacity"), ("unknown", "invalid_upload_response")])
async def test_upload_codes_and_unknown_not_replayed(media, mode, expected):
    m = media
    route = SessionRoute(m.identity.robot, "group", "g")
    prepared = await m.service.prepare(route, MediaInput("image", "base64://" + base64.b64encode(PNG).decode()))
    m.modes[:] = [mode]
    try:
        if expected:
            with pytest.raises(V2Error) as error:
                await m.service.upload(route, prepared, operation_id="bound")
            assert error.value.code == expected
            if mode == "daily":
                assert error.value.business_code == 40093002 and error.value.retry_after == "60"
                assert error.value.phase == "rejected"
            if mode == "unknown":
                assert m.state.operation(route.robot, error.value.operation_id)["state"] == "unknown"
                before = len(m.calls)
                with pytest.raises(V2Error):
                    await m.service.upload(route, prepared, operation_id="bound")
                assert len(m.calls) == before
        else:
            await m.service.upload(route, prepared)
            assert sum(p.endswith("upload_prepare") for p, _ in m.calls) == 2
    finally:
        prepared.close()


def sending_core(m, event="GROUP_AT_MESSAGE_CREATE"):
    from test_messaging_state import chat_payload

    from v2.messaging.convert import convert_chat
    from v2.messaging.outbound import SendingCore
    from v2.protocol import RawEnvelope
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(event), m.clock[0]))
    m.store.observe(chat)
    core = SendingCore(m.identity, m.http, m.store, media=m.service, is_online=lambda: True, ws_online=lambda: True)
    return core, chat


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
async def test_media_send_uses_one_message_reservation_and_actual_id(media, event):
    core, chat = sending_core(media, event)
    result = await core.send(chat.route, [{"type": "image", "data": {"file": "base64://" + base64.b64encode(PNG).decode()}}], source=chat.source, operation_id="send-image")
    body = media.calls[-1][1]
    assert result["message_id"] == "actual-media-message" and result["message_id"] != "file-uuid"
    assert body == {"msg_type": 7, "media": {"file_info": "actual-file-receipt"}, "msg_id": "msg-one", "msg_seq": 1}
    assert media.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
    assert media.pool.used == 0 and not core.tasks


@pytest.mark.parametrize("event,path", [("GROUP_AT_MESSAGE_CREATE", "/v2/groups/group-one/messages"), ("C2C_MESSAGE_CREATE", "/v2/users/user-one/messages")])
async def test_group_c2c_media_keeps_text_caption(media, event, path):
    from astrbot.core.message.components import Image, Plain
    from astrbot.core.message.message_event_result import MessageChain
    core, chat = sending_core(media, event)
    image = Image.fromBase64(base64.b64encode(PNG).decode())
    result = await core.send(chat.route, MessageChain([Plain("caption"), image]), source=chat.source, operation_id="caption-image")
    assert result["message_id"] == "actual-media-message"
    assert media.calls[-1] == (path, {"content": "caption", "msg_type": 7, "media": {"file_info": "actual-file-receipt"}, "msg_id": "msg-one", "msg_seq": 1})


async def test_channel_multipart_auth_retry_rewinds_owned_bytes(media):
    from astrbot.core.message.components import Image, Plain
    from astrbot.core.message.message_event_result import MessageChain
    core, chat = sending_core(media, "AT_MESSAGE_CREATE")
    media.modes[:] = ["send401"]
    image = Image.fromBase64(base64.b64encode(PNG).decode())
    result = await core.send(chat.route, MessageChain([Plain("caption"), image]), source=chat.source)
    assert result["message_id"] == "actual-channel-message"
    assert len(media.calls) == 2 and all(fields["file_image"] == PNG and fields["content"] == "caption" and fields["msg_id"] == "msg-one" for _, fields in media.calls)
    assert not media.puts and media.pool.used == 0


@pytest.mark.parametrize("mode", ["expired", "unknown"])
async def test_upload_finish_rechecks_source_without_replaying_unknown_media(media, mode):
    core, chat = sending_core(media)
    media.modes[:] = [mode]
    value = [MediaInput("image", "base64://" + base64.b64encode(PNG).decode())]
    if mode == "expired":
        result = await core.send(chat.route, value, source=chat.source, operation_id="media-late")
        assert result["state"] == "sent" and result["delivery"]["reason"]["code"] == "reply_window_expired"
        messages = [b for p, b in media.calls if p.endswith("/messages")]
        assert len(messages) == 1 and "msg_id" not in messages[0]
        assert len([p for p, _ in media.calls if p.endswith("/files")]) == 1
        assert b"".join(media.puts) == PNG
    else:
        with pytest.raises(V2Error) as error:
            await core.send(chat.route, value, source=chat.source, operation_id="media-failure")
        assert error.value.code == "invalid_upload_response" and error.value.phase == "not_sent"
        assert not any(p.endswith("/messages") for p, _ in media.calls)
        assert media.store.operation(media.identity.robot, "media-failure")["state"] == "not_sent"
        details = error.value.as_dict()["details"]
        assert details["message_sent"] is False and details["media_phase"] == "result_unknown"
        assert media.state.operation(media.identity.robot, details["media_operation_id"])["state"] == "unknown"
    assert media.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    assert media.pool.used == 0


async def test_entire_media_chain_and_cross_target_preflight(media):
    core, chat = sending_core(media)
    value = MediaInput("image", "https://assets.test/image")
    result = await core.send(chat.route, [value, {"type": "text", "data": {"text": "not silently dropped"}}], source=chat.source)
    assert result["message_id"] == "actual-media-message"
    assert media.calls[-1][1]["content"] == "not silently dropped" and media.calls[-1][1]["media"]["file_info"] == "actual-file-receipt"
    prepared = await media.service.prepare(chat.route, value)
    try:
        other = SessionRoute(media.identity.robot, "group", "different")
        with pytest.raises(V2Error) as error:
            await media.service.upload(other, prepared)
        assert error.value.code == "media_scope_mismatch"
    finally:
        prepared.close()
