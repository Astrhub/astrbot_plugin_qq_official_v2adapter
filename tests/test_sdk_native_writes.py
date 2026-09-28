"""Native writes use literal QQ routes, one ledger and non-replayable results."""

import json
from types import SimpleNamespace

import pytest

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.outbound import SendingCore
from v2.messaging.store import MessageStore, robot_key
from v2.models import InstanceKey, RobotKey


@pytest.fixture
async def native(tmp_path):
    identity = InstanceKey("platform", RobotKey("app"))
    clock = [10000.0]
    store = MessageStore(tmp_path / "messages.sqlite3", clock=lambda: clock[0])
    extension = ExtensionStore(store)
    calls = []
    failure = []
    class HTTP:
        def __init__(self):
            self.identity = identity
        def check(self):
            return None
        async def request(self, spec, *, before_send=None):
            url = spec.url
            if before_send is not None:
                before_send()
            calls.append((spec.method, url, spec.json_body, spec.multipart))
            if failure:
                raise failure.pop(0)
            if url.endswith("/upload_prepare"):
                data = {"upload_id": "task-001", "block_size": "2", "parts": [
                    {"index": 1, "block_size": "2", "presigned_url": "https://upload.test/part?sig=private"}],
                    "upload_config": {"concurrency": 1}}
            elif url.endswith("/files"):
                data = {"file_info": "sensitive-file-info", "ttl": 300,
                        "raw_url": "https://download.test/private?token=secret"}
                if spec.json_body["srv_send_msg"]:
                    data["id"] = "native-sent"
            elif url.endswith("/dms"):
                data = {"guild_id": "DM-1", "channel_id": "channel", "create_time": "001", "extra": True}
            elif spec.method in ("DELETE", "PATCH") or url.endswith("/upload_part_finish"):
                data = {}
            else:
                data = {"id": "native-sent", "timestamp": "2026-09-28T08:00:00+08:00", "extra": {"field": 0}}
                if url.endswith("/stream_messages"):
                    data["id"] = "stream-001"
            return SimpleNamespace(data=data, status=200, trace_id="fixture-trace")
    http = HTTP()
    sender = SendingCore(identity, http, store, is_online=lambda: True, ws_online=lambda: True)
    policy = {"management_writes": True}
    class Management:
        def check(self, *, write=False):
            http.check()
            if write and not policy["management_writes"]:
                raise V2Error("management_disabled", "fixture", status=403)
    state = ClientState(identity)
    state.http, state.sender, state.extension_state, state.management = http, sender, extension, Management()
    client = V2Client(identity, state=state)
    try:
        yield SimpleNamespace(client=client, calls=calls, failure=failure, store=store, extension=extension,
                              policy=policy, clock=clock, http=http)
    finally:
        await sender.close()
        await extension.close()
        store.close()


async def test_group_native_uses_literal_origin_fields_and_call_view(native):
    m = native
    view = m.client.qq.with_options(operation_id="same", owner=object())
    data = await view.post_group_message("g/001", 2, "", markdown={"content": "**raw**"},
                                         keyboard={"content": {"rows": []}}, msg_id="caller-1")
    assert data["extra"] == {"field": 0}
    assert m.calls == [("POST", "https://api.bot.qq.com/v2/groups/g%2F001/messages",
                       {"msg_type": 2, "content": "", "markdown": {"content": "**raw**"},
                        "keyboard": {"content": {"rows": []}}, "msg_id": "caller-1", "msg_seq": 1}, None)]
    retained = m.store.operation(m.client.identity.robot, "same")
    assert retained["state"] == "sent" and retained["seq"] == 1
    assert "**raw**" not in json.dumps(retained) and "owner" not in json.dumps(retained)
    with pytest.raises(V2Error) as duplicate:
        await view.post_group_message("g/001", 2, "", markdown={"content": "**raw**"},
                                      keyboard={"content": {"rows": []}}, msg_id="caller-1")
    assert duplicate.value.code == "operation_result_not_retained" and len(m.calls) == 1


async def test_source_sequences_keep_explicit_values_and_cross_route_fence(native):
    m = native
    await m.client.qq.post_group_message("g", content="first", msg_id="incoming", msg_seq=8, operation_id="one")
    await m.client.qq.post_group_message("g", content="second", msg_id="incoming", msg_seq=2, operation_id="two")
    await m.client.qq.post_group_message("g", content="third", msg_id="incoming", operation_id="three")
    assert [call[2]["msg_seq"] for call in m.calls] == [8, 2, 9]
    with pytest.raises(V2Error) as conflict:
        await m.client.qq.post_group_message("g", content="fourth", msg_id="incoming", msg_seq=2, operation_id="four")
    assert conflict.value.code == "sequence_conflict" and len(m.calls) == 3
    m.store.db.execute("INSERT INTO sources(robot,scene,target,message_id,started,received,expires) VALUES(?,?,?,?,?,?,?)",
                       (robot_key(RobotKey("other")), "group", "other", "foreign", 10000, 10000, 10300))
    m.store.db.commit()
    with pytest.raises(V2Error) as mismatch:
        await m.client.qq.post_group_message("g", content="never", msg_id="foreign", operation_id="cross")
    assert mismatch.value.code == "identity_mismatch" and len(m.calls) == 3
    m.store.db.execute("INSERT INTO extension_events VALUES(?,?,?,?,?,?,?,?,?)", (
        robot_key(m.client.identity.robot), "interaction:known", "INTERACTION_CREATE", 10000,
        json.dumps({"event_id": "outer-known", "scene": "group", "target": "other-group"}),
        "not_required", "typed_notice", None, 10000))
    m.store.db.commit()
    with pytest.raises(V2Error) as wrong_event:
        await m.client.qq.post_group_message("g", content="never", event_id="outer-known", operation_id="cross-event")
    assert wrong_event.value.code == "identity_mismatch" and len(m.calls) == 3
    result = await m.client.qq.post_group_message("unobserved-target", content="active", operation_id="active")
    assert result["id"] == "native-sent" and "msg_id" not in m.calls[-1][2]

@pytest.mark.parametrize("error,operation,state", [
    (V2Error("qq_rate_limited", "fixture", status=429, phase="rejected", http_status=429), "limited", "rejected"),
    (V2Error("qq_api_error", "fixture", status=502, phase="result_unknown", http_status=502), "server", "unknown"),
])
async def test_native_send_does_not_fallback_or_replay_on_rate_or_server_failure(native, error, operation, state):
    m = native
    m.failure.append(error)
    with pytest.raises(V2Error) as failed:
        await m.client.qq.post_group_message("g", content="passive", msg_id="incoming", operation_id=operation)
    assert failed.value.phase == error.phase
    retained = m.store.operation(m.client.identity.robot, operation)
    assert retained["state"] == state and retained["source"] == "incoming"
    assert retained["error"]["details"]["delivery"]["attempts"][0]["wire_attempts"] == 1
    assert len(m.calls) == 1 and m.calls[0][2]["msg_id"] == "incoming"
    with pytest.raises(V2Error):
        await m.client.qq.post_group_message("g", content="passive", msg_id="incoming", operation_id=operation)
    assert len(m.calls) == 1


async def test_native_send_cancellation_after_preflight_retains_unknown(native):
    import asyncio
    m = native
    calls = []
    async def interrupted(spec, *, before_send=None):
        before_send()
        calls.append((spec.method, spec.url))
        raise asyncio.CancelledError
    m.http.request = interrupted
    with pytest.raises(asyncio.CancelledError):
        await m.client.qq.post_c2c_message("u", content="maybe", operation_id="canceled")
    assert calls == [("POST", "https://api.bot.qq.com/v2/users/u/messages")]
    assert m.store.operation(m.client.identity.robot, "canceled")["state"] == "unknown"
    with pytest.raises(V2Error) as again:
        await m.client.qq.post_c2c_message("u", content="maybe", operation_id="canceled")
    assert again.value.code == "send_result_unknown" and len(calls) == 1


async def test_ambiguous_observed_message_id_does_not_select_a_favorable_scope(native):
    from test_messaging_state import chat_payload

    from v2.messaging.convert import convert_chat
    from v2.protocol import RawEnvelope
    m = native
    for target in ("g", "other-group"):
        payload = chat_payload(message_id="same-id", target=target, timestamp=10000.0)
        m.store.observe(convert_chat(m.client.identity, RawEnvelope(payload, 10000.0)))
    with pytest.raises(V2Error) as ambiguous:
        await m.client.qq.post_group_message("g", content="no", msg_id="same-id", operation_id="ambiguous")
    assert ambiguous.value.code == "identity_mismatch" and not m.calls


async def test_observed_chat_outer_event_id_cannot_cross_group(native):
    from test_messaging_state import chat_payload

    from v2.messaging.convert import convert_chat
    from v2.protocol import RawEnvelope
    m = native
    chat = convert_chat(m.client.identity, RawEnvelope(chat_payload(timestamp=10000.0), 10000.0))
    m.store.observe(chat)
    with pytest.raises(V2Error) as mismatch:
        await m.client.qq.post_group_message("different", content="no", event_id="event-msg-one", operation_id="event-cross")
    assert mismatch.value.code == "identity_mismatch" and not m.calls
    assert m.store.db.execute("SELECT event_id FROM sources WHERE message_id='msg-one'").fetchone()[0] == "event-msg-one"



async def test_native_c2c_stream_continuation_preserves_one_seq_and_never_replays(native):
    m = native
    first = await m.client.qq.post_c2c_stream_message("u", "start", 0, msg_id="incoming", operation_id="s0")
    second = await m.client.qq.post_c2c_stream_message("u", "end", 1, input_state=10,
        msg_id="incoming", stream_msg_id=first["id"], operation_id="s1")
    assert second["id"] == first["id"] == "stream-001"
    assert [call[2]["msg_seq"] for call in m.calls] == [1, 1]
    assert [call[2]["index"] for call in m.calls] == [0, 1]
    with pytest.raises(V2Error) as replay:
        await m.client.qq.post_c2c_stream_message("u", "again", 1, msg_id="incoming",
             stream_msg_id="stream-001", operation_id="s1")
    assert replay.value.code == "operation_conflict" or replay.value.code == "operation_result_not_retained"
    assert len(m.calls) == 2
    with pytest.raises(V2Error) as after_end:
        await m.client.qq.post_c2c_stream_message("u", "again", 2, msg_id="incoming",
             stream_msg_id="stream-001", operation_id="s2")
    assert after_end.value.code == "stream_sequence_conflict" and len(m.calls) == 2


async def test_unknown_fragment_blocks_continuation_and_restart(native):
    m = native
    await m.client.qq.post_c2c_stream_message("u", "start", 0, msg_id="incoming", operation_id="f0")
    m.failure.append(V2Error("network_failure", "fixture", status=503, phase="result_unknown"))
    with pytest.raises(V2Error) as unknown:
        await m.client.qq.post_c2c_stream_message("u", "pending", 1, msg_id="incoming",
             stream_msg_id="stream-001", operation_id="f1")
    assert unknown.value.phase == "result_unknown" and m.store.operation(m.client.identity.robot, "f1")["state"] == "unknown"
    with pytest.raises(V2Error) as next_fragment:
        await m.client.qq.post_c2c_stream_message("u", "not allowed", 2, msg_id="incoming",
             stream_msg_id="stream-001", operation_id="f2")
    assert next_fragment.value.code == "stream_result_unknown"
    with pytest.raises(V2Error) as restart:
        await m.client.qq.post_c2c_stream_message("u", "do not replay", 0, msg_id="incoming", operation_id="f3")
    assert restart.value.code == "stream_already_started" and len(m.calls) == 2

async def test_evicted_first_stream_receipt_keeps_nonreplay_fence(native):
    m = native
    m.store.operation_capacity = 1
    first = await m.client.qq.post_c2c_stream_message("u", "partial", 0, msg_id="incoming", operation_id="stream-root")
    await m.client.qq.post_group_message("other-group", content="unrelated", operation_id="other-send")
    row = m.store.db.execute("SELECT state,result FROM operations WHERE op_id='stream-root'").fetchone()
    assert row["state"] == "history_evicted"
    assert json.loads(row["result"]) == {"native_stream": {"index": 0, "id": first["id"], "finished": False}}
    with pytest.raises(V2Error) as restart:
        await m.client.qq.post_c2c_stream_message("u", "restart", 0, msg_id="incoming", operation_id="stream-restart")
    assert restart.value.code == "stream_already_started"
    with pytest.raises(V2Error) as continuation:
        await m.client.qq.post_c2c_stream_message("u", "unproven", 1, msg_id="incoming",
             stream_msg_id=first["id"], operation_id="stream-next")
    assert continuation.value.code == "stream_sequence_conflict" and len(m.calls) == 2



async def test_c2c_input_notify_and_channel_dm_fields_preserved(native):
    m = native
    await m.client.qq.post_c2c_message("u", 6, input_notify={"input_type": 1, "input_second": 0},
                                        is_wakeup=False, operation_id="typing-native")
    assert m.calls[-1][2] == {"msg_type": 6, "input_notify": {"input_type": 1, "input_second": 0}, "is_wakeup": False}
    await m.client.qq.post_message("c", content="", embed={"title": "Hi"}, image="https://img.test/a.png",
                                   operation_id="channel-native")
    assert m.calls[-1][0:2] == ("POST", "https://api.bot.qq.com/channels/c/messages")
    assert m.calls[-1][2]["content"] == "" and m.calls[-1][2]["embed"] == {"title": "Hi"}
    await m.client.qq.post_dms("dm", markdown={"content": "raw"}, operation_id="dm-native")
    assert m.calls[-1][1] == "https://api.bot.qq.com/dms/dm/messages"
    with pytest.raises(V2Error) as invalid:
        await m.client.qq.post_c2c_message("u", msg_id="m", event_id="e")
    assert invalid.value.code == "invalid_source"


@pytest.mark.parametrize("name,args,url,method,body", [
    ("recall_group_message", ("g", "m"), "https://api.bot.qq.com/v2/groups/g/messages/m", "DELETE", None),
    ("recall_c2c_message", ("u", "m"), "https://api.bot.qq.com/v2/users/u/messages/m", "DELETE", None),
    ("recall_message", ("c", "m"), "https://api.bot.qq.com/channels/c/messages/m?hidetip=false", "DELETE", None),
    ("recall_dms", ("dm", "m"), "https://api.bot.qq.com/dms/dm/messages/m?hidetip=false", "DELETE", None),
    ("patch_guild_message", ("c", "m"), "https://api.bot.qq.com/channels/c/messages/m", "PATCH", {}),
    ("create_dms", ("guild", "user"), "https://api.bot.qq.com/users/@me/dms", "POST",
     {"recipient_id": "user", "source_guild_id": "guild"}),
])
async def test_native_mutations_are_scoped_and_reuse_extension_ledger(native, name, args, url, method, body):
    m = native
    result = await getattr(m.client.api, name)(*args, operation_id="action")
    assert m.calls[-1][:3] == (method, url, body)
    if name == "create_dms":
        assert result["extra"] is True
        assert m.extension.operation(m.client.identity.robot, "action")["result"] == {"guild_id": "DM-1"}
    else:
        assert result == {}
    m.policy["management_writes"] = False
    with pytest.raises(V2Error) as disabled:
        await getattr(m.client.qq, name)(*args, operation_id="action")
    assert disabled.value.code == "management_disabled" and len(m.calls) == 1

@pytest.mark.parametrize("name,args,url,body", [
    ("recall_group_message", ("g", "m"), "https://api.bot.qq.com/v2/groups/g/messages/m", None),
    ("recall_c2c_message", ("u", "m"), "https://api.bot.qq.com/v2/users/u/messages/m", None),
    ("recall_message", ("c", "m"), "https://api.bot.qq.com/channels/c/messages/m?hidetip=false", None),
    ("recall_dms", ("dm", "m"), "https://api.bot.qq.com/dms/dm/messages/m?hidetip=false", None),
    ("patch_guild_message", ("c", "m"), "https://api.bot.qq.com/channels/c/messages/m", {}),
])
async def test_offline_receiver_does_not_block_rest_recall_or_patch(native, name, args, url, body):
    m = native
    m.client._state.sender.is_online = lambda: False
    m.client._state.sender.ws_online = lambda: False
    result = await getattr(m.client.qq, name)(*args, operation_id="offline-rest")
    assert result == {}
    assert m.calls == [("PATCH" if name == "patch_guild_message" else "DELETE", url, body, None)]
    with pytest.raises(V2Error) as sending:
        await m.client.qq.post_group_message("g", content="offline", operation_id="offline-send")
    assert sending.value.code == "transport_not_ready" and len(m.calls) == 1
    with pytest.raises(V2Error) as channel_send:
        await m.client.qq.post_message("c", content="offline", operation_id="offline-channel")
    assert channel_send.value.code == "channel_ws_required" and len(m.calls) == 1
    m.policy["management_writes"] = False
    with pytest.raises(V2Error) as disabled:
        await getattr(m.client.qq, name)(*args, operation_id="disabled")
    assert disabled.value.code == "management_disabled" and len(m.calls) == 1
    m.policy["management_writes"] = True
    m.http.check = lambda: (_ for _ in ()).throw(V2Error("service_stopped", "fixture", status=503))
    with pytest.raises(V2Error) as stopped:
        await getattr(m.client.qq, name)(*args, operation_id="http-closed")
    assert stopped.value.code == "service_stopped" and len(m.calls) == 1



async def test_native_write_alias_conflict_and_unknown_result_never_replayed(native):
    m = native
    await m.client.qq.post_group_message("g", content="hello", operation_id="shared")
    with pytest.raises(V2Error) as conflict:
        await m.client.qq.create_dms("g", "u", operation_id="shared")
    assert conflict.value.code == "operation_conflict" and len(m.calls) == 1
    m.failure.append(V2Error("network_failure", "fixture", status=503, phase="result_unknown"))
    with pytest.raises(V2Error):
        await m.client.qq.create_dms("g", "u", operation_id="unknown-dm")
    assert m.extension.operation(m.client.identity.robot, "unknown-dm")["state"] == "unknown"
    with pytest.raises(V2Error):
        await m.client.qq.create_dms("g", "u", operation_id="unknown-dm")
    assert len(m.calls) == 2

async def test_rejected_native_sequence_is_consumed_but_not_sent_is_reusable(native):
    m = native
    m.failure.append(V2Error("qq_api_error", "fixture", business_code=11253, phase="rejected", http_status=400))
    with pytest.raises(V2Error) as refused:
        await m.client.qq.post_group_message("g", content="attempt", event_id="outer-event",
                                             msg_seq=5, operation_id="rejected-5")
    assert refused.value.phase == "rejected"
    with pytest.raises(V2Error) as conflict:
        await m.client.qq.post_group_message("g", content="repeat", event_id="outer-event",
                                             msg_seq=5, operation_id="retry-5")
    assert conflict.value.code == "sequence_conflict"
    await m.client.qq.post_group_message("g", content="next", event_id="outer-event", operation_id="auto-6")
    assert m.calls[-1][2]["msg_seq"] == 6
    m.failure.append(V2Error("connect_failed", "fixture", phase="not_sent"))
    with pytest.raises(V2Error):
        await m.client.qq.post_c2c_message("u", content="unsent", msg_id="source",
                                            msg_seq=9, operation_id="not-sent-9")
    await m.client.qq.post_c2c_message("u", content="safe retry", msg_id="source",
                                        msg_seq=9, operation_id="new-9")
    assert m.calls[-1][2]["msg_seq"] == 9



@pytest.mark.parametrize("name,target,url", [
    ("post_group_file", "g/1", "https://api.bot.qq.com/v2/groups/g%2F1/files"),
    ("post_c2c_file", "u/1", "https://api.bot.qq.com/v2/users/u%2F1/files"),
])
async def test_native_files_do_not_download_urls_or_persist_sensitive_receipts(native, name, target, url):
    m = native
    uploaded = await getattr(m.client.qq, name)(target, 1, "https://cdn.example.com/image.png",
                                                 file_name="image.png", operation_id="upload-only")
    assert uploaded["file_info"] == "sensitive-file-info"
    assert m.calls[-1][:3] == ("POST", url, {"file_type": 1, "url": "https://cdn.example.com/image.png",
                                                "srv_send_msg": False, "file_name": "image.png"})
    assert len(m.calls) == 1  # URL transfers are performed only by QQ.
    retained = m.extension.operation(m.client.identity.robot, "upload-only")
    assert retained["result"] == {"file_uuid": None, "ttl": 300}
    assert "sensitive-file-info" not in json.dumps(retained) and "download.test" not in json.dumps(retained)
    with pytest.raises(V2Error) as repeated:
        await getattr(m.client.qq, name)(target, 1, "https://cdn.example.com/image.png",
                                             file_name="image.png", operation_id="upload-only")
    assert repeated.value.code == "operation_result_not_retained" and len(m.calls) == 1
    merge = await getattr(m.client.qq, name)(target, 1, "", upload_id="task-001", operation_id="merge-empty-url")
    assert merge["file_info"] == "sensitive-file-info"
    assert m.calls[-1][2] == {"file_type": 1, "url": "", "srv_send_msg": False, "upload_id": "task-001"}


async def test_srv_send_msg_is_a_real_send_not_only_upload(native):
    m = native
    receipt = await m.client.qq.post_group_file("g", 1, "https://cdn.example.com/image.png", True, operation_id="file-send")
    assert receipt["id"] == "native-sent" and receipt["file_info"] == "sensitive-file-info"
    assert m.store.operation(m.client.identity.robot, "file-send")["state"] == "sent"
    assert m.extension.db.execute("SELECT 1 FROM extension_ops WHERE op_id='file-send'").fetchone() is None
    assert "sensitive-file-info" not in json.dumps(m.store.operation(m.client.identity.robot, "file-send"))
    with pytest.raises(V2Error) as same:
        await m.client.qq.post_group_file("g", 1, "https://cdn.example.com/image.png", True, operation_id="file-send")
    assert same.value.code == "operation_result_not_retained" and len(m.calls) == 1

async def test_srv_send_msg_without_confirmed_id_is_unknown_not_upload_success(native):
    m = native
    original = m.http.request
    async def omit_id(spec, *, before_send=None):
        response = await original(spec, before_send=before_send)
        if spec.path.endswith("/files"):
            response.data.pop("id", None)
        return response
    m.http.request = omit_id
    with pytest.raises(V2Error) as missing:
        await m.client.qq.post_c2c_file("u", 1, "https://cdn.test/image.png", True, operation_id="maybe-sent")
    assert missing.value.phase == "result_unknown"
    assert m.store.operation(m.client.identity.robot, "maybe-sent")["state"] == "unknown"
    assert len(m.calls) == 1
    with pytest.raises(V2Error):
        await m.client.qq.post_c2c_file("u", 1, "https://cdn.test/image.png", True, operation_id="maybe-sent")
    assert len(m.calls) == 1


async def test_multipart_prepared_blob_is_closed_if_operation_options_conflict(native):
    m = native
    closed = []
    class Prepared:
        def close(self):
            closed.append(True)
    class Media:
        async def prepare(self, route, media_input):
            assert media_input.value == b"image-bytes"
            return Prepared()
    m.client._state.sender.media = Media()
    with pytest.raises(V2Error) as mismatch:
        await m.client.qq.with_options(operation_id="expected").post_message(
            "c", content="hi", file_image=b"image-bytes", operation_id="different")
    assert mismatch.value.code == "operation_conflict" and closed == [True]
    assert not m.calls



@pytest.mark.parametrize("scene,prefix", [("group", "https://api.bot.qq.com/v2/groups/g/"),
                                           ("c2c", "https://api.bot.qq.com/v2/users/g/")])
async def test_native_prepare_and_finish_preserve_server_index_and_hide_ticket(native, scene, prefix):
    m = native
    prepare = (m.client.qq.post_group_upload_prepare if scene == "group" else m.client.qq.post_c2c_upload_prepare)
    finish = (m.client.qq.post_group_upload_part_finish if scene == "group" else m.client.qq.post_c2c_upload_part_finish)
    args = ("g", 1, "2", "image.png", "a" * 32, "b" * 40, "c" * 32)
    response = await prepare(*args, operation_id="prepare")
    assert response["parts"][0]["index"] == 1
    assert m.calls[0][:3] == ("POST", prefix + "upload_prepare", {
        "file_type": 1, "file_size": "2", "file_name": "image.png",
        "md5": "a" * 32, "sha1": "b" * 40, "md5_10m": "c" * 32})
    retained = m.extension.operation(m.client.identity.robot, "prepare")
    assert "presigned_url" not in json.dumps(retained) and "upload.test" not in json.dumps(retained)
    await finish("g", "task-001", 1, "2", "d" * 32, operation_id="finish")
    assert m.calls[-1][:3] == ("POST", prefix + "upload_part_finish", {
        "upload_id": "task-001", "part_index": 1, "block_size": "2", "md5": "d" * 32})


async def test_owned_upload_handle_put_and_finish_release_blob(native, tmp_path):
    from v2.media.io import BlobPool
    from v2.media.service import MediaService
    m = native
    pool = BlobPool(tmp_path / "media")
    puts = []
    class Transfer:
        max_tasks = 1
        timeout = 30
        async def put(self, url, *, blob, offset, count, request_seconds):
            puts.append((url, blob.read(offset, count)))
        async def close(self):
            pass
    media = MediaService(m.client.identity, m.http, m.extension, pool, transfer=Transfer())
    m.client._state.sender.media = media
    try:
        async with await m.client.qq.begin_upload("group", "g", b"ab", kind="image", name="picture.png",
                                                  operation_id="owned") as task:
            assert task.index_base == 1 and pool.used == 2
            await task.put_part(1)
            await task.finish_part(1)
            result = await task.complete()
        assert result["file_info"] == "sensitive-file-info"
        assert puts == [("https://upload.test/part?sig=private", b"ab")]
        assert pool.used == 0 and not pool.blobs
        assert [call[1] for call in m.calls] == [
            "https://api.bot.qq.com/v2/groups/g/upload_prepare",
            "https://api.bot.qq.com/v2/groups/g/upload_part_finish",
            "https://api.bot.qq.com/v2/groups/g/files"]
        assert "upload.test" not in json.dumps(m.extension.operation(m.client.identity.robot, "owned:prepare"))
    finally:
        await media.close()
        pool.close()


async def test_caller_stream_ownership_and_multipart_body(native, tmp_path):
    import io

    from v2.media.io import BlobPool
    from v2.media.service import MediaService
    m = native
    pool = BlobPool(tmp_path / "stream-media")
    media = MediaService(m.client.identity, m.http, m.extension, pool)
    m.client._state.sender.media = media
    caller = io.BytesIO(b"image-bytes")
    seen = []
    original = m.http.request
    async def record(spec, *, before_send=None):
        seen.append((spec.method, spec.url, spec.multipart["file_image"].blob.read(0, 11)))
        return await original(spec, before_send=before_send)
    m.http.request = record
    try:
        data = await m.client.qq.post_message("channel", file_image=caller, operation_id="caller-stream")
        assert data["id"] == "native-sent"
        assert seen == [("POST", "https://api.bot.qq.com/channels/channel/messages", b"image-bytes")]
        assert not caller.closed and pool.used == 0
    finally:
        caller.close()
        await media.close()
        pool.close()
