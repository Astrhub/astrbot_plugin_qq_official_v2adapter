"""Composed media operations keep bounded, deterministic child IDs before side effects."""

import base64
import hashlib

import pytest
from test_media_boundary import PNG
from test_sdk_event_convenience import event_media as event_media

pytest_plugins = ("test_media_upload",)


def expected_child(parent, suffix):
    original = f"{parent}:{suffix}"
    if len(original) <= 512:
        return original
    return "qq-v2-child-" + hashlib.sha256(b"qq-v2-child\0" + parent.encode() + b"\0" + suffix.encode()).hexdigest()


@pytest.mark.parametrize("width", [7, 507, 508, 512])
async def test_event_upload_and_send_child_is_usable_and_stable_for_full_parent_range(event_media, width):
    e = event_media
    parent = "x" * width
    child = expected_child(parent, "send")
    result = await e.event.upload_group_and_c2c_media("https://media.test/photo.png", 1, srv_send_msg=True,
        group_openid="group-one", operation_id=parent)
    assert result.file_info == "actual-file-receipt" and result.send_result["id"] == "actual-media-message"
    assert 1 <= len(child) <= 512
    assert e.media.store.operation(e.media.identity.robot, child)["state"] == "sent"
    assert [call[1] for call in e.media.http.session.calls if call[0] == "POST" and not call[1].endswith("/app/getAppAccessToken")] == [
        "https://api.bot.qq.com/v2/groups/group-one/files",
        "https://api.bot.qq.com/v2/groups/group-one/messages"]
    assert e.media.calls == [
        ("/v2/groups/group-one/files", {"file_type": 1, "srv_send_msg": False,
                                         "file_name": "upload", "url": "https://media.test/photo.png"}),
        ("/v2/groups/group-one/messages", {"msg_type": 7, "media": {"file_info": "actual-file-receipt"}})]
    before = len(e.media.calls)
    with pytest.raises(RuntimeError) as repeat:
        await e.event.upload_group_and_c2c_media("https://media.test/photo.png", 1, srv_send_msg=True,
            group_openid="group-one", operation_id=parent)
    assert repeat.value.code in {"operation_result_not_retained", "operation_already_attempted"}
    assert len(e.media.calls) == before


@pytest.mark.parametrize("width", [507, 508, 512])
async def test_event_upload_only_and_image_wrapper_preserve_original_parent(event_media, width):
    e = event_media
    parent = "u" * width
    result = await e.event.upload_group_and_c2c_media("https://media.test/photo.png", 1,
        group_openid="group-one", operation_id=parent)
    assert result.file_info == "actual-file-receipt" and result.send_result is None
    assert e.media.calls == [("/v2/groups/group-one/files", {"file_type": 1, "srv_send_msg": False,
        "file_name": "upload", "url": "https://media.test/photo.png"})]
    assert e.media.pool.used == 0
    image_id = "i" * width
    wrapped = await e.event.upload_group_and_c2c_image(base64.b64encode(PNG).decode(), 1,
        openid="user-one", srv_send_msg=True, operation_id=image_id)
    assert wrapped.file_info == "actual-file-receipt" and wrapped.send_result["id"] == "actual-media-message"
    assert e.media.store.operation(e.media.identity.robot, expected_child(image_id, "send"))["state"] == "sent"
    assert e.media.calls[-1][0] == "/v2/users/user-one/messages"
    assert e.media.pool.used == 0


@pytest.mark.parametrize("send", [False, True])
async def test_event_rejects_illegal_parent_before_upload_or_send(event_media, send):
    e = event_media
    with pytest.raises(RuntimeError) as bad:
        await e.event.upload_group_and_c2c_media("https://media.test/photo.png", 1, srv_send_msg=send,
            group_openid="group-one", operation_id="z" * 513)
    assert bad.value.code == "invalid_id" and not e.media.calls and not e.media.puts
    assert e.media.pool.used == 0 and e.media.http.session is None


@pytest.mark.parametrize("width,size,block", [(5, 2, 40), (504, 2, 40), (503, 11, 1), (512, 2, 40)])
async def test_multipart_parent_and_server_indices_keep_child_ids_bounded(event_media, width, size, block):
    e = event_media
    parent = "p" * width
    e.media.modes[:] = [{"block_size": block}]
    source = b"a" * size
    async with await e.event.qq.begin_upload("group", "group-one", source, kind="image", name="image.png",
                                             operation_id=parent) as task:
        assert task.parent_id == parent
        assert len(task.parts) == (size if block == 1 else 1)
        assert task.index_base == 0
        for part in task.parts:
            index = part["index"]
            await task.put_part(index)
            await task.finish_part(index)
        result = await task.complete()
    assert result["file_info"] == "actual-file-receipt" and e.media.pool.used == 0
    children = [expected_child(parent, "prepare"),
                *(expected_child(parent, f"finish:{i}") for i in range(len(e.media.puts))),
                expected_child(parent, "files")]
    assert len(set(children)) == len(children) and all(1 <= len(op) <= 512 for op in children)
    assert all(e.media.state.operation(e.media.identity.robot, op)["state"] == "succeeded" for op in children)
    assert [path for path, _ in e.media.calls] == ["/v2/groups/group-one/upload_prepare",
        *(["/v2/groups/group-one/upload_part_finish"] * len(e.media.puts)), "/v2/groups/group-one/files"]
    assert [body["part_index"] for path, body in e.media.calls if path.endswith("upload_part_finish")] == list(range(len(e.media.puts)))
    assert [call[1] for call in e.media.http.session.calls if call[0] == "POST" and not call[1].endswith("/app/getAppAccessToken")] == [
        "https://api.bot.qq.com/v2/groups/group-one/upload_prepare",
        *(["https://api.bot.qq.com/v2/groups/group-one/upload_part_finish"] * len(e.media.puts)),
        "https://api.bot.qq.com/v2/groups/group-one/files"]
    before = len(e.media.calls)
    with pytest.raises(RuntimeError):
        await e.event.qq.begin_upload("group", "group-one", source, kind="image", name="image.png", operation_id=parent)
    assert len(e.media.calls) == before


async def test_multipart_rejects_illegal_parent_before_prepare(event_media):
    e = event_media
    with pytest.raises(RuntimeError) as bad:
        await e.event.qq.begin_upload("group", "group-one", b"ab", kind="image", name="image.png",
                                            operation_id="m" * 513)
    assert bad.value.code == "invalid_id"
    assert not e.media.calls and not e.media.puts and e.media.pool.used == 0
    assert e.media.http.session is None
