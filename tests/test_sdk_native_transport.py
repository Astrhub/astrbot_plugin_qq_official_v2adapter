"""Native write methods reach the exact HTTPS destination via HTTPTransport."""

import json

import pytest

from test_sdk_native_writes import native
from v2.transport.http import HTTPTransport


CASES = [
    ("post_group_message", ("g/1",), {"content": "hi"}, "POST", "https://api.bot.qq.com/v2/groups/g%2F1/messages", {"msg_type": 0, "content": "hi"}),
    ("post_c2c_message", ("u/1",), {"content": "hi"}, "POST", "https://api.bot.qq.com/v2/users/u%2F1/messages", {"msg_type": 0, "content": "hi"}),
    ("post_c2c_stream_message", ("u/1", "hello", 0), {"msg_id": "old"}, "POST", "https://api.bot.qq.com/v2/users/u%2F1/stream_messages", {"input_mode": "append", "input_state": 1, "index": 0, "content_type": "text", "content_raw": "hello", "msg_id": "old", "msg_seq": 1}),
    ("post_message", ("c/1",), {"content": "hi"}, "POST", "https://api.bot.qq.com/channels/c%2F1/messages", {"content": "hi"}),
    ("post_keyboard_message", ("c/1",), {"keyboard": {"id": "template"}}, "POST", "https://api.bot.qq.com/channels/c%2F1/messages", {"keyboard": {"id": "template"}}),
    ("post_dms", ("dm/1",), {"content": "hi"}, "POST", "https://api.bot.qq.com/dms/dm%2F1/messages", {"content": "hi"}),
    ("recall_group_message", ("g/1", "m/1"), {}, "DELETE", "https://api.bot.qq.com/v2/groups/g%2F1/messages/m%2F1", None),
    ("recall_c2c_message", ("u/1", "m/1"), {}, "DELETE", "https://api.bot.qq.com/v2/users/u%2F1/messages/m%2F1", None),
    ("recall_message", ("c/1", "m/1"), {"hidetip": True}, "DELETE", "https://api.bot.qq.com/channels/c%2F1/messages/m%2F1?hidetip=true", None),
    ("recall_dms", ("dm/1", "m/1"), {"hidetip": False}, "DELETE", "https://api.bot.qq.com/dms/dm%2F1/messages/m%2F1?hidetip=false", None),
    ("create_dms", ("g/1", "u/1"), {}, "POST", "https://api.bot.qq.com/users/@me/dms", {"recipient_id": "u/1", "source_guild_id": "g/1"}),
    ("patch_guild_message", ("c/1", "m/1"), {"markdown": {"content": "hi"}}, "PATCH", "https://api.bot.qq.com/channels/c%2F1/messages/m%2F1", {"markdown": {"content": "hi"}}),
    ("post_group_file", ("g/1", 1, "https://cdn.test/image.png"), {}, "POST", "https://api.bot.qq.com/v2/groups/g%2F1/files", {"file_type": 1, "url": "https://cdn.test/image.png", "srv_send_msg": False}),
    ("post_c2c_file", ("u/1", 1, "https://cdn.test/image.png"), {"srv_send_msg": True}, "POST", "https://api.bot.qq.com/v2/users/u%2F1/files", {"file_type": 1, "url": "https://cdn.test/image.png", "srv_send_msg": True}),
    ("post_group_upload_prepare", ("g/1", 1, "2", "pic.png", "a" * 32, "b" * 40, "c" * 32), {}, "POST", "https://api.bot.qq.com/v2/groups/g%2F1/upload_prepare", {"file_type": 1, "file_size": "2", "file_name": "pic.png", "md5": "a" * 32, "sha1": "b" * 40, "md5_10m": "c" * 32}),
    ("post_c2c_upload_part_finish", ("u/1", "task", 0, "2", "d" * 32), {}, "POST", "https://api.bot.qq.com/v2/users/u%2F1/upload_part_finish", {"upload_id": "task", "part_index": 0, "block_size": "2", "md5": "d" * 32}),
]


@pytest.mark.parametrize("name,args,options,method,url,body", CASES)
async def test_native_write_real_transport_keeps_literal_origin(native, name, args, options, method, url, body):
    calls = []
    if name == "create_dms":
        payload = {"guild_id": "dm-1", "extra": True}
    elif name.endswith("file"):
        payload = {"file_info": "temporary-file-info", "ttl": 0}
        if options.get("srv_send_msg"):
            payload["id"] = "sent-file"
    elif name.endswith("upload_prepare"):
        payload = {"upload_id": "task-001", "parts": [{"index": 0, "presigned_url": "https://upload.test/secret"}]}
    elif name.endswith("upload_part_finish"):
        payload = {}
    elif name.startswith("post_"):
        payload = {"id": "stream-001" if name == "post_c2c_stream_message" else "message-001", "extra": False}
    else:
        payload = {}
    class Response:
        status = 200
        headers = {}
        @property
        def content(self):
            class Stream:
                async def iter_chunked(self, size):
                    yield json.dumps(payload).encode()
            return Stream()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    class Session:
        def request(self, verb, destination, **kwargs):
            calls.append((verb, destination, kwargs))
            return Response()
        async def close(self):
            pass
    async def token_provider(*, rejected=None):
        return "token-fixture"
    transport = HTTPTransport(native.client.identity, "", session_factory=Session, token_provider=token_provider)
    native.client._state.http = transport
    native.client._state.sender.http = transport
    try:
        result = await getattr(native.client.qq, name)(*args, **options)
        assert result == payload
        assert len(calls) == 1 and calls[0][0:2] == (method, url)
        assert calls[0][2]["headers"]["Authorization"] == "QQBot token-fixture"
        assert calls[0][2]["allow_redirects"] is False
        if body is not None:
            assert calls[0][2]["json"] == body
        else:
            assert "json" not in calls[0][2]
    finally:
        await transport.close()
