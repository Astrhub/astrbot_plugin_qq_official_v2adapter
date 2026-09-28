"""SDK deliveries retain the subscription owner's client lifetime."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_messaging_state import NOW, chat_payload
from test_transport_http import MappedSession, upstream

from v2.protocol import RawEnvelope
from v2.sdk.events import EventBus, EventContext, NativeEvent
from v2.sdk.ingress import CoreConsumer

pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


@pytest.fixture
async def delivery(receiver):
    owner, instance = receiver
    token_entered, token_release = asyncio.Event(), asyncio.Event()
    hold_token = [False]
    calls = []
    wire_entered, wire_release = asyncio.Event(), asyncio.Event()
    hold_business, outcome = [False], ["success"]

    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            if hold_token[0]:
                token_entered.set()
                await token_release.wait()
            return web.json_response({"access_token": "event-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot event-fixture"
        body = await request.text()
        calls.append((request.method, request.path, json.loads(body) if body else None))
        if request.path == "/users/@me":
            return web.json_response({"id": "bot", "username": "Actual", "bot": True})
        if request.path == "/v2/groups/g/bot_state":
            return web.json_response({"member_openid": "bot", "member_role": "admin"})
        if hold_business[0]:
            wire_entered.set()
            await wire_release.wait()
        if request.path == "/guilds/g/members/u/mute":
            return web.Response(status=500 if outcome[0] == "unknown" else 204)
        if request.path == "/v2/groups/g/batch_remove_members":
            return web.json_response({"remove_members_result": "success",
                "add_to_member_blacklist_fail_openids": ["u"] if outcome[0] == "partial" else []})
        assert request.path == "/v2/groups/group-one/messages"
        return web.json_response({"id": "actual-event-send", "timestamp": "2027-01-15T08:00:00+08:00",
                                  "ext_info": {"ref_idx": "REFIDX_actual-event-send"}})

    async with upstream(handler) as base:
        instance.http._factory = lambda: MappedSession(base)
        instance.sender.is_online = lambda: True
        manager = instance.client._state.management
        settings = manager.settings
        manager.settings = lambda: {**settings(), "management_writes": True}
        try:
            yield SimpleNamespace(owner=owner, instance=instance, calls=calls, hold_token=hold_token,
                token_entered=token_entered, token_release=token_release, wire_entered=wire_entered,
                wire_release=wire_release, hold_business=hold_business, outcome=outcome)
        finally:
            token_release.set()
            wire_release.set()
            await instance.http.close()


def accept(p, payload):
    key = p.instance.identity.settings_key
    receipt = p.owner.inbox.accept(key, RawEnvelope(payload, NOW, "websocket", (0, 1),
        "session", p.instance.identity.generation))
    assert receipt and p.instance.core.step()
    return receipt


def operation(event_client, name):
    if name == "read":
        return event_client.me()
    if name == "send":
        return event_client.send("group", "group-one", "literal", operation_id="delivered-send")
    assert name == "legacy_write"
    return event_client.guild_mute("g", "u", 60, operation_id="delivered-mute")


@pytest.mark.parametrize("subscription", ["callback", "stream"])
@pytest.mark.parametrize("name", ["read", "send", "legacy_write"])
async def test_event_client_revoked_during_token_wait_never_starts_business_wire(delivery, subscription, name):
    p = delivery
    plugin = object()
    observed, callback_done = [], asyncio.Event()

    async def capture(notice):
        observed.append(notice)
        callback_done.set()

    bus = p.instance.client.qq.events
    sub = bus.subscribe({"GROUP_AT_MESSAGE_CREATE"}, owner=plugin,
        callback=capture if subscription == "callback" else None)
    stream = sub.__aiter__() if subscription == "stream" else None
    task = None
    try:
        receipt = accept(p, chat_payload(message_id="delivered-owner"))
        if stream is not None:
            observed.append(await asyncio.wait_for(anext(stream), 2))
        else:
            await asyncio.wait_for(callback_done.wait(), 2)
        notice = observed[0]
        assert notice.context.receipt == receipt and notice.reply_context is not None
        p.hold_token[0] = True
        task = asyncio.create_task(operation(notice.client, name))
        await asyncio.wait_for(p.token_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        assert sub.closed
        p.token_release.set()
        with pytest.raises(RuntimeError) as stopped:
            await task
        assert stopped.value.code == "stale_owner" and stopped.value.phase == "not_sent"
        assert p.calls == []
        assert [(method, url) for method, url, _ in p.instance.http.session.calls] == [
            ("POST", "https://api.bot.qq.com/app/getAppAccessToken")]
        if name == "send":
            assert p.owner.messages.operation(p.instance.identity.robot, "delivered-send")["state"] == "not_sent"
        elif name == "legacy_write":
            assert p.owner.extension_state.operation(p.instance.identity.robot, "delivered-mute")["state"] == "not_sent"
    finally:
        p.token_release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if stream is not None:
            await stream.aclose()
        await sub.close()


async def test_fanout_owner_views_preserve_source_and_other_owner_after_unload(delivery, monkeypatch):
    p = delivery
    plugin_a, plugin_b = object(), object()
    callback_done = asyncio.Event()
    callback_seen, sources = [], []

    async def capture(event):
        callback_seen.append(event)
        callback_done.set()

    bus = p.instance.client.qq.events
    original_publish = bus.publish

    def record_source(event, *, raw_only=False):
        sources.append(event)
        return original_publish(event, raw_only=raw_only)

    monkeypatch.setattr(bus, "publish", record_source)
    a = bus.stream({"GROUP_AT_MESSAGE_CREATE"}, owner=plugin_a)
    b = bus.subscribe({"GROUP_AT_MESSAGE_CREATE"}, owner=plugin_b, callback=capture)
    stream = a.__aiter__()
    try:
        receipt = accept(p, chat_payload(message_id="fanout-owner"))
        source = sources[0]
        size = len(json.dumps(source.raw(), ensure_ascii=False).encode())
        assert bus.queued_bytes == size * 2 and a.bytes == b.bytes == size
        first = await asyncio.wait_for(anext(stream), 2)
        await asyncio.wait_for(callback_done.wait(), 2)
        second = callback_seen[0]
        assert bus.queued_bytes == a.bytes == b.bytes == 0
        assert source.client is p.instance.client.qq
        assert first is not second and first is not source and second is not source
        assert first.client is not source.client and second.client is not source.client
        assert first.client is not second.client
        assert first.client._options.owner is plugin_a and second.client._options.owner is plugin_b
        assert first.payload is second.payload is source.payload
        assert first.raw() == second.raw() == source.raw()
        assert source.schema_valid and source.typed is not None
        assert first.typed == second.typed == source.typed
        assert first.context is second.context is source.context and first.context.receipt == receipt
        assert first.key == second.key == source.key and first.reply_context is second.reply_context is source.reply_context
        assert first.schema_missing == second.schema_missing == source.schema_missing
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin_a))
        assert a.closed and not b.closed
        with pytest.raises(RuntimeError) as stale:
            await first.client.me()
        assert stale.value.code == "stale_owner" and p.calls == []
        assert (await second.client.me())["id"] == "bot"
        assert (await source.client.me())["id"] == "bot"
        assert p.calls == [("GET", "/users/@me", None), ("GET", "/users/@me", None)]
    finally:
        await stream.aclose()
        await a.close()
        await b.close()


@pytest.mark.parametrize("kind", ["raw_only", "diagnostic"])
async def test_raw_and_diagnostic_ingress_deliver_owner_bound_client(delivery, kind):
    p = delivery
    plugin = object()
    bus = p.instance.client.qq.events
    raw = bus.stream({"*"}, owner=plugin)
    typed = bus.stream({"GUILD_CREATE"} if kind == "raw_only" else {"GROUP_MEMBER_ADD"}, owner=object())
    iterator = raw.__aiter__()
    try:
        if kind == "raw_only":
            payload = {"op": 0, "id": "incomplete-guild", "t": "GUILD_CREATE", "d": {"name": "Unknown"}}
        else:
            payload = {"op": 0, "id": "bad-member", "t": "GROUP_MEMBER_ADD", "d": []}
        receipt = accept(p, payload)
        event = await asyncio.wait_for(anext(iterator), 2)
        assert typed.queue.empty() and event.client is not p.instance.client.qq
        assert event.client._options.owner is plugin and event.context.receipt == receipt
        assert not event.schema_valid and event.typed is None and event.raw()["id"] == payload["id"]
        assert event.diagnostic == ("invalid_event_data" if kind == "diagnostic" else None)
        if kind == "raw_only":
            assert event.schema_missing == ("id",)
        else:
            assert event.d == () and event.context.core_state == "invalid"
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        with pytest.raises(RuntimeError) as stale:
            await event.client.me()
        assert stale.value.code == "stale_owner" and p.calls == []
    finally:
        await iterator.aclose()
        await raw.close()
        await typed.close()


async def test_recovered_ingress_uses_subscription_owner_without_renewing_reply(delivery):
    p = delivery
    payload = {"op": 0, "id": "recovered-member", "s": 12, "t": "GROUP_MEMBER_ADD",
               "d": {"timestamp": int(NOW), "group_openid": "group-one", "member_openid": "user-one"}}
    plugin = object()
    old = p.instance.client.qq.events.stream({"*"}, owner=plugin, include_recovered=True)
    live_only = p.instance.client.qq.events.stream({"GROUP_MEMBER_ADD"}, owner=object())
    iterator = old.__aiter__()
    try:
        receipt = p.owner.inbox.accept(p.instance.identity.settings_key, RawEnvelope(payload, NOW))
        recovered = CoreConsumer(p.instance)
        assert recovered.step() and live_only.queue.empty()
        notice = await asyncio.wait_for(anext(iterator), 2)
        assert notice.context.receipt == receipt and notice.context.recovered
        assert notice.reply_context is None and notice.client._options.owner is plugin
        assert notice.typed is not None and notice.schema_valid
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        with pytest.raises(RuntimeError) as stale:
            await notice.client.me()
        assert stale.value.code == "stale_owner" and p.calls == []
        await recovered.close()
    finally:
        await iterator.aclose()
        await old.close()
        await live_only.close()


@pytest.mark.parametrize("outcome,name,op_id,state", [
    ("success", "guild_mute", "sent-before-unload", "succeeded"),
    ("partial", "group_kick", "partial-before-unload", "partial"),
    ("unknown", "guild_mute", "unknown-before-unload", "unknown"),
])
async def test_delivered_client_preserves_actual_inflight_write_outcome(delivery, outcome, name, op_id, state):
    p = delivery
    plugin = object()
    sub = p.instance.client.qq.events.stream({"GROUP_AT_MESSAGE_CREATE"}, owner=plugin)
    iterator = sub.__aiter__()
    p.hold_business[0] = True
    p.outcome[0] = outcome
    task = None
    try:
        accept(p, chat_payload(message_id="inflight-" + outcome))
        event = await asyncio.wait_for(anext(iterator), 2)
        def write(client):
            if name == "group_kick":
                return client.group_kick("g", ["u"], blacklist=True, operation_id=op_id)
            return client.guild_mute("g", "u", 60, operation_id=op_id)
        task = asyncio.create_task(write(event.client))
        await asyncio.wait_for(p.wire_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.wire_release.set()
        if outcome == "success":
            assert await task == {"state": "succeeded"}
        else:
            with pytest.raises(RuntimeError) as error:
                await task
            assert error.value.phase == ("partial" if outcome == "partial" else "result_unknown")
        assert p.owner.extension_state.operation(p.instance.identity.robot, op_id)["state"] == state
        writes = [(method, path) for method, path, _ in p.calls if method in {"POST", "PATCH", "DELETE"}]
        assert writes == [(("POST", "/v2/groups/g/batch_remove_members") if outcome == "partial"
                           else ("PATCH", "/guilds/g/members/u/mute"))]
        if outcome != "success":
            with pytest.raises(RuntimeError) as replay:
                await write(p.instance.client.qq)
            assert replay.value.phase == ("partial" if outcome == "partial" else "result_unknown")
            assert [(method, path) for method, path, _ in p.calls if method in {"POST", "PATCH", "DELETE"}] == writes
    finally:
        p.wire_release.set()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await iterator.aclose()
        await sub.close()


async def test_filtered_or_full_subscriptions_do_not_bind_extra_client():
    class CountingClient:
        def __init__(self):
            self.binds = []
        def with_options(self, *, owner):
            self.binds.append(owner)
            return self
    client = CountingClient()
    bus = EventBus(max_bytes=1024)
    context = EventContext("p", "app", "production", "generation", "websocket", (0, 1), "session", NOW, 1)
    source = NativeEvent({"op": 0, "id": "counted", "t": "GROUP_MEMBER_ADD", "s": 1,
        "d": {"timestamp": int(NOW), "group_openid": "g", "member_openid": "u"}}, context, client=client)
    bus.publish(source)  # No subscriptions: no serialization or owner binding.
    assert not client.binds
    filtered = bus.subscribe({"GROUP_MEMBER_ADD"}, owner=object(), capacity=1)
    try:
        bus.publish(NativeEvent({"op": 0, "t": "RESUMED", "d": ""}, context, client=client))
        assert filtered.queue.empty() and not client.binds
        bus.publish(source)
        assert client.binds == [filtered.owner] and filtered.queue.qsize() == 1
        bus.publish(source)
        assert filtered.closed and filtered.gap == "subscription_gap"
        assert client.binds == [filtered.owner] and bus.queued_bytes == 0
    finally:
        await filtered.close()
        await bus.close()


async def test_pure_bus_events_keep_none_client_and_bounded_queue():
    bus = EventBus(max_bytes=1024)
    owner = object()
    sub = bus.stream({"GROUP_MEMBER_ADD"}, owner=owner, include_recovered=True)
    context = EventContext("p", "app", "production", "generation", "websocket", (0, 1), "session", NOW, 1)
    source = NativeEvent({"op": 0, "id": "standalone", "t": "GROUP_MEMBER_ADD", "s": 1,
        "d": {"timestamp": int(NOW), "group_openid": "g", "member_openid": "u"}}, context)
    iterator = sub.__aiter__()
    try:
        bus.publish(source)
        size = len(json.dumps(source.raw(), ensure_ascii=False).encode())
        assert bus.queued_bytes == sub.bytes == size
        notice = await asyncio.wait_for(anext(iterator), 2)
        assert notice is source and notice.client is None
        assert bus.queued_bytes == sub.bytes == 0
    finally:
        await iterator.aclose()
        await sub.close()
        await bus.close()
