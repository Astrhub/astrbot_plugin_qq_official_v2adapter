"""AstrBot event helpers share native media, send and source-bound lifecycle rules."""

import base64
from types import SimpleNamespace

import pytest
from astrbot.core.platform.platform_metadata import PlatformMetadata
from test_media_boundary import PNG
from test_media_upload import media
from test_messaging_state import NOW, chat_payload

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.event import V2MediaReceipt, V2MessageEvent
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore
from v2.protocol import RawEnvelope


@pytest.fixture
async def event_media(media):
    m = media
    state = ClientState(m.identity)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, ws_online=lambda: True, media=m.service)
    state.sender, state.http, state.extension_state = sender, m.http, m.state
    client = V2Client(m.identity, state=state)
    chat = convert_chat(m.identity, RawEnvelope(chat_payload("GROUP_MESSAGE_CREATE", target="group-one", timestamp=NOW), NOW))
    event = V2MessageEvent(chat.message, PlatformMetadata("qq_official_v2", "fixture", m.identity.platform_id), client, chat.route)
    try:
        yield SimpleNamespace(event=event, media=m, state=state, sender=sender)
    finally:
        await sender.close()


async def test_event_media_upload_urls_are_transferred_by_qq_then_sent_once(event_media):
    e = event_media
    result = await e.event.upload_group_and_c2c_media("https://media.test/file.png?secret=not-retained", 1,
        srv_send_msg=True, group_openid="group-one", operation_id="event-image")
    assert isinstance(result, V2MediaReceipt) and result.file_info == "actual-file-receipt"
    assert result.send_result["id"] == "actual-media-message"
    assert [(path, body.get("srv_send_msg") if body else None) for path, body in e.media.calls] == [
        ("/v2/groups/group-one/files", False), ("/v2/groups/group-one/messages", None)]
    assert e.media.calls[-1][1]["media"] == {"file_info": "actual-file-receipt"}
    assert e.media.store.operation(e.media.identity.robot, "event-image:send")["state"] == "sent"
    assert e.media.pool.used == 0
    with pytest.raises(V2Error):
        await e.event.upload_group_and_c2c_media("https://media.test/file.png?secret=not-retained", 1,
            srv_send_msg=True, group_openid="group-one", operation_id="event-image")
    assert len(e.media.calls) == 2


async def test_event_base64_image_and_native_c2c_preserve_explicit_origin(event_media):
    e = event_media
    receipt = await e.event.upload_group_and_c2c_image(base64.b64encode(PNG).decode(), 1,
                                                        openid="user-one", operation_id="base64-event")
    assert receipt.file_uuid == "file-uuid" and e.media.pool.used == 0
    paths = [path for path, _ in e.media.calls]
    assert paths[0] == "/v2/users/user-one/upload_prepare" and paths[-1] == "/v2/users/user-one/files"
    assert paths[1:-1] and all(path == "/v2/users/user-one/upload_part_finish" for path in paths[1:-1])
    result = await e.event.post_c2c_message("user-one", content="exact", msg_seq=2,
                                            operation_id="native-event-c2c")
    assert result["id"] == "actual-media-message" and e.media.calls[-1][1]["msg_seq"] == 2
    with pytest.raises(V2Error) as stream:
        await e.event.post_c2c_message("user-one", stream={"id": "x"})
    assert stream.value.code == "unsupported"
    with pytest.raises(V2Error) as target:
        await e.event.upload_group_and_c2c_media("https://media.test/file.png", 1, openid="u", group_openid="g")
    assert target.value.code == "invalid_media_target"


async def test_event_get_group_uses_verified_current_data_and_denied_event_fallback(event_media):
    e = event_media
    class Management:
        def __init__(self):
            self.denied = False
        async def group_info(self, group):
            if self.denied:
                raise V2Error("qq_forbidden", "fixture", status=403, phase="rejected")
            return {"group_openid": group, "group_name": "Actual Group", "group_member_num": 19}
    manager = Management()
    e.state.management = manager
    actual = await e.event.get_group()
    assert actual.group_id == "group-one" and actual.group_name == "Actual Group" and actual.member_count == 19
    manager.denied = True
    fallback = await e.event.get_group()
    assert fallback.group_id == "group-one" and fallback.group_name is None and fallback.member_count is None
    with pytest.raises(V2Error) as unknown:
        await e.event.get_group("unobserved-other-group")
    assert unknown.value.code == "qq_forbidden"
