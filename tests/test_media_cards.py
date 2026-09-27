"""Owned rich-media cards and explicit stream boundaries."""
import base64

import pytest
from astrbot.core.message.components import Json, Plain
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
