"""Owned convenience sends remain bound through every asynchronous wire boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from test_media_upload import media as media
from test_media_upload import sending_core
from test_messaging_send import sending as sending
from test_streaming import streaming as streaming

from v2.client import V2Client
from v2.errors import V2Error


async def test_revoked_owned_convenience_send_and_file_never_reserve_or_send(sending):
    s = sending
    _, client = s.observe()
    owner = object()
    view = client.qq.with_options(owner=owner)
    client._state.revoke_owner(owner)
    for operation in (view.send("group", "group-one", "text"),
                      view.send_file("group", "group-one", "base64://eA==")):
        with pytest.raises(V2Error) as error:
            await operation
        assert error.value.code == "stale_owner"
    assert not s.calls and s.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


async def test_owned_send_revoked_during_token_wait_never_crosses_http_boundary(sending):
    s = sending
    chat, client = s.observe()
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()
    original = s.http.token

    async def blocked(*, rejected=None):
        entered.set()
        await release.wait()
        return await original(rejected=rejected)

    s.http.token = blocked
    task = asyncio.create_task(client.qq.with_options(owner=owner).send("group", "group-one", "queued", operation_id="owned-token"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert not s.calls
        assert s.store.operation(chat.route.robot, "owned-token")["state"] == "not_sent"
        assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    finally:
        release.set()
        s.http.token = original
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owned_send_does_not_start_active_fallback_after_owner_unloads(sending):
    s = sending
    chat, client = s.observe()
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()
    original = s.http.token
    calls = [0]
    async def blocked(*, rejected=None):
        calls[0] += 1
        if calls[0] == 2:
            entered.set()
            await release.wait()
        return await original(rejected=rejected)
    s.http.token = blocked
    s.modes.append(40034128)
    task = asyncio.create_task(client.qq.with_options(owner=owner).send(
        "group", "group-one", "fallback", operation_id="owned-fallback"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(s.calls) == 1 and s.calls[0][1]["msg_id"] == "msg-one"
        client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert len(s.calls) == 1 and s.store.operation(chat.route.robot, "owned-fallback")["state"] == "not_sent"
    finally:
        release.set()
        s.http.token = original
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owned_send_file_revoked_after_media_receipt_does_not_send_message(media):
    m = media
    core, chat = sending_core(m)
    client = V2Client(m.identity)
    client._state.http, client._state.sender = m.http, core
    bound = client.bind(chat.route, source=chat.source)
    owner = object()
    m.modes.append("wait_files")
    task = asyncio.create_task(bound.qq.with_options(owner=owner).send_file(
        "group", "group-one", "base64://eA==", operation_id="owned-file"))
    try:
        await asyncio.wait_for(m.entered.wait(), 2)
        client._state.revoke_owner(owner)
        m.release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert [path for path, _ in m.calls] == [
            "/v2/groups/group-one/upload_prepare", "/v2/groups/group-one/upload_part_finish",
            "/v2/groups/group-one/files"]
        assert m.store.operation(chat.route.robot, "owned-file")["state"] == "not_sent"
        assert m.pool.used == 0
    finally:
        m.release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await core.close()


async def test_owned_send_file_revoked_before_media_wire_leaves_no_qq_upload(media):
    m = media
    core, chat = sending_core(m)
    client = V2Client(m.identity)
    client._state.http, client._state.sender = m.http, core
    bound = client.bind(chat.route, source=chat.source)
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()
    original = m.http.token
    async def blocked(*, rejected=None):
        entered.set()
        await release.wait()
        return await original(rejected=rejected)
    m.http.token = blocked
    task = asyncio.create_task(bound.qq.with_options(owner=owner).send_file(
        "group", "group-one", "base64://eA==", operation_id="owned-media-token"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert not m.calls and not m.puts
        assert m.pool.used == 0
        assert m.store.operation(chat.route.robot, "owned-media-token")["state"] == "not_sent"
    finally:
        release.set()
        m.http.token = original
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await core.close()


def bound_stream(streaming):
    s = streaming
    chat = s.observe()
    client = V2Client(s.sender.identity)
    client._state.http, client._state.sender, client._state.streaming = s.http, s.sender, s.core
    return SimpleNamespace(client=client.bind(chat.route, source=chat.source), chat=chat)


async def test_owned_stream_revoked_before_first_fragment_never_sends(streaming):
    s = streaming
    bound = bound_stream(s)
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        entered.set()
        await release.wait()
        yield MessageChain([Plain("first")])

    task = asyncio.create_task(bound.client.qq.with_options(owner=owner).send_streaming(
        "c2c", "user-one", delayed(), operation_id="owned-first"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        bound.client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert not s.calls and s.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owned_first_stream_frame_revoked_during_token_wait_stays_not_sent(streaming):
    s = streaming
    bound = bound_stream(s)
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()
    original = s.http.token
    async def blocked(*, rejected=None):
        entered.set()
        await release.wait()
        return await original(rejected=rejected)
    s.http.token = blocked
    async def chunks():
        yield MessageChain([Plain("first")])
    task = asyncio.create_task(bound.client.qq.with_options(owner=owner).send_streaming(
        "c2c", "user-one", chunks(), operation_id="owned-frame-token"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        bound.client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert not s.calls
        assert s.store.operation(bound.chat.route.robot, "owned-frame-token")["state"] == "not_sent"
        assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    finally:
        release.set()
        s.http.token = original
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owned_stream_revoked_between_confirmed_fragments_keeps_partial_history(streaming):
    s = streaming
    bound = bound_stream(s)
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        yield MessageChain([Plain("first")])
        entered.set()
        await release.wait()
        yield MessageChain([Plain(" second")])

    task = asyncio.create_task(bound.client.qq.with_options(owner=owner).send_streaming(
        "c2c", "user-one", delayed(), operation_id="owned-middle"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert len(s.calls) == 1 and s.calls[0][1]["input_state"] == 1
        bound.client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "result_unknown"
        assert error.value.details["partial_message_id"] == "real-stream-id"
        assert len(s.calls) == 1 and s.store.operation(bound.chat.route.robot, "owned-middle")["state"] == "unknown"
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_owned_aggregate_revoked_after_generator_wait_does_not_fallback_send(streaming):
    s = streaming
    bound = bound_stream(s)
    owner = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed():
        yield MessageChain([Plain("complete")])
        entered.set()
        await release.wait()

    task = asyncio.create_task(bound.client.qq.with_options(owner=owner).send_streaming(
        "c2c", "user-one", delayed(), use_fallback=True, operation_id="owned-aggregate"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        bound.client._state.revoke_owner(owner)
        release.set()
        with pytest.raises(V2Error) as error:
            await task
        assert error.value.code == "stale_owner" and error.value.phase == "not_sent"
        assert not s.calls and s.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_public_send_does_not_accept_guard_from_action_parameters(sending):
    s = sending
    chat, client = s.observe()
    with pytest.raises(V2Error) as direct:
        await client.send(chat.route, "not sent", guard=lambda: None)
    assert direct.value.code == "invalid_params"
    with pytest.raises(V2Error) as action:
        await client.call_action("send_group_msg", group_id="group-one", message="not sent", guard="fake")
    assert action.value.code == "invalid_params" and not s.calls


async def test_unowned_convenience_send_keeps_existing_delivery(sending):
    s = sending
    _, client = s.observe()
    sent = await client.qq.send("group", "group-one", "unowned")
    assert sent["state"] == "sent" and len(s.calls) == 1


async def test_unowned_convenience_stream_keeps_existing_delivery(streaming):
    bound = bound_stream(streaming)
    async def chunks():
        yield MessageChain([Plain("unowned")])
    result = await bound.client.qq.send_streaming("c2c", "user-one", chunks(), operation_id="unowned-stream")
    assert result["state"] == "sent" and len(streaming.calls) == 2
