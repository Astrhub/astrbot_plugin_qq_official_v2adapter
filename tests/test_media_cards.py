"""Owned rich-media cards and explicit stream boundaries."""
import base64

import pytest
from astrbot.core.message.components import File, Image, Json, Plain, Record, Video
from astrbot.core.message.message_event_result import MessageChain
from test_media_upload import PNG
from test_media_upload import media as media
from test_media_upload import sending_core

from v2.errors import V2Error
from v2.media.types import MediaInput
from v2.messaging.cards import media_card
from v2.messaging.streaming import StreamingCore


KEYBOARD = {"content": {"rows": [{"buttons": [{
    "id": "open", "render_data": {"label": "打开", "style": 1},
    "action": {"type": 2, "permission": {"type": 2}, "data": "/open", "enter": False},
}]}]}}


@pytest.mark.parametrize("scene,path", [("group", "/v2/groups/group-one"), ("c2c", "/v2/users/user-one")])
async def test_media_card_uses_owned_upload_receipt_and_single_message(media, scene, path):
    core, chat = sending_core(media, "GROUP_AT_MESSAGE_CREATE" if scene == "group" else "C2C_MESSAGE_CREATE")
    card = media_card(MediaInput("image", "base64://" + base64.b64encode(PNG).decode()), KEYBOARD)
    assert type(card) is Json
    result = await core.send(chat.route, MessageChain([card]), source=chat.source)
    assert result["message_id"] == "actual-media-message"
    assert media.calls[-1] == (path + "/messages", {
        "msg_type": 7, "keyboard": KEYBOARD, "media": {"file_info": "actual-file-receipt"},
        "msg_id": "msg-one", "msg_seq": 1})
    assert media.calls[-2][0] == path + "/files"
    assert [(method, url) for method, url, _ in media.http.session.calls if url.endswith("/messages")] == [
        ("POST", "https://api.bot.qq.com" + path + "/messages")]
    assert media.pool.used == 0


@pytest.mark.parametrize("factory,kind,file_type", [
    (lambda url: Image.fromURL(url), "image", 1),
    (lambda url: Record.fromURL(url), "record", 3),
    (lambda url: Video.fromURL(url), "video", 2),
    (lambda url: File("native.txt", url=url), "file", 4),
])
async def test_native_media_components_use_existing_url_upload_without_local_fetch(media, factory, kind, file_type):
    core, chat = sending_core(media)
    url = "https://assets.test/native-" + kind
    component = media_card(factory(url), KEYBOARD)
    assert type(component) is Json and type(component.data["media"]) is MediaInput
    result = await core.send(chat.route, MessageChain([component]), source=chat.source)
    assert result["message_id"] == "actual-media-message"
    assert media.calls == [
        ("/v2/groups/group-one/files", {"file_type": file_type, "srv_send_msg": False,
                                        "file_name": "native.txt" if kind == "file" else "upload", "url": url}),
        ("/v2/groups/group-one/messages", {"msg_type": 7, "keyboard": KEYBOARD,
                                           "media": {"file_info": "actual-file-receipt"}, "msg_id": "msg-one", "msg_seq": 1}),
    ]
    assert not media.transfers and not media.pool.blobs
    assert [(method, target) for method, target, _ in media.http.session.calls][-2:] == [
        ("POST", "https://api.bot.qq.com/v2/groups/group-one/files"),
        ("POST", "https://api.bot.qq.com/v2/groups/group-one/messages"),
    ]


@pytest.mark.parametrize("factory,kind", [
    (lambda data: Image.fromBase64(data), "image"),
    (lambda data: Record.fromBase64(data), "record"),
    (lambda data: Video.fromBase64(data), "video"),
])
async def test_native_base64_media_card_keeps_owned_chunk_upload(media, factory, kind):
    core, chat = sending_core(media)
    card = media_card(factory(base64.b64encode(PNG).decode()), KEYBOARD)
    result = await core.send(chat.route, MessageChain([card]), source=chat.source)
    assert result["media"]["requested_kind"] == kind
    assert media.calls[0][0] == "/v2/groups/group-one/upload_prepare"
    assert media.calls[-2][0] == "/v2/groups/group-one/files"
    assert media.calls[-1][0] == "/v2/groups/group-one/messages"
    assert b"".join(media.puts) == PNG and media.pool.used == 0


@pytest.mark.parametrize("factory,expected_name", [
    (lambda path: Image.fromFileSystem(path), "upload"),
    (lambda path: File("native.txt", file=path.as_uri()), "native.txt"),
])
async def test_native_file_uri_media_card_reads_process_allowed_file(media, tmp_path, factory, expected_name):
    source = tmp_path / "native-file.txt"
    source.write_bytes(PNG)
    core, chat = sending_core(media)
    card = media_card(factory(source), KEYBOARD)
    assert card.data["media"].value == source.as_uri()
    await core.send(chat.route, MessageChain([card]), source=chat.source)
    assert media.calls[0][1]["file_name"] == expected_name
    assert media.calls[-1][1]["media"] == {"file_info": "actual-file-receipt"}
    assert b"".join(media.puts) == PNG and media.pool.used == 0


def test_media_card_rejects_untyped_input_without_creating_component():
    with pytest.raises(V2Error) as error:
        media_card("https://assets.test/untyped", KEYBOARD)
    assert error.value.code == "invalid_media_input"


async def test_unowned_file_info_is_rejected_before_any_network(media):
    core, chat = sending_core(media)
    body = {"msg_type": 7, "keyboard": KEYBOARD, "media": {"file_info": "other-bot-receipt"}}
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, MessageChain([Json(body)]), source=chat.source)
    assert error.value.code == "unsupported" and media.calls == []


async def test_media_upload_rejection_prevents_card_message(media):
    core, chat = sending_core(media)
    media.modes[:] = ["files_rejected"]
    card = media_card(MediaInput("image", "https://assets.test/image.png"), KEYBOARD)
    with pytest.raises(V2Error) as error:
        await core.send(chat.route, MessageChain([card]), source=chat.source)
    assert error.value.phase == "not_sent"
    assert [path for path, _ in media.calls] == ["/v2/groups/group-one/files"]


@pytest.mark.parametrize("scene,use_fallback", [("c2c", False), ("group", True), ("c2c", True)])
async def test_stream_rejects_json_card_before_wire_in_native_and_aggregate(media, scene, use_fallback):
    core, chat = sending_core(media, "C2C_MESSAGE_CREATE" if scene == "c2c" else "GROUP_AT_MESSAGE_CREATE")
    stream = StreamingCore(core, media.state)

    async def fragments():
        yield MessageChain([Json({"msg_type": 2, "markdown": {"content": "card"}, "keyboard": KEYBOARD})])

    with pytest.raises(V2Error) as error:
        await stream.send(chat.route, fragments(), source=chat.source, use_fallback=use_fallback)
    assert error.value.code == "unsupported"
    assert media.calls == []


async def test_aggregate_does_not_convert_trailing_card_into_text_or_replay_full_stream(media):
    core, chat = sending_core(media)
    stream = StreamingCore(core, media.state)

    async def fragments():
        yield MessageChain([Plain("first text")])
        yield MessageChain([Json({"msg_type": 2, "markdown": {"content": "button"}, "keyboard": KEYBOARD})])

    with pytest.raises(V2Error):
        await stream.send(chat.route, fragments(), source=chat.source, use_fallback=True)
    assert media.calls == []
