"""QQ structured attachments and quotes reach AstrBot's standard components."""
import base64
from pathlib import Path

import pytest
from aiohttp import web
from astrbot.core.message.components import (
    At,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Unknown,
    Video,
)
from astrbot.core.utils.quoted_message import (
    extract_quoted_message_images,
    extract_quoted_message_text,
)
from test_host_message_compat import make_event
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW, chat_payload
from test_transport_http import upstream

from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.models import InstanceKey
from v2.protocol import RawEnvelope

IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/hXcAAAAASUVORK5CYII="
)
FILE_BYTES = b"local fixture file\n"


def quote(payload, *, ref="REFIDX_prior", index=True, child=None):
    payload["d"]["message_type"] = 103
    payload["d"]["message_scene"]["ext"].append(f"ref_msg_idx={ref}")
    payload["d"]["msg_elements"] = [child if child is not None else {
        "message_type": 103, **({"msg_idx": ref} if index else {}), "content": "prior text",
    }]
    return payload


def converted(config, payload, **kwargs):
    return convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW), **kwargs)


def reply_of(chat):
    return next(part for part in chat.message.message if isinstance(part, Reply))


async def test_captured_group_file_quote_reaches_host_file_get_file(config):
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(body=FILE_BYTES)

    async with upstream(serve) as base:
        payload = quote(chat_payload("GROUP_MESSAGE_CREATE", sender="current", text="please edit"),
                        ref="REFIDX_file", child={"message_type": 103, "msg_idx": "REFIDX_file",
                        "content": "", "attachments": [{"content_type": "file", "filename": "sample.txt",
                        "size": len(FILE_BYTES), "url": base + "/quoted.txt"}]})
        payload["d"]["mentions"] = [{"id": "bot-mention", "bot": True, "is_you": True}]
        chat = converted(config, payload, bot_id="bot-ready")
        event = make_event(chat)
        reply = reply_of(chat)
        assert requests == []
        assert event.get_messages()[0] is reply
        assert reply.id == "REFIDX_file" and reply.sender_id is None and reply.sender_nickname is None
        assert reply.message_str == "" and chat.message.message_str == "please edit"
        assert [type(part) for part in reply.chain] == [File]
        assert not any(isinstance(part, File) for part in event.get_messages())
        assert any(isinstance(part, At) and part.qq == "bot-mention" for part in event.get_messages())
        assert chat.message.sender.user_id == "current" and chat.source.ref_idx == "REFIDX_msg-one"
        assert chat.source.message_id == "msg-one" and chat.message.raw_message == payload
        file = reply.chain[0]
        assert file.name == "sample.txt" and file.url == base + "/quoted.txt" and file.file_ == ""
        assert await file.get_file(allow_return_url=True) == file.url and requests == []
        local = Path(await file.get_file())
        try:
            assert local.read_bytes() == FILE_BYTES and requests == ["/quoted.txt"]
        finally:
            local.unlink()
        event.cleanup_temporary_local_files()


async def test_top_and_quoted_media_are_independent_and_host_consumable(config):
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(body=IMAGE_BYTES)

    async with upstream(serve) as base:
        def media(suffix):
            return [
                {"content_type": "image/png", "url": base + f"/{suffix}/image"},
                {"content_type": "file", "filename": f"{suffix}.txt", "url": base + f"/{suffix}/file"},
                {"content_type": "voice", "url": base + f"/{suffix}/silk", "voice_wav_url": base + f"/{suffix}/wav", "asr_refer_text": "speech"},
                {"content_type": "video/mp4", "url": base + f"/{suffix}/video"},
            ]
        payload = quote(chat_payload(text="new body"), child={"message_type": 103, "msg_idx": "REFIDX_prior",
                        "content": "old body", "attachments": media("old")})
        payload["d"]["attachments"] = media("new")
        chat = converted(config, payload)
        event = make_event(chat)
        reply = reply_of(chat)
        assert requests == []  # Conversion and event construction cannot fetch media.
        assert [type(p) for p in reply.chain] == [Plain, Image, File, Record, Video]
        assert [type(p) for p in event.get_messages()] == [Reply, Plain, Image, File, Record, Video]
        assert reply.message_str == "old body" and chat.message.message_str == "new body"
        assert reply.chain[1].file == reply.chain[1].url == base + "/old/image"
        assert reply.chain[2].name == "old.txt" and reply.chain[2].url == base + "/old/file"
        assert reply.chain[3].file == reply.chain[3].url == base + "/old/wav" and reply.chain[3].text == "speech"
        assert reply.chain[4].file == reply.chain[4].url == base + "/old/video"
        assert event.get_messages()[2].url == base + "/new/image"
        assert len(chat.attachments) == 8 and requests == []
        assert await extract_quoted_message_text(event, reply) == "old body[Image][File:old.txt][Video]"
        assert await extract_quoted_message_images(event, reply) == [base + "/old/image"]
        assert requests == []
        image = reply.chain[1]
        assert base64.b64decode(await image.convert_to_base64()) == IMAGE_BYTES
        assert requests == ["/old/image"]
        event.cleanup_temporary_local_files()


def test_voice_without_wav_uses_official_download_url(config):
    payload = chat_payload(text="")
    payload["d"]["attachments"] = [{"content_type": "voice", "url": "https://fixture.invalid/record.silk",
                                    "voice_wav_url": "file:///tmp/not-a-wav", "asr_refer_text": "spoken words"}]
    chat = converted(config, payload)
    record = chat.message.message[0]
    assert isinstance(record, Record) and record.file == record.url == "https://fixture.invalid/record.silk"
    assert record.text == "spoken words" and chat.message.message_str == ""

@pytest.mark.parametrize("event,field", [("GROUP_AT_MESSAGE_CREATE", "member_openid"), ("C2C_MESSAGE_CREATE", "user_openid")])
def test_indexed_quote_author_is_scene_typed_and_string_preserving(config, event, field):
    payload = quote(chat_payload(event, sender="000002", text="current"))
    payload["d"]["msg_elements"][0].update(author={field: "000001", "id": "unrelated", "username": "quoted"})
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert reply.sender_id == "000001" and reply.sender_nickname == "quoted"
    assert reply.message_str == "prior text" and [p.text for p in reply.chain] == ["prior text"]
    assert chat.message.sender.user_id == "000002" and {o["user_id"] for o in chat.observations} == {"000001", "000002"}
    assert chat.references == [{"ref_idx": "REFIDX_prior", "sender": "000001"}]


def test_nested_match_and_conflicting_quote_authors(config):
    payload = quote(chat_payload(text="current"), child={"msg_elements": [
        {"msg_idx": "REFIDX_prior", "author": {"member_openid": "first"}, "content": "one",
         "msg_elements": [{"content": " two"}]},
        {"msg_idx": "REFIDX_prior", "author": {"member_openid": "second"}, "content": " three"},
    ]})
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert [part.text for part in reply.chain] == ["one", " two", " three"]
    assert reply.message_str == "one two three" and reply.sender_id is None
    assert {o["user_id"] for o in chat.observations} == {"user-one"}
    assert len(chat.references) == 1 and chat.references[0]["ref_idx"] == "REFIDX_prior"

def test_official_unindexed_group_quote_and_nested_content(config):
    payload = quote(chat_payload("GROUP_MESSAGE_CREATE", text=" current"), index=False, child={
        "content": "first", "msg_elements": [{"content": " second", "attachments": [
            {"content_type": "image/png", "url": "https://fixture.invalid/quoted.png"}]}],
    })
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert [type(part) for part in reply.chain] == [Plain, Plain, Image]
    assert reply.message_str == "first second" and chat.message.message_str == " current"
    assert not any(isinstance(part, Image) for part in chat.message.message)
    assert reply.sender_id is None and {o["user_id"] for o in chat.observations} == {"user-one"}


@pytest.mark.parametrize("excluded_type", [101, 102])
def test_quote_skips_parallel_or_forwarded_child_content_and_media(config, excluded_type):
    payload = quote(chat_payload("GROUP_MESSAGE_CREATE", text="current body"), child={
        "message_type": 103, "msg_idx": "REFIDX_prior", "content": "real quote",
        "author": {"member_openid": "000quoted"},
        "msg_elements": [
            {"message_type": excluded_type, "content": "FORWARDED",
             "author": {"member_openid": "history-author"},
             "attachments": [
                 {"content_type": "image/png", "url": "https://fixture.invalid/history.png"},
                 {"content_type": "file", "url": "https://fixture.invalid/history.txt"},
             ],
             "msg_elements": [{"content": "nested history", "attachments": [
                 {"content_type": "image/png", "url": "https://fixture.invalid/nested.png"}]}]},
            {"content": " ordinary", "attachments": [{
                "content_type": "file", "filename": "normal.txt", "url": "https://fixture.invalid/normal.txt"}]},
        ],
    })
    payload["d"]["attachments"] = [{"content_type": "image/png", "url": "https://fixture.invalid/current.png"}]
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert [type(part) for part in reply.chain] == [Plain, Plain, File]
    assert reply.message_str == "real quote ordinary" and reply.sender_id == "000quoted"
    assert reply.chain[-1].url == "https://fixture.invalid/normal.txt"
    assert [type(part) for part in chat.message.message] == [Reply, Plain, Image]
    assert chat.message.message_str == "current body"
    assert {o["user_id"] for o in chat.observations} == {"user-one", "000quoted"}
    assert len(chat.attachments) == 5 and chat.message.raw_message == payload
    assert chat.source.message_id == "msg-one" and chat.source.ref_idx == "REFIDX_msg-one"
    assert chat.references == [{"ref_idx": "REFIDX_prior", "sender": "000quoted"}]


@pytest.mark.parametrize("excluded_type", [101, 102])
@pytest.mark.parametrize("shape", ["ancestor", "matched_node"])
def test_quote_match_does_not_enter_parallel_or_forwarded_tree(config, excluded_type, shape):
    historical = {"message_type": excluded_type, "content": "FORWARDED",
                  "author": {"member_openid": "history-author"},
                  "attachments": [{"content_type": "image/png", "url": "https://fixture.invalid/history.png"},
                                  {"content_type": "file", "url": "https://fixture.invalid/history.txt"}]}
    if shape == "ancestor":
        historical["msg_elements"] = [{"msg_idx": "REFIDX_prior", "content": "nested history",
                                      "author": {"member_openid": "nested-history-author"}}]
    else:
        historical["msg_idx"] = "REFIDX_prior"
    payload = quote(chat_payload(text="current body"), child=historical)
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert reply.chain == [] and reply.message_str == "" and reply.sender_id is None
    assert chat.references == [] and {o["user_id"] for o in chat.observations} == {"user-one"}
    assert [type(part) for part in chat.message.message] == [Reply, Plain]
    assert len(chat.attachments) == 2 and chat.message.raw_message == payload
    assert chat.source.message_id == "msg-one" and chat.source.ref_idx == "REFIDX_msg-one"


def test_c2c_quote_without_author_and_bot_mention_stays_unknown(config):
    payload = quote(chat_payload("C2C_MESSAGE_CREATE", text="thanks"))
    payload["d"]["author"]["id"] = "current-only"
    payload["d"]["msg_elements"][0]["author"] = {"id": "untyped-quoted", "username": "unverified"}
    payload["d"]["mentions"] = [{"user_openid": "bot-mentioned", "bot": True, "is_you": True}]
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert reply.sender_id is None and reply.sender_nickname is None
    assert reply.message_str == "prior text" and {o["user_id"] for o in chat.observations} == {"user-one", "bot-mentioned"}


def test_mismatched_index_or_forwarded_author_cannot_supply_quote(config):
    payload = quote(chat_payload(text="own content"), child={"msg_idx": "REFIDX_other", "message_type": 103,
                        "author": {"member_openid": "wrong-quoted"}, "content": "not prior"})
    payload["d"]["msg_elements"].append({"message_type": 102, "author": {"member_openid": "forwarded"},
                                         "content": "forwarded body"})
    chat = converted(config, payload)
    assert reply_of(chat).chain == [] and reply_of(chat).sender_id is None
    assert chat.references == [] and chat.message.message_str == "own content"
    assert {o["user_id"] for o in chat.observations} == {"user-one"}
    payload["d"]["message_scene"]["ext"].pop()  # Without ref binding, nested profiles remain untrusted.
    chat = converted(config, payload)
    assert not any(isinstance(p, Reply) for p in chat.message.message)
    assert {o["user_id"] for o in chat.observations} == {"user-one"}


def test_channel_reference_is_id_only_and_media_from_root_only(config):
    for event in ("AT_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"):
        payload = chat_payload(event, text="channel body")
        payload["d"]["message_reference"] = {"message_id": "000011"}
        payload["d"]["attachments"] = [{"content_type": "image/png", "url": "https://fixture.invalid/root.png"}]
        payload["d"]["msg_elements"] = [{"msg_idx": "000011", "author": {"id": "nested"},
                                        "content": "not a channel quote", "attachments": [
                                        {"content_type": "file", "url": "https://fixture.invalid/nested"}]}]
        chat = converted(config, payload)
        reply = reply_of(chat)
        assert reply.id == "000011" and reply.chain == [] and reply.sender_id is None
        assert [type(part) for part in chat.message.message] == [Reply, Plain, Image]
        assert {o["user_id"] for o in chat.observations} == {"user-one"}
        assert chat.references == [{"message_id": "000011"}]


@pytest.mark.parametrize("event", ["AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
async def test_channel_url_only_attachment_is_lazy_standard_file(config, event):
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(body=FILE_BYTES)

    async with upstream(serve) as base:
        payload = chat_payload(event, text="attachment")
        payload["d"].pop("message_type")
        payload["d"]["attachments"] = [{"url": base + "/download"}]
        chat = converted(config, payload)
        event_obj = make_event(chat)
        assert requests == []
        assert chat.message.message_str == "attachment" and chat.message.raw_message == payload
        assert [type(part) for part in event_obj.get_messages()] == [Plain, File]
        assert chat.attachments == payload["d"]["attachments"]
        file = event_obj.get_messages()[1]
        assert file.name == "file" and file.url == base + "/download" and file.file_ == ""
        assert await file.get_file(allow_return_url=True) == file.url and requests == []
        local = Path(await file.get_file())
        try:
            assert local.read_bytes() == FILE_BYTES and requests == ["/download"]
        finally:
            local.unlink()
        event_obj.cleanup_temporary_local_files()


@pytest.mark.parametrize("context", ["current", "quoted"])
@pytest.mark.parametrize("kind", [None, "unknown/binary"])
def test_untyped_or_unknown_attachment_uses_generic_file_in_its_own_chain(config, context, kind):
    item = {"url": "https://fixture.invalid/opaque", "filename": "payload.dat"}
    if kind is not None:
        item["content_type"] = kind
    payload = chat_payload(text="current body")
    if context == "quoted":
        quote(payload, child={"msg_idx": "REFIDX_prior", "attachments": [item]})
        chat = converted(config, payload)
        chain = reply_of(chat).chain
        assert not any(isinstance(part, File) for part in chat.message.message)
    else:
        payload["d"]["attachments"] = [item]
        chain = converted(config, payload).message.message
    files = [part for part in chain if isinstance(part, File)]
    assert len(files) == 1 and files[0].url == item["url"] and files[0].name == "payload.dat"
    assert files[0].file_ == ""


def test_generic_file_uses_neutral_name_for_untrusted_filename(config):
    payload = chat_payload("MESSAGE_CREATE", text="")
    payload["d"]["attachments"] = [{"url": "https://fixture.invalid/opaque", "filename": "../local.txt"}]
    chat = converted(config, payload)
    assert [type(part) for part in chat.message.message] == [File]
    assert chat.message.message[0].name == "file" and chat.message.message[0].file_ == ""


@pytest.mark.parametrize("context", ["current", "quoted"])
@pytest.mark.parametrize("case", ["missing_url", "rejected_url", "all_valid", "all_invalid"])
async def test_unconvertible_attachments_keep_hint_in_their_own_chain(config, context, case):
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(body=IMAGE_BYTES)

    async with upstream(serve) as base:
        image = {"content_type": "image/png", "url": base + "/image"}
        file = {"content_type": "file", "filename": "safe.txt", "url": base + "/file"}
        without_url = {"content_type": "file", "filename": "missing.txt"}
        rejected_url = {"content_type": "file", "url": "file:///etc/passwd"}
        items = {
            "missing_url": [image, without_url],
            "rejected_url": [image, rejected_url],
            "all_valid": [image, file],
            "all_invalid": [without_url, rejected_url],
        }[case]
        payload = chat_payload(text="current body")
        if context == "quoted":
            quote(payload, child={"msg_idx": "REFIDX_prior", "content": "quoted body", "attachments": items})
        else:
            payload["d"]["attachments"] = items
        chat = converted(config, payload)
        event = make_event(chat)
        if context == "quoted":
            chain = reply_of(chat).chain
            assert [type(part) for part in event.get_messages()] == [Reply, Plain]
            assert chat.message.message_str == "current body"
        else:
            chain = event.get_messages()
            assert chat.message.message_str == "current body"
        expected = {
            "missing_url": [Plain, Image, Unknown],
            "rejected_url": [Plain, Image, Unknown],
            "all_valid": [Plain, Image, File],
            "all_invalid": [Plain, Unknown],
        }[case]
        assert [type(part) for part in chain] == expected
        assert chain[0].text == ("quoted body" if context == "quoted" else "current body")
        assert [part.text for part in chain if isinstance(part, Unknown)] == (
            [] if case == "all_valid" else ["[附件元数据；需显式受控读取]"]
        )
        assert [part.url for part in chain if isinstance(part, Image)] == ([] if case == "all_invalid" else [base + "/image"])
        assert [part.url for part in chain if isinstance(part, File)] == ([base + "/file"] if case == "all_valid" else [])
        assert chat.attachments == items and chat.message.raw_message == payload and requests == []
        event.cleanup_temporary_local_files()


def test_known_quote_in_other_scope_never_fills_missing_author(config, tmp_path):
    from v2.messaging.store import MessageStore

    source = quote(chat_payload("GROUP_MESSAGE_CREATE", target="group-one"))
    source["d"]["msg_elements"][0]["author"] = {"member_openid": "known-quoted"}
    store = MessageStore(tmp_path / "messages.sqlite3", clock=lambda: NOW)
    try:
        store.observe(converted(config, source))
        for changes, event, target in [
            ({}, "GROUP_MESSAGE_CREATE", "group-one"),
            ({"appid": "other-app"}, "GROUP_MESSAGE_CREATE", "group-one"),
            ({"environment": "sandbox"}, "GROUP_MESSAGE_CREATE", "group-one"),
            ({"id": "another-platform"}, "GROUP_MESSAGE_CREATE", "group-one"),
            ({}, "GROUP_MESSAGE_CREATE", "group-two"),
            ({}, "C2C_MESSAGE_CREATE", "group-one"),
        ]:
            payload = quote(chat_payload(event, target=target))
            payload["d"]["mentions"] = [{"member_openid": "known-quoted", "bot": True, "is_you": True}] if event == "GROUP_MESSAGE_CREATE" else []
            chat = converted({**config, **changes}, payload)
            assert reply_of(chat).sender_id is None and reply_of(chat).sender_nickname is None
            assert chat.source.ref_idx == "REFIDX_msg-one" and chat.route.target == (target if event == "GROUP_MESSAGE_CREATE" else "user-one")
    finally:
        store.close()

@pytest.mark.parametrize("kind", ["file", None, "unknown/binary"])
@pytest.mark.parametrize("url", ["file:///etc/passwd", "/tmp/private", "base64://abcd", "ftp://fixture.invalid/x",
                                     "https://user:pass@fixture.invalid/x", "https://fixture.invalid/x\n", "https://[malformed"])
def test_untrusted_attachment_urls_remain_metadata_not_local_files(config, kind, url):
    payload = chat_payload(text="normal https://fixture.invalid/chat.png")
    item = {"filename": "private.txt", "url": url}
    if kind is not None:
        item["content_type"] = kind
    payload["d"]["attachments"] = [item]
    chat = converted(config, payload)
    assert not any(isinstance(part, (File, Image, Record, Video)) for part in chat.message.message)
    assert chat.attachments == payload["d"]["attachments"]
    assert any(isinstance(part, Unknown) for part in chat.message.message)
    assert chat.message.message_str == "normal https://fixture.invalid/chat.png"


@pytest.mark.parametrize("context", ["current", "quoted"])
@pytest.mark.parametrize("kind", ["file", "image/png", "video/mp4", "voice"])
@pytest.mark.parametrize("port", ["bad", "-1", "70000"])
async def test_bad_attachment_port_stays_hint_beside_good_media(config, context, kind, port):
    requests = []

    async def serve(request):
        requests.append(request.path)
        return web.Response(body=IMAGE_BYTES)

    async with upstream(serve) as base:
        items = [{"content_type": "image/png", "url": base + "/good.png"},
                 {"content_type": kind, "url": f"https://fixture.invalid:{port}/bad"}]
        if kind == "voice":
            items[1]["voice_wav_url"] = f"https://fixture.invalid:{port}/bad.wav"
        payload = chat_payload(text="current body")
        if context == "quoted":
            quote(payload, child={"msg_idx": "REFIDX_prior", "content": "quoted body", "attachments": items})
        else:
            payload["d"]["attachments"] = items
        chat = converted(config, payload)
        event = make_event(chat)
        if context == "quoted":
            chain = reply_of(chat).chain
            assert [type(part) for part in event.get_messages()] == [Reply, Plain]
        else:
            chain = event.get_messages()
        assert [type(part) for part in chain] == [Plain, Image, Unknown]
        assert chain[1].url == base + "/good.png"
        assert chain[2].text == "[附件元数据；需显式受控读取]"
        assert chat.attachments == items and chat.message.raw_message == payload
        assert chat.source.message_id == "msg-one" and chat.source.ref_idx == "REFIDX_msg-one"
        assert requests == []
        event.cleanup_temporary_local_files()


@pytest.mark.parametrize("url,kind,component", [
    ("https://fixture.invalid/download", "file", File),
    ("https://fixture.invalid:443/image", "image/png", Image),
    ("https://[2001:db8::1]:8443/voice", "voice", Record),
])
def test_attachment_accepts_normal_explicit_and_ipv6_ports(config, url, kind, component):
    payload = chat_payload(text="")
    payload["d"]["attachments"] = [{"content_type": kind, "url": url}]
    chat = converted(config, payload)
    assert [type(part) for part in chat.message.message] == [component]
    assert chat.message.message[0].url == url and chat.attachments == payload["d"]["attachments"]


@pytest.mark.parametrize("context", ["current", "quoted"])
@pytest.mark.parametrize("port", ["bad", "-1", "70000"])
@pytest.mark.parametrize("original_valid", [False, True])
def test_voice_with_bad_wav_port_only_uses_valid_original(config, context, port, original_valid):
    original = "https://fixture.invalid/voice.silk" if original_valid else "https://fixture.invalid:70000/voice.silk"
    item = {"content_type": "voice", "url": original, "voice_wav_url": f"https://fixture.invalid:{port}/voice.wav"}
    payload = chat_payload(text="current body")
    if context == "quoted":
        quote(payload, child={"msg_idx": "REFIDX_prior", "attachments": [item]})
    else:
        payload["d"]["attachments"] = [item]
    chat = converted(config, payload)
    chain = reply_of(chat).chain if context == "quoted" else chat.message.message
    expected = ([Plain] if context == "current" else []) + ([Record] if original_valid else [Unknown])
    assert [type(part) for part in chain] == expected
    if original_valid:
        assert chain[-1].file == chain[-1].url == original
    else:
        assert chain[-1].text == "[附件元数据；需显式受控读取]"
    if context == "quoted":
        assert [type(part) for part in chat.message.message] == [Reply, Plain]
    assert chat.attachments == [item] and chat.message.raw_message == payload


@pytest.mark.parametrize("case", ["too_many", "negative_size", "invalid_nested", "oversized", "bad_idx"])
def test_attachment_and_nested_structure_bounds_remain_enforced(config, case):
    payload = quote(chat_payload(), child={"msg_idx": "REFIDX_prior", "content": "quoted"})
    if case == "too_many":
        payload["d"]["msg_elements"][0]["attachments"] = [{"content_type": "file"}] * 33
    elif case == "negative_size":
        payload["d"]["msg_elements"][0]["attachments"] = [{"size": -1}]
    elif case == "invalid_nested":
        payload["d"]["msg_elements"][0]["msg_elements"] = ["invalid"]
    elif case == "oversized":
        payload["d"]["msg_elements"][0]["content"] = "x" * (65 * 1024)
    else:
        payload["d"]["msg_elements"][0]["msg_idx"] = 7
    with pytest.raises(V2Error):
        converted(config, payload)


async def test_real_delivery_queue_keeps_reply_media_and_isolation(receiver):
    owner, instance = receiver
    payload = quote(chat_payload("GROUP_MESSAGE_CREATE", text="use attachment"), child={
        "msg_idx": "REFIDX_prior", "message_type": 103, "attachments": [{"content_type": "image/jpeg",
        "url": "https://fixture.invalid/reply.jpg"}]})
    assert owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    assert instance.consumer.step()
    event = instance._event_queue.get_nowait()
    assert [type(part) for part in event.get_messages()] == [Reply, Plain]
    assert isinstance(event.get_messages()[0].chain[0], Image)
    assert event.get_messages()[0].chain[0].url == "https://fixture.invalid/reply.jpg"
    assert event.bot._source.message_id == "msg-one" and event.bot._source.ref_idx == "REFIDX_msg-one"
    assert owner.messages.delivered(convert_chat(instance.identity, RawEnvelope(payload, NOW)))
    event.cleanup_temporary_local_files()
    assert not owner.delivery_slots.events
