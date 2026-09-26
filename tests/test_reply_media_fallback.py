"""A mode conversion reuses uploaded receipts and rebuilds multipart byte readers."""
import base64

import pytest
from astrbot.core.message.components import Plain, Reply
from test_media_boundary import PNG
from test_media_upload import media as media
from test_messaging_state import chat_payload

from v2.errors import V2Error
from v2.media.types import MediaInput
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore
from v2.protocol import RawEnvelope


def observed_sender(m, event):
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(event), m.clock[0]))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, ws_online=lambda: True, media=m.service)
    return chat, sender


@pytest.mark.parametrize("event,url", [("GROUP_AT_MESSAGE_CREATE", False), ("C2C_MESSAGE_CREATE", False),
    ("AT_MESSAGE_CREATE", False), ("GROUP_AT_MESSAGE_CREATE", True), ("DIRECT_MESSAGE_CREATE", True)])
async def test_media_conversion_reuses_receipt_and_reference_with_fresh_body(media, event, url):
    m = media
    chat, sender = observed_sender(m, event)
    value = "https://fixture.invalid/image.png" if url else "base64://" + base64.b64encode(PNG).decode()
    m.modes.append(40034128)
    try:
        result = await sender.send(chat.route, [Reply(id="msg-one"), Plain("image text"), MediaInput("image", value)], source=chat.source, operation_id="same-media-op")
        sent = [body for path, body in m.calls if path.endswith("/messages")]
        assert len(sent) == 2 and sent[0]["msg_id"] == "msg-one"
        assert all(key not in sent[1] for key in ("msg_id", "event_id", "msg_seq"))
        assert sent[0]["message_reference"] == sent[1]["message_reference"]
        assert sent[0]["content"] == sent[1]["content"] == "image text"
        if event == "AT_MESSAGE_CREATE":
            assert sent[0]["file_image"] == sent[1]["file_image"] == PNG
        elif chat.route.scene in {"group", "c2c"}:
            assert sent[0]["media"] == sent[1]["media"] == {"file_info": "actual-file-receipt"}
            assert len([p for p, _ in m.calls if p.endswith("/files")]) == 1
            if not url:
                assert len([p for p, _ in m.calls if p.endswith("/upload_prepare")]) == 1
                assert b"".join(m.puts) == PNG
        if url:
            assert not m.transfers and not m.puts
        assert result["state"] == "sent" and result["delivery"]["mode"] == "active"
        assert m.store.operation(chat.route.robot, "same-media-op")["source"] == "msg-one"
        assert m.pool.used == 0
    finally:
        await sender.close()


async def test_local_expiry_during_media_preparation_selects_active_without_passive_write(media, monkeypatch):
    m = media
    chat, sender = observed_sender(m, "GROUP_AT_MESSAGE_CREATE")
    original = m.service.prepare
    async def prepare(*args, **kwargs):
        value = await original(*args, **kwargs)
        m.clock[0] += 301
        return value
    monkeypatch.setattr(m.service, "prepare", prepare)
    try:
        result = await sender.send(chat.route, [MediaInput("image", "base64://" + base64.b64encode(PNG).decode())], source=chat.source)
        sent = [body for path, body in m.calls if path.endswith("/messages")]
        assert len(sent) == 1 and "msg_id" not in sent[0]
        assert result["delivery"]["reason"]["code"] == "reply_window_expired" and m.pool.used == 0
    finally:
        await sender.close()


async def test_unknown_upload_with_time_error_code_cannot_trigger_message_fallback(media):
    m = media
    chat, sender = observed_sender(m, "GROUP_AT_MESSAGE_CREATE")
    m.modes.append("files_http_500")
    try:
        with pytest.raises(V2Error) as exc:
            await sender.send(chat.route, [MediaInput("image", "https://fixture.invalid/a.png")], source=chat.source, operation_id="upload-unknown")
        assert exc.value.phase == "not_sent" and exc.value.details["media_phase"] == "result_unknown"
        assert not any(p.endswith("/messages") for p, _ in m.calls)
        assert m.store.operation(chat.route.robot, "upload-unknown")["state"] == "not_sent"
        assert m.store.db.execute("SELECT blocked FROM sources").fetchone()[0] is None
    finally:
        await sender.close()


async def test_expired_receipt_cannot_be_reused_for_the_active_attempt(media, monkeypatch):
    m = media
    chat, sender = observed_sender(m, "GROUP_AT_MESSAGE_CREATE")
    original = m.http.request
    messages = 0
    async def request(spec, **kwargs):
        nonlocal messages
        if spec.path.endswith("/messages"):
            messages += 1
            if messages == 2:
                m.clock[0] += 6
        return await original(spec, **kwargs)
    monkeypatch.setattr(m.http, "request", request)
    m.modes.append(40034128)
    try:
        with pytest.raises(V2Error) as exc:
            await sender.send(chat.route, [MediaInput("image", "https://fixture.invalid/a.png")], source=chat.source, operation_id="receipt-expired")
        assert exc.value.code == "upload_ticket_expired" and exc.value.phase == "not_sent"
        assert len([p for p, _ in m.calls if p.endswith("/files")]) == 1
        assert len([p for p, _ in m.calls if p.endswith("/messages")]) == 1
        assert [a["state"] for a in exc.value.details["delivery"]["attempts"]] == ["rejected", "not_sent"]
        assert m.store.operation(chat.route.robot, "receipt-expired")["state"] == "not_sent"
        assert m.pool.used == 0
    finally:
        await sender.close()
