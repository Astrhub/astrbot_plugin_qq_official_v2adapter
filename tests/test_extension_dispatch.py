import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from test_interactions import authorization_notice, interaction
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import accept
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW
from test_transport_http import MappedSession, upstream

from v2.protocol import RawEnvelope


@pytest.fixture
async def dispatch(receiver, config, monkeypatch):
    owner, instance = receiver
    from astrbot.core.utils.metrics import Metric
    async def metric_upload(**kwargs):
        return None
    monkeypatch.setattr(Metric, "upload", metric_upload)
    calls, modes = [], []
    entered, release = asyncio.Event(), asyncio.Event()
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "ack-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot ack-fixture"
        assert request.headers["X-Union-Appid"] == config["appid"]
        body = await request.json()
        calls.append((request.method, request.path, body))
        mode = modes.pop(0) if modes else "ok"
        if mode == "wait":
            entered.set()
            await release.wait()
        if request.path.startswith("/interactions/"):
            assert request.method == "PUT" and body == {"code": 0}
            if mode == "missing_contract":
                return web.json_response({"unexpected": True})
            if mode == "server_429":
                return web.json_response({"code": 50002}, status=429,
                                         headers={"Retry-After": "3", "X-Tps-Trace-Id": "ack-rate-trace"})
            return web.Response(status=204)
        assert request.path == "/v2/groups/group-one/messages"
        return web.json_response({"id": "actual-event-reply", "ext_info": {"ref_idx": "REFIDX_callback"}})
    async with upstream(handler) as base:
        instance.http._factory = lambda: MappedSession(base)
        instance.ack_http._factory = lambda: MappedSession(base)
        monkeypatch.setattr(instance.sender, "is_online", lambda: True)
        try:
            yield SimpleNamespace(owner=owner, instance=instance, service=instance.extensions, config=config,
                                  calls=calls, modes=modes, entered=entered, release=release)
        finally:
            release.set()
            owner.extension_state.tasks.clear()  # Any deliberately occupied fixture slots are not real jobs.
            await instance.extensions.close()


async def settle(s):
    await asyncio.wait_for(asyncio.gather(*tuple(s.service.tasks)), 3)


async def test_ack_lane_preempts_backpressure_and_uses_inner_id_without_chat_identity(dispatch):
    s = dispatch
    await s.instance.http.token()
    for _ in range(6):
        await s.instance.http._slots.acquire()
    s.owner.extension_state.tasks.update(object() for _ in range(32))
    for _ in range(128):
        s.instance._event_queue.put_nowait(None)
    accept(s.owner, s.instance, message_id="blocked-chat")
    frame = interaction(s.config)
    s.owner.inbox.accept(s.instance.identity.settings_key, RawEnvelope(frame, NOW))
    assert s.instance.consumer.step()  # Receives the interaction before the older blocked chat.
    await settle(s)
    assert s.calls == [("PUT", "/interactions/inner-interaction", {"code": 0})]
    record = s.service.records()[0]
    assert record["ack"] == "succeeded" and record["business"] == "rejected"
    assert record["metadata"]["event_id"] == "outer-event" and record["metadata"]["message_id"] == "real-operated-message"
    assert s.owner.messages.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 0
    assert s.owner.inbox.pending(s.instance.identity.settings_key)[0]["payload"]["d"]["id"] == "blocked-chat"
    assert not s.instance.consumer.step()
    for _ in range(6):
        s.instance.http._slots.release()
    assert all(url == "https://api.bot.qq.com/interactions/inner-interaction" for _, url, _ in s.instance.ack_http.session.calls)


@pytest.mark.parametrize("kind,acks", [(11, 1), (12, 1), (13, 0), (16, 0)])
async def test_only_documented_types_ack_and_duplicates_never_execute(dispatch, kind, acks):
    s = dispatch
    payload = interaction(s.config, kind=kind)
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert len(s.calls) == acks and s.instance._event_queue.empty()
    assert s.service.records()[0]["ack"] == ("succeeded" if acks else "not_required")


async def test_authorization_notice_is_recorded_without_ack_or_chat_and_does_not_block_delivery(dispatch):
    s = dispatch
    key = s.instance.identity.settings_key
    payload = authorization_notice()
    s.owner.inbox.accept(key, RawEnvelope(payload, NOW))
    assert s.instance.consumer.step()
    await settle(s)
    assert not s.owner.inbox.retained(key) and not s.owner.inbox.pending(key)
    record = s.service.records()[0]
    assert record["ack"] == "not_required" and record["business"] == "typed_notice"
    assert record["metadata"]["interaction_type"] == 20 and record["metadata"]["actor"] is None
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert len(s.service.records()) == 1 and s.instance._event_queue.empty()
    assert not s.calls and s.instance.http.session is None and s.instance.ack_http.session is None
    assert s.owner.messages.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 0
    assert s.owner.messages.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    accept(s.owner, s.instance, message_id="chat-after-notice")
    assert s.instance.consumer.step()
    chat = s.instance._event_queue.get_nowait()
    assert chat.message_obj.message_id == "chat-after-notice"
    chat.cleanup_temporary_local_files()


async def test_ack_unknown_is_durable_and_not_replayed(dispatch):
    s = dispatch
    s.modes[:] = ["wait"]
    deadline = asyncio.timeout(None)
    s.service.timeout_factory = lambda seconds: deadline
    payload = interaction(s.config)
    assert s.service.accept(payload, NOW)
    await asyncio.wait_for(s.entered.wait(), 2)
    deadline.reschedule(asyncio.get_running_loop().time() - 1)
    await settle(s)
    assert s.entered.is_set() and s.service.records()[0]["ack"] == "unknown"
    assert s.service.records()[0]["business"] == "not_executed"
    s.release.set()
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert len(s.calls) == 1
    state_type = type(s.owner.extension_state)
    restarted = state_type(s.owner.messages)
    s.service.state = restarted
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert len(s.calls) == 1 and s.service.records()[0]["ack"] == "unknown"
    await restarted.close()


async def test_owned_projection_uses_event_id_and_never_observes_chat_identity(dispatch):
    s = dispatch
    key = s.instance.identity.settings_key
    current = s.owner.store.get(key)
    saved = s.owner.store.mutate(key, current["revision"], "fixture", operation="save", patch={"extensions": {"keyboard_enabled": True, "keyboard_execute": True}})
    s.owner.store.mutate(key, saved["revision"], "fixture", operation="apply")
    accept(s.owner, s.instance)
    assert s.instance.consumer.step()
    chat = s.instance._event_queue.get_nowait()
    chat.cleanup_temporary_local_files()
    identities = [tuple(row) for row in s.owner.messages.db.execute("SELECT * FROM identities")]
    node = {"id": "fixture.command", "binding": "source", "command": "/run", "parameters": [], "group": False, "enabled": True, "permission": [], "menu_entry": False}
    catalog = {"scene": "group", "version": "one", "nodes": [node]}
    s.service.tickets.catalog_provider = lambda route: catalog
    token = s.service.tickets.issue(chat.route, "user-one", node, "/run", "execute", "one")
    with s.owner.messages.transaction():
        s.owner.messages.db.execute("DELETE FROM targets")  # The owned ticket is not a new chat observation.
    payload = interaction(s.config, token=token)
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert s.service.records()[0]["business"] == "queued", s.service.records()
    projected = s.instance._event_queue.get_nowait()
    assert projected.message_obj.message_id == "" and projected.bot._source.message_id is None
    assert projected.raw_data["derived_from_interaction"] and projected.raw_data["t"] == "INTERACTION_CREATE"
    assert projected.get_extra("qq_source_kind") == "interaction_projection"
    projected.should_call_llm(True)
    assert not projected.call_llm
    projected.set_extra("activated_handlers", [SimpleNamespace(handler_full_name=node["id"]), SimpleNamespace(handler_full_name="unrelated")])
    assert projected.command_admitted and len(projected.get_extra("activated_handlers")) == 1
    await projected.send(MessageChain([Plain("real callback response")]))
    assert s.calls[-1][2]["event_id"] == "outer-event" and "msg_id" not in s.calls[-1][2]
    projected.cleanup_temporary_local_files()
    assert not s.owner.delivery_slots.events
    assert s.service.records()[0]["business"] == "finished_unconfirmed"
    assert identities == [tuple(row) for row in s.owner.messages.db.execute("SELECT * FROM identities")]
    assert s.service.accept(payload, NOW)
    await settle(s)
    assert s.instance._event_queue.empty() and len(s.calls) == 2


async def test_completed_callback_capacity_does_not_reanimate_old_ack(dispatch):
    from datetime import UTC, datetime
    s = dispatch
    clock = [NOW]
    s.owner.messages.clock = lambda: clock[0]
    s.service.event_capacity = 1
    old = interaction(s.config, kind=12)
    s.service.accept(old, clock[0])
    await settle(s)
    clock[0] += 301
    fresh = interaction(s.config, kind=12, interaction_id="new-interaction", event_id="new-event")
    fresh["d"]["timestamp"] = datetime.fromtimestamp(clock[0], UTC).isoformat()
    s.service.accept(fresh, clock[0])
    await settle(s)
    assert len(s.service.records()) == 1 and len(s.calls) == 2
    with pytest.raises(Exception) as error:
        s.service.accept(old, clock[0])
    assert error.value.code == "interaction_expired" and len(s.calls) == 2
