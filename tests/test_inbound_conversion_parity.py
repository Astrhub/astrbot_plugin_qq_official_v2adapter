"""Inbound face, media and group metadata parity across QQ message scenes."""
import asyncio
import base64
import copy
import html
import json
import subprocess
import urllib.request

import aiohttp
import pytest
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
from test_messaging_state import NOW, chat_payload

from v2.messaging.convert import convert_chat, decode_faces
from v2.models import InstanceKey
from v2.protocol import RawEnvelope

EVENTS = (
    "GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE",
    "AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE",
)
CHANNEL_EVENTS = {"AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"}


def encoded(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode("utf-8")).decode("ascii")


def face(ext):
    return f'<faceType=4,faceId="",ext="{ext}">'


def text_face(text):
    return face(encoded({"text": text}))


def converted(config, payload, **kwargs):
    original = copy.deepcopy(payload)
    settings = copy.deepcopy(config)
    envelope = RawEnvelope(payload, NOW)
    chat = convert_chat(InstanceKey.from_config(config), envelope, **kwargs)
    assert payload == original and envelope.payload == original
    assert config == settings and envelope.received_at == NOW
    assert chat.message.raw_message == original
    return chat


def quote(payload, child):
    payload["d"]["message_type"] = 103
    payload["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_prior")
    payload["d"]["msg_elements"] = [{"msg_idx": "REFIDX_prior", **child}]
    return payload


def reply_of(chat):
    return next(part for part in chat.message.message if isinstance(part, Reply))


@pytest.mark.parametrize("text", ["知识增加", "知" * 256, " ", "换行分隔符\u2028和\u0085"])
def test_decode_faces_accepts_unicode_and_character_limit(text):
    assert decode_faces(text_face(text)) == f"[表情:{text}]"


def test_decode_faces_preserves_surroundings_and_non_face_tags():
    other = '<qqbot-at-user id="someone"/><unknown ext="bad">'
    text = "前" + text_face("知识增加") + other + face("!") + text_face("再见") + "后"
    assert decode_faces(text) == "前[表情:知识增加]" + other + "[表情][表情:再见]后"


@pytest.mark.parametrize("value", [
    None, [], "知识增加", 7, True, {},
    {"text": ""}, {"text": None}, {"text": 7}, {"text": True},
    {"text": []}, {"text": {}}, {"text": "知" * 257},
])
def test_decode_faces_requires_object_with_nonempty_bounded_string(value):
    assert decode_faces(face(encoded(value))) == "[表情]"


def test_decode_faces_rejects_every_ascii_control_character():
    for code in (*range(32), 127):
        assert decode_faces(text_face("前" + chr(code) + "后")) == "[表情]", code


@pytest.mark.parametrize("ext", [
    "", "not-base64!", "_w==", "/w==",  # URL-safe alphabet and invalid UTF-8.
    base64.b64encode(b"not JSON").decode("ascii"),
    base64.b64encode(b'{"text":"ok"').decode("ascii"),
    encoded({"text": "ok"})[:-1],
    encoded({"text": "ok"}) + "!",
    encoded({"text": "ok"}) + "=",
    encoded({"text": "ok"})[:4] + "\n" + encoded({"text": "ok"})[4:],
])
def test_decode_faces_rejects_invalid_base64_utf8_and_json(ext):
    assert decode_faces("before" + face(ext) + "after") == "before[表情]after"


@pytest.mark.parametrize("raw", [
    b'{"text":"\\ud800"}',
    b"[" * 1500 + b"0" + b"]" * 1500,
])
def test_decode_faces_rejects_invalid_unicode_and_excessive_json_depth(raw):
    assert decode_faces(face(base64.b64encode(raw).decode("ascii"))) == "[表情]"


def test_decode_faces_without_ext_uses_neutral_description():
    assert decode_faces('before<faceType=4,faceId="42">after') == "before[表情]after"


def test_decode_faces_ext_limit_is_inclusive_and_failure_is_local():
    # JSON trailing whitespace keeps the document valid at exactly 4096 Base64 characters.
    raw = json.dumps({"text": "知识增加", "extra": "allowed"}, ensure_ascii=False).encode("utf-8")
    raw += b" " * (3072 - len(raw))
    at_limit = base64.b64encode(raw).decode("ascii")
    over_limit = base64.b64encode(raw + b" ").decode("ascii")
    assert len(at_limit) == 4096 and len(over_limit) == 4100
    assert decode_faces(face(at_limit)) == "[表情:知识增加]"
    assert decode_faces(face(over_limit) + text_face("后续")) == "[表情][表情:后续]"
    assert decode_faces(face("A" * 4097)) == "[表情]"


@pytest.mark.parametrize("event", EVENTS)
def test_body_faces_decode_in_all_six_events_without_mutating_envelope(config, event):
    body = "前&amp;" + text_face("知识增加") + face("!") + "<other>后"
    if event in CHANNEL_EVENTS:
        # Channel/DM first apply their existing HTML unescape, including escaped face tags.
        body = html.escape(body, quote=True)
    payload = chat_payload(event, text=body)
    chat = converted(config, payload)
    expected = "前&amp;[表情:知识增加][表情]<other>后"
    assert [type(part) for part in chat.message.message] == [Plain]
    assert chat.message.message[0].text == chat.message.message_str == expected


@pytest.mark.parametrize("event", ["GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
def test_nested_quote_faces_share_body_decoding_and_keep_chain_boundaries(config, event):
    payload = quote(chat_payload(event, text=text_face("正文")), {
        "content": "旧" + text_face("知识增加"),
        "msg_elements": [{
            "content": face("!") + "&amp;",
            "msg_elements": [{"content": text_face("叶子")}],
        }],
    })
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert [type(part) for part in chat.message.message] == [Reply, Plain]
    assert chat.message.message_str == chat.message.message[1].text == "[表情:正文]"
    assert [type(part) for part in reply.chain] == [Plain, Plain, Plain]
    assert [part.text for part in reply.chain] == [
        "旧[表情:知识增加]", "[表情]&amp;", "[表情:叶子]",
    ]
    assert reply.message_str == "旧[表情:知识增加][表情]&amp;[表情:叶子]"


@pytest.mark.parametrize("event", sorted(CHANNEL_EVENTS))
def test_decoded_faces_cannot_create_mentions_between_real_channel_tags(config, event):
    injection = '<@trusted-user><qqbot-at-user id="trusted-user"/>&lt;@trusted-user&gt;'
    rendered = f"[表情:{injection}]"
    tag = text_face(injection)
    payload = chat_payload(event, text=tag + "<@trusted-user>" + tag
                           + '<qqbot-at-user id="trusted-user"/>' + tag)
    payload["d"]["mentions"] = [{"id": "trusted-user"}]
    chat = converted(config, payload)
    assert [type(part) for part in chat.message.message] == [Plain, At, Plain, At, Plain]
    assert [part.qq for part in chat.message.message if isinstance(part, At)] == ["trusted-user", "trusted-user"]
    assert [part.text for part in chat.message.message if isinstance(part, Plain)] == [rendered] * 3
    assert chat.message.message_str == rendered * 3


def test_decoded_quote_mentions_remain_plain_text(config):
    injection = '<@someone><qqbot-at-user id="someone"/>'
    payload = quote(chat_payload(text="current"), {
        "content": text_face(injection), "msg_elements": [{"content": text_face(injection)}],
    })
    payload["d"]["mentions"] = [{"member_openid": "someone"}]
    chat = converted(config, payload)
    reply = reply_of(chat)
    assert [type(part) for part in reply.chain] == [Plain, Plain]
    assert reply.message_str == f"[表情:{injection}]" * 2
    assert sum(isinstance(part, At) for part in chat.message.message) == 1


@pytest.fixture
def forbid_media_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Inbound attachment classification must not fetch or transcode media.")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)


@pytest.mark.parametrize("event", ["GROUP_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
@pytest.mark.parametrize("context", ["body", "quote"])
def test_attachment_classification_parity_is_lazy_and_mime_based(config, forbid_media_io, event, context):
    # Explicit expectations include fields that must remain exclusive to QQ's voice type.
    cases = [
        ({"content_type": "IMAGE/PNG"}, Image, None, None),
        ({"content_type": "ImAgE/SvG+XmL"}, Image, None, None),
        ({"content_type": "VIDEO/MP4"}, Video, None, None),
        ({"content_type": "AuDiO/WaV"}, Record, None, None),
        ({"content_type": "AUDIO/X-WAV"}, Record, None, None),
        ({"content_type": "audio/mpeg", "voice_wav_url": "https://fixture.invalid/ignored.wav",
          "asr_refer_text": "not QQ voice ASR"}, Record, None, None),
        ({"content_type": "VoIcE", "voice_wav_url": "https://fixture.invalid/preferred.wav",
          "asr_refer_text": "spoken words"}, Record, "https://fixture.invalid/preferred.wav", "spoken words"),
        ({"content_type": "VOICE", "voice_wav_url": "file:///tmp/rejected.wav",
          "asr_refer_text": "fallback speech"}, Record, None, "fallback speech"),
        ({"content_type": "voice", "asr_refer_text": 42}, Record, None, None),
        ({"content_type": "audio/ogg", "filename": "misleading.wav"}, File, None, None),
        ({"content_type": "audio/silk", "filename": "misleading.mp3"}, File, None, None),
        ({"content_type": "audio/unknown"}, File, None, None),
        ({"content_type": "unknown/binary"}, File, None, None),
        ({"content_type": "image/"}, File, None, None),
        ({"content_type": "video/webm"}, File, None, None),
        ({"content_type": "file", "filename": "misleading.png"}, File, None, None),
        ({"filename": "misleading.wav"}, File, None, None),
        ({"content_type": None}, File, None, None),
        ({"content_type": 42}, File, None, None),
    ]
    items = [{"url": f"https://fixture.invalid/{i}/download.wav", **item}
             for i, (item, _, _, _) in enumerate(cases)]
    payload = chat_payload(event, text="current")
    if context == "quote":
        quote(payload, {"content": "quoted", "attachments": items})
    else:
        payload["d"]["attachments"] = items
    chat = converted(config, payload)
    if context == "quote":
        chain = reply_of(chat).chain
        assert [type(part) for part in chat.message.message] == [Reply, Plain]
        assert reply_of(chat).message_str == "quoted"
    else:
        chain = chat.message.message
    assert chat.message.message_str == "current"
    assert chain[0].text == ("quoted" if context == "quote" else "current")
    assert [type(part) for part in chain[1:]] == [component for _, component, _, _ in cases]
    for part, item, (_, component, preferred, caption) in zip(chain[1:], items, cases, strict=True):
        assert part.url == (preferred or item["url"])
        if component is File:
            assert part.file_ == "" and part.name == item.get("filename", "file")
        else:
            assert part.file == part.url
        if component is Record:
            assert part.text == caption
    assert chat.attachments == items


@pytest.mark.parametrize("context", ["body", "quote"])
def test_dangerous_media_urls_remain_metadata_in_their_own_chain(config, forbid_media_io, context):
    items = [
        {"content_type": "audio/unknown", "url": "https://fixture.invalid/safe.wav"},
        {"content_type": "IMAGE/PNG", "url": "file:///etc/passwd"},
        {"content_type": "audio/wav", "url": "base64://abcd"},
        {"content_type": "AUDIO/X-WAV", "url": "https://fixture.invalid:70000/bad.wav"},
        {"content_type": "audio/mpeg", "url": "https://user:pass@fixture.invalid/private"},
        {"content_type": "VIDEO/MP4", "url": "https://[malformed"},
        {"content_type": "VOICE", "url": "https://fixture.invalid/voice\n",
         "voice_wav_url": "file:///tmp/private.wav"},
        {"content_type": "unknown/binary", "url": "javascript:alert(1)"},
    ]
    payload = chat_payload(text="")
    if context == "quote":
        quote(payload, {"attachments": items})
    else:
        payload["d"]["attachments"] = items
    chat = converted(config, payload)
    chain = reply_of(chat).chain if context == "quote" else chat.message.message
    assert [type(part) for part in chain] == [File, Unknown]
    assert chain[0].url == items[0]["url"] and chain[0].file_ == ""
    assert chain[1].text == "[附件元数据；需显式受控读取]"
    assert chat.attachments == items and chat.message.message_str == ""
    if context == "quote":
        assert [type(part) for part in chat.message.message] == [Reply]
        assert reply_of(chat).message_str == ""


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"])
@pytest.mark.parametrize("name", ["测试群", "知" * 256, " "])
def test_group_name_preserves_valid_text_and_string_group_id(config, event, name):
    payload = chat_payload(event, target="000007", text="body")
    payload["d"]["group_name"] = name
    chat = converted(config, payload)
    assert chat.message.group.group_name == name
    assert chat.message.group_id == chat.route.target == "000007"
    assert chat.message.message_str == "body"


@pytest.mark.parametrize("name", [None, "", 7, False, [], {}, "知" * 257])
def test_invalid_optional_group_name_is_ignored_without_rejecting_chat(config, name):
    payload = chat_payload(target="000007")
    payload["d"]["group_name"] = name
    chat = converted(config, payload)
    assert getattr(chat.message.group, "group_name", "") in (None, "")
    assert chat.message.group_id == chat.route.target == "000007"


def test_group_name_rejects_every_ascii_control_character(config):
    for code in (*range(32), 127):
        payload = chat_payload(target="000007")
        payload["d"]["group_name"] = "群" + chr(code) + "名"
        chat = converted(config, payload)
        assert getattr(chat.message.group, "group_name", "") in (None, ""), code
        assert chat.message.group_id == "000007"


@pytest.mark.parametrize("event", ["AT_MESSAGE_CREATE", "MESSAGE_CREATE", "C2C_MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
def test_non_group_scenes_do_not_borrow_group_name(config, event):
    payload = chat_payload(event, target="000007")
    payload["d"]["group_name"] = "unrelated group name"
    chat = converted(config, payload)
    assert getattr(chat.message.group, "group_name", "") in (None, "")
    assert chat.route.scene != "group"
