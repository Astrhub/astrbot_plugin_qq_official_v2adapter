import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from test_messaging_state import NOW, chat_payload
from test_transport_http import MappedSession, upstream

from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore
from v2.messaging.store import MessageStore
from v2.messaging.streaming import StreamingCore
from v2.models import InstanceKey
from v2.protocol import RawEnvelope
from v2.transport.http import HTTPTransport


@pytest.fixture
async def streaming(config, tmp_path):
    clock, modes, calls = [NOW], [], []
    identity = InstanceKey.from_config(config)
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    state = ExtensionStore(store)
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        data = await request.json()
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "stream-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot stream-fixture"
        calls.append((request.path, data))
        mode = modes.pop(0) if modes else "ok"
        if mode == "wait":
            entered.set()
            await release.wait()
        if mode in {"401", "429"}:
            return web.json_response({"code": 50002 if mode == "429" else 11244}, status=int(mode), headers={"Retry-After": "0"})
        if mode == "missing":
            return web.json_response({"remain_msg_len": 0})
        if mode == "ambiguous":
            return web.json_response({"code": 50055001})
        if mode == "event_expired":
            return web.json_response({"code": 40034026})
        if mode == "500":
            return web.json_response({"code": 50001}, status=500)
        return web.json_response({"id": "real-stream-id", "remain_msg_len": 0, "ext_info": {"ref_idx": "REFIDX_stream"}})
    async def sleep(seconds):
        clock[0] += seconds
    async with upstream(handler) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        sender = SendingCore(identity, http, store, is_online=lambda: True, ws_online=lambda: True)
        core = StreamingCore(sender, state, sleep=sleep)
        def observe(event="C2C_MESSAGE_CREATE"):
            chat = convert_chat(identity, RawEnvelope(chat_payload(event), clock[0]))
            store.observe(chat)
            return chat
        try:
            yield SimpleNamespace(core=core, sender=sender, state=state, store=store, http=http, observe=observe,
                                  clock=clock, calls=calls, modes=modes, entered=entered, release=release)
        finally:
            release.set()
            await core.close()
            await sender.close()
            await state.close()
            await http.close()
            store.close()


async def chains(*values, markdown=False):
    for value in values:
        yield MessageChain([Plain(value)]).use_markdown(markdown)


@pytest.mark.parametrize("mode,values,wire", [("append", ["hello", " world"], ["hello", " world", ""]),
                                           ("replace", ["hello", "hello world"], ["hello", "hello world", "hello world"])])
async def test_native_fragments_share_real_id_sequence_and_one_quota(streaming, mode, values, wire):
    s = streaming
    chat = s.observe()
    result = await s.core.send(chat.route, chains(*values), source=chat.source, input_mode=mode, operation_id="one-stream")
    assert result["message_id"] == "real-stream-id" and result["mode"] == "native" and result["remain_msg_len"] == 0
    assert [path for path, _ in s.calls] == ["/v2/users/user-one/stream_messages"] * 3
    bodies = [body for _, body in s.calls]
    assert [b["index"] for b in bodies] == [0, 1, 2]
    assert [b["input_state"] for b in bodies] == [1, 1, 10]
    assert [b["content_raw"] for b in bodies] == wire
    assert "stream_msg_id" not in bodies[0] and all(b["stream_msg_id"] == "real-stream-id" for b in bodies[1:])
    assert all(b["msg_seq"] == 1 and b["msg_id"] == "msg-one" and "is_wakeup" not in b for b in bodies)
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
    assert s.store.operation(chat.route.robot, "one-stream")["state"] == "sent"
    assert all(url == "https://api.bot.qq.com/v2/users/user-one/stream_messages" for method, url, _ in s.http.session.calls if url.endswith("stream_messages"))


@pytest.mark.parametrize("failure", ["format", "prefix", "exception", "expired"])
async def test_partial_failure_is_unknown_and_never_refunded_or_replayed(streaming, failure):
    s = streaming
    chat = s.observe()
    async def generator():
        yield MessageChain([Plain("prefix")])
        if failure == "format":
            yield MessageChain([Plain("changed")]).use_markdown(True)
        elif failure == "prefix":
            yield MessageChain([Plain("replaced")])
        elif failure == "expired":
            s.clock[0] += 3601
            yield MessageChain([Plain("late")])
        else:
            raise RuntimeError("private provider detail must not be echoed")
    with pytest.raises(V2Error) as error:
        await s.core.send(chat.route, generator(), source=chat.source, input_mode="replace", operation_id="failed")
    assert error.value.phase == "result_unknown" and "private provider" not in str(error.value)
    assert len(s.calls) == 1
    assert s.store.operation(chat.route.robot, "failed")["state"] == "unknown"
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
    with pytest.raises(V2Error):
        await s.core.send(chat.route, chains("prefix"), source=chat.source, input_mode="replace", operation_id="failed")
    assert len(s.calls) == 1 and not s.core.tasks


@pytest.mark.parametrize("error_index", [0, 1, 2])
async def test_missing_id_at_each_fragment_keeps_unknown(streaming, error_index):
    s = streaming
    chat = s.observe()
    s.modes[:] = ["ok"] * error_index + ["missing"]
    with pytest.raises(V2Error):
        await s.core.send(chat.route, chains("a", "b"), source=chat.source, operation_id="unknown")
    assert len(s.calls) == error_index + 1
    assert s.store.operation(chat.route.robot, "unknown")["state"] == "unknown"
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1


@pytest.mark.parametrize("status", ["401", "429"])
async def test_definite_rejection_retry_uses_same_fragment(streaming, status):
    s = streaming
    chat = s.observe()
    s.modes[:] = [status, "ok"]
    await s.core.send(chat.route, chains("one"), source=chat.source)
    assert s.calls[0][1] == s.calls[1][1]
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1


async def test_aggregate_fallback_and_empty_generator(streaming):
    s = streaming
    chat = s.observe("GROUP_AT_MESSAGE_CREATE")
    assert s.core.mode(chat.route) == "aggregate"
    result = await s.core.send(chat.route, chains("hello", " world"), source=chat.source)
    assert result["mode"] == "aggregate" and len(s.calls) == 1
    assert s.calls[0][1]["content"] == "hello world"
    with pytest.raises(V2Error) as error:
        await s.core.send(chat.route, chains("", ""), source=chat.source)
    assert error.value.code == "stream_empty" and len(s.calls) == 1


async def test_cancelled_middle_frame_keeps_unknown_and_closes_generator(streaming):
    s = streaming
    chat = s.observe()
    closed = []
    async def generator():
        try:
            yield MessageChain([Plain("first")])
            yield MessageChain([Plain("second")])
        finally:
            closed.append(True)
    s.modes[:] = ["ok", "wait"]
    task = asyncio.create_task(s.core.send(chat.route, generator(), source=chat.source, operation_id="cancelled"))
    await asyncio.wait_for(s.entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == [True] and not s.core.tasks
    assert s.store.operation(chat.route.robot, "cancelled")["state"] == "unknown"


async def test_confirmed_stream_result_survives_generator_cleanup_failure(streaming):
    s = streaming
    chat = s.observe()
    class Generator:
        count = 0
        def __aiter__(self):
            return self
        async def __anext__(self):
            self.count += 1
            if self.count > 1:
                raise StopAsyncIteration
            return MessageChain([Plain("confirmed")])
        async def aclose(self):
            raise RuntimeError("private cleanup detail")
    result = await s.core.send(chat.route, Generator(), source=chat.source, operation_id="sent-with-warning")
    assert result["state"] == "sent" and result["message_id"] == "real-stream-id"
    assert result["cleanup_error"] == "generator_close_failed" and "private" not in str(result)
    assert s.store.operation(chat.route.robot, "sent-with-warning")["state"] == "sent"
    assert not s.core.tasks and len(s.calls) == 2
