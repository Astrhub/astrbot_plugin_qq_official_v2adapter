"""Instance Markdown defaults resolve once at the host send boundary, three-state."""
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot.core.message.components import Image, Json, Plain
from astrbot.core.message.message_event_result import MessageChain
from test_messaging_state import NOW, chat_payload
from test_transport_http import MappedSession, upstream

from v2.client import V2Client
from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore, parse_message
from v2.messaging.store import IdentityView, MessageStore
from v2.messaging.streaming import StreamingCore
from v2.models import InstanceKey
from v2.protocol import RawEnvelope
from v2.transport.http import HTTPTransport


@pytest.fixture
async def md(config, tmp_path, monkeypatch):
    from astrbot.core.utils.metrics import Metric
    monkeypatch.setattr(Metric, "upload", lambda **kwargs: None)
    clock, calls = [NOW], []
    identity = InstanceKey.from_config(config)
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    state = ExtensionStore(store)
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "md-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot md-fixture"
        calls.append((request.path, await request.json()))
        if request.path.endswith("/stream_messages"):
            return web.json_response({"id": "md-stream-id", "remain_msg_len": 0})
        return web.json_response({"id": "md-real-" + str(len(calls))})
    async with upstream(handler) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        text_core = SendingCore(identity, http, store, is_online=lambda: True, ws_online=lambda: True)
        md_core = SendingCore(identity, http, store, is_online=lambda: True, ws_online=lambda: True, markdown_default=True)
        client = V2Client(identity)
        client._state.http, client._state.sender, client._state.cache = http, text_core, IdentityView(store, identity.robot)
        md_stream = StreamingCore(md_core, state)

        def observe(event="GROUP_AT_MESSAGE_CREATE"):
            chat = convert_chat(identity, RawEnvelope(chat_payload(event), clock[0]))
            store.observe(chat)
            return chat
        try:
            yield SimpleNamespace(identity=identity, store=store, client=client, text=text_core, md=md_core,
                                  md_stream=md_stream, state=state, http=http, observe=observe, calls=calls, clock=clock)
        finally:
            await md_stream.close()
            await md_core.close()
            await text_core.close()
            await state.close()
            await http.close()
            store.close()


def message_bodies(md):
    return [body for path, body in md.calls if path.endswith("/messages")]


CHAIN_FLAG_CASES = [
    (None, "default"), (True, "markdown"), (False, "text"),
]


def chain(text, flag):
    result = MessageChain([Plain(text)])
    return result if flag is None else result.use_markdown(flag)


@pytest.mark.parametrize("flag,expected", CHAIN_FLAG_CASES)
async def test_default_off_keeps_text_for_unspecified_chains(md, flag, expected):
    chat = md.observe()
    await md.text.send(chat.route, chain("body", flag), source=chat.source)
    body = message_bodies(md)[0]
    if expected == "markdown":
        assert body == {"markdown": {"content": "body"}, "msg_type": 2, "msg_id": "msg-one", "msg_seq": 1}
    else:
        assert body == {"content": "body", "msg_type": 0, "msg_id": "msg-one", "msg_seq": 1}


@pytest.mark.parametrize("flag,expected", CHAIN_FLAG_CASES)
async def test_default_on_resolves_unspecified_chains_to_markdown(md, flag, expected):
    chat = md.observe()
    await md.md.send(chat.route, chain("body", flag), source=chat.source)
    body = message_bodies(md)[0]
    if expected == "text":
        assert body == {"content": "body", "msg_type": 0, "msg_id": "msg-one", "msg_seq": 1}
    else:
        assert body == {"markdown": {"content": "body"}, "msg_type": 2, "msg_id": "msg-one", "msg_seq": 1}


async def test_default_on_session_and_sdk_strings_keep_exact_types(md):
    chat = md.observe()
    from v2.messaging.session_sources import SessionSendPolicy
    await md.md.send(chat.route, chain("session body", None), session=SessionSendPolicy(chat.source, "session_index"))
    assert message_bodies(md)[0] == {"markdown": {"content": "session body"}, "msg_type": 2}
    assert (await md.client.qq.send("group", "group-one", "sdk string"))["state"] == "sent"
    assert message_bodies(md)[1] == {"content": "sdk string", "msg_type": 0}
    assert (await md.client.call_action("send_group_msg", group_id="group-one", message="onebot text"))["state"] == "sent"
    assert message_bodies(md)[2] == {"content": "onebot text", "msg_type": 0}


async def test_default_on_does_not_convert_cards_or_explicit_mixes(md):
    chat = md.observe()
    card = {"msg_type": 2, "markdown": {"content": "## card"}, "keyboard": {"id": "fixture-template"}}
    await md.md.send(chat.route, MessageChain([Json(card)]), source=chat.source)
    assert message_bodies(md)[0] == {**card, "msg_id": "msg-one", "msg_seq": 1}
    with pytest.raises(V2Error):
        await md.md.send(chat.route, MessageChain([{"type": "markdown", "data": {"content": "md"}}, Plain("text")]), source=chat.source)
    with pytest.raises(V2Error):
        await md.md.send(chat.route, MessageChain([Plain("a"), Image.fromURL("https://fixture.invalid/i.png")]).use_markdown(True), source=chat.source)
    assert len(message_bodies(md)) == 1


async def test_parse_message_resolves_default_only_for_unspecified_chains():
    from astrbot.core.message.components import Plain
    atoms, use_md = parse_message(MessageChain([Plain("x")]), default=True)
    assert use_md is True
    atoms, use_md = parse_message(MessageChain([Plain("x")]), default=False)
    assert use_md is False
    atoms, use_md = parse_message(MessageChain([Plain("x")]).use_markdown(False), default=True)
    assert use_md is False
    atoms, use_md = parse_message("plain string", default=True)
    assert use_md is False
    atoms, use_md = parse_message(MessageChain([Image.fromURL("https://fixture.invalid/i.png")]), default=True)
    assert use_md is False


async def test_aggregate_stream_inherits_default_and_locks_it(md):
    chat = md.observe("GROUP_AT_MESSAGE_CREATE")

    async def fragments():
        yield MessageChain([Plain("hello")])
        yield MessageChain([Plain(" world")])

    await md.md_stream.send(chat.route, fragments(), source=chat.source, use_fallback=True)
    assert message_bodies(md)[0] == {"markdown": {"content": "hello world"}, "msg_type": 2, "msg_id": "msg-one", "msg_seq": 1}

    async def explicit_text():
        yield MessageChain([Plain("plain")]).use_markdown(False)

    await md.md_stream.send(chat.route, explicit_text(), source=chat.source, use_fallback=True, operation_id="stream-text")
    assert message_bodies(md)[1] == {"content": "plain", "msg_type": 0, "msg_id": "msg-one", "msg_seq": 2}


async def test_native_c2c_stream_content_type_follows_default(md):
    chat = md.observe("C2C_MESSAGE_CREATE")

    async def fragments():
        yield MessageChain([Plain("native")])

    await md.md_stream.send(chat.route, fragments(), operation_id="native-md-stream")
    frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
    assert [frame["content_type"] for frame in frames] == ["markdown", "markdown"]
    assert frames[0]["content_raw"] == "native"

    async def explicit_text():
        yield MessageChain([Plain("t")]).use_markdown(False)

    with pytest.raises(V2Error):
        await md.md_stream.send(chat.route, explicit_text(), operation_id="native-md-stream")
    md.calls.clear()

    async def fresh_text():
        yield MessageChain([Plain("t")]).use_markdown(False)

    text_sender = StreamingCore(md.text, md.state)
    try:
        await text_sender.send(chat.route, fresh_text(), operation_id="native-text-stream")
    finally:
        await text_sender.close()
    frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
    assert [frame["content_type"] for frame in frames] == ["text", "text"]


async def test_stream_cannot_switch_format_after_the_default_is_fixed(md):
    chat = md.observe("C2C_MESSAGE_CREATE")

    async def mixed():
        yield MessageChain([Plain("first")])
        yield MessageChain([Plain("second")]).use_markdown(False)

    with pytest.raises(V2Error) as exc:
        await md.md_stream.send(chat.route, mixed(), operation_id="mixed-stream")
    assert exc.value.code == "stream_format_changed"


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
async def test_leading_break_keeps_the_stream_markdown_default(md, event):
    chat = md.observe(event)

    async def fragments():
        yield MessageChain(type="break")
        yield MessageChain([Plain("visible content")])

    result = await md.md_stream.send(chat.route, fragments(), source=chat.source)
    assert result["state"] == "sent"
    if event == "GROUP_AT_MESSAGE_CREATE":
        body = next(body for path, body in md.calls if path.endswith("/messages"))
        assert body["markdown"]["content"].endswith("visible content")
    else:
        frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
        assert frames and all(body["content_type"] == "markdown" for body in frames)


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
async def test_stream_default_is_fixed_when_the_stream_starts(md, event):
    chat = md.observe(event)

    async def fragments():
        yield MessageChain([Plain("first")])
        md.md.markdown_default = False  # A later default change must not reformat or interrupt.
        yield MessageChain([Plain(" second")])

    result = await md.md_stream.send(chat.route, fragments(), source=chat.source,
                                     operation_id="snapshot-" + event)
    assert result["state"] == "sent"
    if event == "C2C_MESSAGE_CREATE":
        stream_frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
        assert all(body["content_type"] == "markdown" for body in stream_frames)
    else:
        body = next(body for path, body in md.calls if path.endswith("/messages"))
        assert body["markdown"]["content"] == "first second"


async def test_break_after_text_follows_the_locked_text_format(md):
    chat = md.observe("C2C_MESSAGE_CREATE")

    async def fragments():
        yield MessageChain([Plain("line")]).use_markdown(False)
        yield MessageChain(type="break")
        yield MessageChain([Plain("next")])

    await md.md_stream.send(chat.route, fragments(), operation_id="break-after-text")
    frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
    assert [body["content_type"] for body in frames] == ["text"] * len(frames)
    assert frames[1]["content_raw"] == "\n"


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
@pytest.mark.parametrize("default", [False, True])
@pytest.mark.parametrize("flag", [False, True])
async def test_leading_break_never_overrides_the_first_explicit_text(md, event, default, flag):
    md.md.markdown_default = default
    chat = md.observe(event)

    async def fragments():
        yield MessageChain(type="break")
        yield MessageChain([Plain("first visible text")]).use_markdown(flag)
        yield MessageChain([Plain(" and continuation")])

    result = await md.md_stream.send(chat.route, fragments(), source=chat.source,
                                     operation_id=f"leading-break-{event}-{default}-{flag}")
    assert result["state"] == "sent"
    if event == "GROUP_AT_MESSAGE_CREATE":
        body = next(body for path, body in md.calls if path.endswith("/messages"))
        assert body["msg_type"] == (2 if flag else 0)
        text = body["markdown"]["content"] if flag else body["content"]
        assert text.endswith("first visible text and continuation")
    else:
        frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
        assert frames and all(frame["content_type"] == ("markdown" if flag else "text") for frame in frames)


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
async def test_multiple_leading_breaks_stay_layout_only(md, event):
    chat = md.observe(event)

    async def fragments():
        yield MessageChain(type="break")
        yield MessageChain(type="break")
        yield MessageChain([Plain("after breaks")]).use_markdown(True)

    result = await md.md_stream.send(chat.route, fragments(), source=chat.source,
                                     operation_id=f"multi-break-{event}")
    assert result["state"] == "sent"
    if event == "GROUP_AT_MESSAGE_CREATE":
        body = next(body for path, body in md.calls if path.endswith("/messages"))
        assert body["markdown"]["content"] == "\n\nafter breaks"
    else:
        frames = [body for path, body in md.calls if path.endswith("/stream_messages")]
        assert all(frame["content_type"] == "markdown" for frame in frames)
        assert frames[0]["content_raw"].endswith("after breaks")


async def test_break_only_stream_stays_empty_and_is_never_sent(md):
    chat = md.observe("C2C_MESSAGE_CREATE")

    async def fragments():
        yield MessageChain(type="break")

    with pytest.raises(V2Error) as exc:
        await md.md_stream.send(chat.route, fragments(), operation_id="break-only")
    assert exc.value.code == "stream_empty" and not md.calls
