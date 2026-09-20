import asyncio
import copy
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot.core.message.components import At, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain

from test_messaging_state import NOW, chat_payload
from test_transport_http import MappedSession, upstream
from v2.client import V2Client
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore, parse_message
from v2.messaging.store import IdentityView, MessageStore
from v2.models import InstanceKey, SessionRoute
from v2.protocol import RawEnvelope, RequestSpec
from v2.transport.http import HTTPTransport


@pytest.fixture
async def sending(config, tmp_path, monkeypatch):
    from astrbot.core.utils.metrics import Metric
    async def metric_upload(**kwargs):
        return None
    monkeypatch.setattr(Metric, "upload", metric_upload)
    clock = [NOW]
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    identity = InstanceKey.from_config(config)
    calls, modes = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    async def handle(request):
        assert request.method == "POST"
        data = await request.json()
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "send-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot send-fixture"
        calls.append((request.path, data))
        mode = modes.pop(0) if modes else "ok"
        if mode == "wait":
            entered.set()
            await release.wait()
        elif mode == "401":
            return web.json_response({"code": 11244}, status=401)
        elif mode == "no-id":
            return web.json_response({"code": 0})
        elif mode == "500":
            return web.json_response({"code": 50055001}, status=500)
        elif isinstance(mode, int):
            return web.json_response({"code": mode}, headers={"X-Tps-Trace-Id": "fixture-trace"})
        return web.json_response({"id": f"real-format-{len(calls)}", "timestamp": "2027-01-15T08:00:00+08:00", "ext_info": {"ref_idx": f"REFIDX_sent{len(calls)}"}})
    async with upstream(handle) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        core = SendingCore(identity, http, store, is_online=lambda: True, ws_online=lambda: True)
        client = V2Client(identity)
        client._state.http, client._state.sender, client._state.cache = http, core, IdentityView(store, identity.robot)
        def observe(event="GROUP_AT_MESSAGE_CREATE", **kwargs):
            chat = convert_chat(identity, RawEnvelope(chat_payload(event, **kwargs), clock[0]))
            store.observe(chat)
            return chat, client.bind(chat.route, source=chat.source)
        try:
            yield SimpleNamespace(clock=clock, store=store, core=core, client=client, http=http, calls=calls, modes=modes, observe=observe, entered=entered, release=release)
        finally:
            release.set()
            await core.close()
            await http.close()
            store.close()


@pytest.mark.parametrize("event,path", [("GROUP_AT_MESSAGE_CREATE", "/v2/groups/group-one/messages"),
    ("C2C_MESSAGE_CREATE", "/v2/users/user-one/messages"), ("AT_MESSAGE_CREATE", "/channels/group-one/messages"),
    ("DIRECT_MESSAGE_CREATE", "/dms/group-one/messages")])
async def test_four_scenes_share_real_send_result(sending, event, path):
    s = sending
    chat, client = s.observe(event)
    result = await client.send(chat.route, MessageChain([Plain("literal <&>")]))
    assert result["message_id"] == "real-format-1"
    assert s.calls == [(path, {"content": "literal &lt;&amp;&gt;", "msg_id": "msg-one", **({"msg_type": 0, "msg_seq": 1} if chat.route.scene in {"c2c", "group"} else {})})]
    assert s.store.operation(client.identity.robot, result["operation_id"])["state"] == "sent"
    assert s.store.reference(chat.route, result["message_id"]) == ("REFIDX_sent1" if chat.route.scene in {"c2c", "group"} else result["message_id"])


async def test_onebot_three_entries_and_native_use_exact_source_and_sequence(sending):
    s = sending
    chat, client = s.observe()
    for method in (client.api.call_action, client.call_action):
        assert (await method("send_group_msg", group_id="group-one", message="ok"))["message_id"].startswith("real-format-")
    await client.send_group_msg(group_id="group-one", message={"type": "text", "data": {"text": "ok"}})
    await client.qq.send("group", "group-one", "native")
    assert [body["msg_seq"] for _, body in s.calls] == [1, 2, 3, 4]
    assert all(body["msg_id"] == "msg-one" for _, body in s.calls)
    s.observe(message_id="newer")
    await client.send_group_msg(group_id="group-one", message="still original")
    assert s.calls[-1][1]["msg_id"] == "msg-one"
    with pytest.raises(V2Error) as exc:
        await client.send_group_msg(group_id="group-one", message="over quota")
    assert exc.value.code == "passive_quota_exhausted" and len(s.calls) == 5


async def test_instance_and_cross_target_are_active_not_borrowed(sending):
    s = sending
    chat, client = s.observe()
    s.observe(target="other", message_id="other-source")
    await client.send_group_msg(group_id="other", message="active")
    await s.client.send_group_msg(group_id="group-one", message="active")
    assert all("msg_id" not in body and "msg_seq" not in body for _, body in s.calls)
    with pytest.raises(V2Error) as exc:
        await client.send_group_msg(group_id="unobserved", message="no")
    assert exc.value.code == "identity_not_observed"
    with pytest.raises(V2Error):
        await client.qq.request(RequestSpec("production", "POST", "/v2/groups/group-one/messages", json_body={"content": "bypass"}))
    assert len(s.calls) == 2


async def test_reply_indices_at_and_markdown_matrix(sending):
    s = sending
    chat, client = s.observe()
    result = await client.send(chat.route, MessageChain([Reply(id="msg-one"), At(qq="user-one"), Plain("hi")]))
    body = s.calls[-1][1]
    assert body["message_reference"] == {"message_id": "REFIDX_msg-one"}
    assert body["content"] == '<qqbot-at-user id="user-one" />hi'
    await client.qq.send("group", "group-one", MessageChain([Plain("# title")]).use_markdown(True))
    assert s.calls[-1][1]["markdown"] == {"content": "# title"} and "content" not in s.calls[-1][1]
    before = len(s.calls)
    for chain in [MessageChain([Reply(id="unobserved"), Plain("x")]), MessageChain([At(qq="all"), Plain("x")]),
                  [{"type": "text", "data": {"text": "x"}}, {"type": "image", "data": {"file": "x"}}]]:
        with pytest.raises(V2Error):
            await client.send(chat.route, chain)
    with pytest.raises(V2Error):
        await client.qq.send("group", "group-one", '<qqbot-cmd-enter text="test" />', markdown=True)
    assert len(s.calls) == before and result["message_id"]


def test_cq_single_decode_and_auto_escape():
    atoms, md = parse_message("a&#91;b&#93;&amp;[CQ:at,qq=id&#44;two]", onebot=True)
    assert atoms == [("text", "a[b]&"), ("at", "id,two")] and not md
    assert parse_message("[CQ:at,qq=x]&amp;", onebot=True, auto_escape=True)[0] == [("text", "[CQ:at,qq=x]&amp;")]
    assert parse_message({"type": "text", "data": {"text": "&#91;"}}, onebot=True)[0] == [("text", "&#91;")]
    for value in ("[CQ:at,qq=x,qq=y]", "[CQ:unknown,a=x]", "[CQ:at,qq=x", [{"type": "at", "data": {"qq": 123}}]):
        with pytest.raises(V2Error):
            parse_message(value, onebot=True)


async def test_auth_retry_keeps_sequence_and_rechecks_original_deadline(sending):
    s = sending
    chat, client = s.observe()
    s.modes[:] = ["401", "ok"]
    await client.send(chat.route, "retry")
    assert [body["msg_seq"] for _, body in s.calls] == [1, 1]
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
    real_token = s.http.token
    async def delayed(*args, **kwargs):
        token = await real_token(*args, **kwargs)
        s.clock[0] += 301
        return token
    s.http.token = delayed
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "too late")
    assert exc.value.code == "reply_expired" and exc.value.phase == "not_sent" and len(s.calls) == 2
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["no-id", "500", 40054005, 304023])
async def test_unknown_write_never_refunds_or_replays(sending, mode):
    s = sending
    chat, client = s.observe()
    s.modes.append(mode)
    with pytest.raises(V2Error) as exc:
        await client.qq.send("group", "group-one", "once", operation_id="unknown-op")
    assert exc.value.phase == "result_unknown"
    assert s.store.operation(chat.route.robot, "unknown-op")["state"] == "unknown"
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
    with pytest.raises(V2Error) as exc:
        await client.qq.send("group", "group-one", "once", operation_id="unknown-op")
    assert exc.value.code == "send_result_unknown" and len(s.calls) == 1


async def test_cancel_after_sent_is_unknown_and_bound_event_not_successful(sending):
    s = sending
    chat, client = s.observe()
    from v2.event import V2MessageEvent
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    event = V2MessageEvent(chat.message, PlatformMetadata("qq_official_v2", "fixture", "test-v2"), s.client, chat.route)
    s.modes.append("wait")
    task = asyncio.create_task(event.send(MessageChain([Plain("once")])))
    await s.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not event._has_send_oper
    assert s.store.db.execute("SELECT state FROM operations").fetchone()[0] == "unknown"
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1


async def test_c2c_60minute_conflict_respects_server_rejection(sending):
    s = sending
    chat, client = s.observe("C2C_MESSAGE_CREATE")
    s.clock[0] += 301
    s.modes.append(40034005)
    with pytest.raises(V2Error) as exc:
        await client.send_private_msg(user_id="user-one", message="within overview but server refused")
    assert exc.value.business_code == 40034005 and exc.value.trace_id == "fixture-trace"
    with pytest.raises(V2Error) as exc:
        await client.send_private_msg(user_id="user-one", message="not active fallback")
    assert exc.value.code == "reply_source_rejected" and len(s.calls) == 1


async def test_success_marks_host_send_only_after_true_id_and_keeps_permission_unknown(sending):
    from astrbot.core.platform.platform_metadata import PlatformMetadata
    from v2.event import V2MessageEvent
    s = sending
    chat, _ = s.observe()
    event = V2MessageEvent(chat.message, PlatformMetadata("qq_official_v2", "fixture", "test-v2"), s.client, chat.route)
    await event.send(MessageChain([Plain("ok")]))
    assert event._has_send_oper and event.get_extra("qq_send_result")["message_id"] == "real-format-1"
    assert s.client.capabilities()["actions"]["send_group_msg"]["permission"] == "unknown"


async def test_concurrent_quota_and_explicit_success_idempotency(sending):
    s = sending
    chat, client = s.observe()
    results = await asyncio.gather(*(client.send_group_msg(group_id="group-one", message=f"reply{i}") for i in range(10)), return_exceptions=True)
    assert sum(isinstance(r, dict) for r in results) == 5 and len(s.calls) == 5
    assert all(isinstance(r, dict) or r.code == "passive_quota_exhausted" for r in results)
    assert sorted(body["msg_seq"] for _, body in s.calls) == [1, 2, 3, 4, 5]
    s.clock[0] += 2
    chat, client = s.observe(message_id="idempotent", timestamp=s.clock[0])
    first = await client.qq.send("group", "group-one", "same", operation_id="stable")
    second = await client.qq.send("group", "group-one", "same", operation_id="stable")
    assert first == second and len(s.calls) == 6
    with pytest.raises(V2Error) as exc:
        await client.qq.send("group", "group-one", "changed", operation_id="stable")
    assert exc.value.code == "operation_conflict"


async def test_definite_markdown_rejection_releases_but_expiry_never_falls_back(sending):
    s = sending
    chat, client = s.observe()
    s.modes.append(304036)
    with pytest.raises(V2Error) as exc:
        await client.qq.send("group", "group-one", "# help", markdown=True)
    assert exc.value.phase == "rejected" and exc.value.business_code == 304036
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    await client.send_group_msg(group_id="group-one", message="help text")
    assert s.calls[-1][1]["msg_seq"] == 2 and s.calls[-1][1]["msg_id"] == "msg-one"


async def test_observed_avatar_precedence_sizes_and_no_channel_id_formula(sending):
    s = sending
    payload = chat_payload("AT_MESSAGE_CREATE")
    payload["d"]["author"]["avatar"] = "https://thirdqq.qlogo.cn/fixture"
    chat = convert_chat(s.client.identity, RawEnvelope(payload, NOW))
    s.store.observe(chat)
    client = s.client.bind(chat.route, source=chat.source)
    avatar = await client._qq_get_avatar(user_id="user-one")
    assert avatar == {"url": "https://thirdqq.qlogo.cn/fixture", "kind": "user", "size": None, "source": "chat_event", "verified": False}
    with pytest.raises(V2Error):
        await client._qq_get_avatar(user_id="user-one", size=640)
    group, client = s.observe()
    for size in (0, 100, 140, 640):
        assert (await client._qq_get_avatar(user_id="user-one", size=size))["url"] == f"https://q.qlogo.cn/qqapp/test-app/user-one/{size}"
        assert (await client._qq_get_avatar(kind="group", group_id="group-one", size=size))["url"].endswith(f"/group-one/{size}")
    with pytest.raises(V2Error) as exc:
        await client._qq_get_avatar(user_id="never-observed")
    assert exc.value.code == "identity_not_observed" and not s.calls


async def test_refresh_failure_after_auth_rejection_is_not_an_unknown_message(sending):
    s = sending
    chat, client = s.observe()
    original = s.http.token
    async def failing_refresh(*, rejected=None):
        if rejected:
            raise V2Error("network_failure", "token fixture", status=503, phase="result_unknown")
        return await original()
    s.http.token = failing_refresh
    s.modes.append("401")
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "rejected")
    assert exc.value.code == "token_refresh_failed" and exc.value.phase == "rejected"
    assert len(s.calls) == 1 and s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
