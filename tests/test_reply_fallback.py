"""Active fallback requires observed sources and a definite passive rejection."""
import pytest
from test_messaging_send import sending as sending
from test_streaming import chains
from test_streaming import streaming as streaming


async def test_sixth_passive_rejection_falls_back_once_and_later_sends_are_active(sending):
    s = sending
    chat, client = s.observe()
    for index in range(5):
        await client.send(chat.route, f"reply-{index}")
    s.modes.append(40034128)
    result = await client.send(chat.route, "tool result", operation_id="sixth")
    assert len(s.calls) == 7 and s.calls[5][1]["msg_seq"] == 6
    assert s.calls[6] == ("/v2/groups/group-one/messages", {"content": "tool result", "msg_type": 0})
    assert result["message_id"] == "real-format-7" and result["operation_id"] == "sixth" and result["msg_seq"] is None
    assert result["delivery"]["mode"] == "active"
    assert [a["state"] for a in result["delivery"]["attempts"]] == ["rejected", "sent"]
    assert s.store.operation(chat.route.robot, "sixth")["source"] == "msg-one"
    assert (await client.send(chat.route, "tool result", operation_id="sixth")) == result
    await client.send(chat.route, "next tool result")
    assert len(s.calls) == 8 and "msg_id" not in s.calls[-1][1]
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 5


async def test_expired_observed_pinned_event_can_send_active(sending):
    s = sending
    chat, client = s.observe()
    s.store.mark_delivered(chat)
    release = s.store.pin_delivery(chat)
    s.clock[0] += 301
    s.store.prune()
    try:
        result = await client.send(chat.route, "late tool result")
        assert result["state"] == "sent" and result["delivery"]["reason"]["code"] == "reply_window_expired"
        assert s.calls == [("/v2/groups/group-one/messages", {"content": "late tool result", "msg_type": 0})]
    finally:
        release()


async def test_native_first_frame_rejection_converts_only_before_any_output(streaming):
    s = streaming
    chat = s.observe()
    s.modes.append("event_expired")
    result = await s.core.send(chat.route, chains("first", "second"), source=chat.source, operation_id="first-frame")
    bodies = [body for _, body in s.calls]
    assert len(bodies) == 4 and bodies[0]["msg_id"] == "msg-one"
    assert all("msg_id" not in b and "event_id" not in b and "msg_seq" not in b for b in bodies[1:])
    assert [b["index"] for b in bodies] == [0, 0, 1, 2]
    assert all(b["stream_msg_id"] == "real-stream-id" for b in bodies[2:])
    assert result["state"] == "sent" and result["delivery"]["mode"] == "active"
    assert s.store.operation(chat.route.robot, "first-frame")["source"] == "msg-one"


@pytest.mark.parametrize("event,path", [("GROUP_AT_MESSAGE_CREATE", "/v2/groups/group-one/messages"),
    ("C2C_MESSAGE_CREATE", "/v2/users/user-one/messages"), ("AT_MESSAGE_CREATE", "/channels/group-one/messages"),
    ("DIRECT_MESSAGE_CREATE", "/dms/group-one/messages")])
@pytest.mark.parametrize("status,code", [(400, 304103), (400, 40034005), (400, 40034026), (400, 40034128), (200, 40034128)])
async def test_explicit_time_errors_use_same_target_and_one_active_attempt(sending, event, path, status, code):
    s = sending
    chat, client = s.observe(event)
    s.modes.append((status, code))
    result = await client.send(chat.route, "literal <&>", operation_id="scoped-fallback")
    assert [p for p, _ in s.calls] == [path, path]
    assert s.calls[0][1]["msg_id"] == "msg-one" and s.calls[1][1]["content"] == "literal &lt;&amp;&gt;"
    assert not {"msg_id", "event_id", "msg_seq"}.intersection(s.calls[1][1])
    assert result["delivery"]["reason"] == {"code": "passive_source_rejected", "business_code": code}
    assert [tuple(row) for row in s.store.db.execute("SELECT number,active FROM attempts ORDER BY number")] == [(1, 0), (2, 1)]


@pytest.mark.parametrize("mode,outcome", [(40034024, "rejected"), (40034025, "rejected"), (40034027, "rejected"),
    (304036, "rejected"), (40034100, "rejected"), ("429", "rejected"), ((429, 40034128), "rejected"), ((403, 40034128), "rejected"),
    ((500, 40034128), "unknown"), ((503, 40034005), "unknown"), ((408, 40034128), "unknown"),
    (50055001, "unknown"), (99999999, "rejected"), ("no-id", "unknown")])
async def test_non_time_or_uncertain_failure_never_falls_back(sending, mode, outcome):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    s.modes.append(mode)
    with pytest.raises(V2Error):
        await client.send(chat.route, "not duplicated", operation_id="one-failure")
    assert len(s.calls) == 1 and s.store.operation(chat.route.robot, "one-failure")["state"] == outcome
    blocked = s.store.db.execute("SELECT blocked FROM sources").fetchone()[0]
    assert not blocked or not blocked.startswith("active:")
    if mode in {40034024, 40034025, 40034027}:
        s.clock[0] += 301
        with pytest.raises(V2Error) as exc:
            await client.send(chat.route, "invalid stays invalid")
        assert exc.value.code == "reply_source_rejected" and len(s.calls) == 1


@pytest.mark.parametrize("active_failure,outcome", [(304036, "rejected"), (40034128, "rejected"), ("401", "rejected"), ("429", "rejected"), ("no-id", "unknown"), ("500", "unknown")])
async def test_active_failure_is_final_and_operation_never_replayed(sending, active_failure, outcome):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    s.modes[:] = [40034128, active_failure]
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "only twice", operation_id="final-failure")
    assert len(s.calls) == 2 and exc.value.operation_id == "final-failure"
    assert exc.value.details["delivery"]["attempts"][0]["state"] == "rejected"
    assert exc.value.details["delivery"]["attempts"][1]["state"] == outcome
    assert s.store.operation(chat.route.robot, "final-failure")["state"] == outcome
    with pytest.raises(V2Error) as reused:
        await client.send(chat.route, "only twice", operation_id="final-failure")
    assert reused.value.code == ("send_result_unknown" if outcome == "unknown" else "operation_already_attempted")
    assert len(s.calls) == 2 and s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0


@pytest.mark.parametrize("scope", ["unknown_source", "target", "scene", "robot", "environment", "generation", "expired_target", "evicted_evidence"])
async def test_expiry_does_not_turn_unobserved_or_foreign_sources_into_active_sends(sending, scope):
    from dataclasses import replace

    from v2.errors import V2Error
    from v2.models import RobotKey
    s = sending
    chat, _ = s.observe()
    source, route = chat.source, chat.route
    if scope == "unknown_source":
        source = replace(source, message_id="never-observed", sent_at=source.sent_at - 500)
    elif scope == "target":
        route = replace(route, target="other-target")
    elif scope == "scene":
        route = replace(route, scene="c2c")
    elif scope in {"robot", "environment"}:
        route = replace(route, robot=RobotKey("other-app" if scope == "robot" else route.robot.appid, "sandbox" if scope == "environment" else "production"))
    elif scope == "generation":
        source = replace(source, generation="another-instance-generation")
    elif scope == "expired_target":
        s.clock[0] += 86401
    else:
        s.clock[0] += 301
        s.store.prune()
    with pytest.raises(V2Error):
        await s.core.send(route, "must not send", source=source)
    assert not s.calls


async def test_concurrent_same_operation_and_unrelated_charge_survive_fallback(sending):
    import asyncio

    from v2.errors import V2Error
    from v2.messaging.store import robot_key
    s = sending
    chat, client = s.observe()
    other = s.store.reserve(chat.route, chat.source, "other-digest", "other")
    with s.store.transaction():
        s.store.db.execute("INSERT INTO charges VALUES(?,?,?,?,?)", (robot_key(chat.route.robot), "other", "legacy", "same-source", s.clock[0] + 300))
    s.modes[:] = [40034128, "wait"]
    task = asyncio.create_task(client.send(chat.route, "concurrent", operation_id="shared"))
    try:
        await asyncio.wait_for(s.entered.wait(), 2)
        with pytest.raises(V2Error) as exc:
            await client.send(chat.route, "concurrent", operation_id="shared")
        assert exc.value.code == "operation_already_attempted" and len(s.calls) == 2
        assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 1
        assert s.store.db.execute("SELECT op_id FROM charges").fetchone()[0] == other["op_id"]
        s.release.set()
        result = await task
        assert await client.send(chat.route, "concurrent", operation_id="shared") == result and len(s.calls) == 2
    finally:
        s.release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("wire_started,expected", [(False, "not_sent"), (True, "unknown")])
def test_restart_during_mode_conversion_preserves_fence_and_diagnostics(config, tmp_path, wire_started, expected):
    from test_messaging_state import NOW, chat_payload

    from v2.errors import V2Error
    from v2.messaging.convert import convert_chat
    from v2.messaging.reply import ReplyDelivery
    from v2.messaging.store import MessageStore
    from v2.models import InstanceKey
    from v2.protocol import RawEnvelope
    path = tmp_path / "restart-state"
    store = MessageStore(path, clock=lambda: NOW)
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(chat_payload(), NOW))
    store.observe(chat)
    operation = store.reserve(chat.route, chat.source, "digest", "interrupted", allow_active=True)
    delivery = ReplyDelivery(store, chat.route, chat.source, operation)
    delivery.before_send()
    store.block_source(chat.route, chat.source, 40034128, allow_active=True)
    delivery.switch({"code": "passive_source_rejected", "business_code": 40034128}, outcome="rejected")
    if wire_started:
        delivery.before_send()
    store.close()
    restored = MessageStore(path, clock=lambda: NOW)
    try:
        row = restored.operation(chat.route.robot, "interrupted")
        assert row["state"] == expected and row["source"] == "msg-one" and row["seq"] is None
        assert [a["state"] for a in row["error"]["details"]["delivery"]["attempts"]] == ["rejected", expected]
        with pytest.raises(V2Error):
            restored.reserve(chat.route, chat.source, "digest", "interrupted", allow_active=True)
        assert restored.db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert restored.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    finally:
        restored.close()


async def test_onebot_context_uses_active_fallback_but_expired_ticket_is_not_revived(sending):
    from test_onebot_network import listener
    s = sending
    chat, _ = s.observe()
    async with listener(s) as n:
        key = n.server.events.context(chat.source)
        s.modes.append(40034128)
        for index in range(2):
            async with n.http.post(n.base + "/send_group_msg", headers=n.headers, json={"group_id": "group-one", "message": "reply", "_qq_reply_context": key, "_qq_operation_id": f"network-{index}"}) as r:
                data = await r.json()
                assert data["status"] == "ok" and data["data"]["delivery"]["mode"] == "active"
        assert len(s.calls) == 3 and all("msg_id" not in b for _, b in s.calls[1:])
        s.clock[0] = chat.source.expires + 1
        async with n.http.post(n.base + "/send_group_msg", headers=n.headers, json={"group_id": "group-one", "message": "expired", "_qq_reply_context": key}) as r:
            assert (await r.json())["code"] == "reply_context_unavailable"
        assert len(s.calls) == 3


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
async def test_stream_uses_active_for_initial_expiry_and_aggregation(streaming, event):
    s = streaming
    chat = s.observe(event)
    s.store.mark_delivered(chat)
    s.clock[0] = chat.source.expires + 1
    result = await s.core.send(chat.route, chains("early", " late"), source=chat.source)
    assert result["state"] == "sent" and result["delivery"]["mode"] == "active"
    assert all(not {"msg_id", "event_id", "msg_seq"}.intersection(b) for _, b in s.calls)


@pytest.mark.parametrize("converted", [False, True])
async def test_stream_rejection_after_visible_output_never_restarts(streaming, converted):
    from v2.errors import V2Error
    s = streaming
    chat = s.observe()
    s.modes[:] = (["event_expired"] if converted else []) + ["ok", "event_expired"]
    with pytest.raises(V2Error) as exc:
        await s.core.send(chat.route, chains("visible", "tail"), source=chat.source, operation_id="partial")
    assert exc.value.phase == "result_unknown" and exc.value.details["partial_message_id"] == "real-stream-id"
    assert len(s.calls) == (3 if converted else 2) and [b["index"] for _, b in s.calls] == ([0, 0, 1] if converted else [0, 1])
    assert s.calls[0][1]["msg_id"] == "msg-one"
    if converted:
        assert all("msg_id" not in b for _, b in s.calls[1:])
    else:
        assert s.calls[1][1]["msg_id"] == "msg-one"
    assert s.store.operation(chat.route.robot, "partial")["state"] == "unknown"


@pytest.mark.parametrize("phase,status,allowed", [("rejected", 400, True), ("result_unknown", 500, False)])
async def test_legacy_block_requires_retained_definite_rejection_and_old_failure_stays_final(sending, phase, status, allowed):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    s.store.reserve(chat.route, chat.source, "old-digest", "legacy-failure")
    s.store.block_source(chat.route, chat.source, 40034128)
    s.store.finish(chat.route.robot, "legacy-failure", "rejected" if allowed else "unknown",
        error={"code": "qq_api_error", "phase": phase, "http_status": status, "business_code": 40034128})
    if allowed:
        result = await client.send(chat.route, "new logical send")
        assert result["delivery"]["mode"] == "active" and len(s.calls) == 1 and "msg_id" not in s.calls[0][1]
    else:
        with pytest.raises(V2Error) as exc:
            await client.send(chat.route, "do not trust old blocked flag")
        assert exc.value.code == "reply_source_rejected" and not s.calls
    with pytest.raises(V2Error):
        s.store.reserve(chat.route, chat.source, "old-digest", "legacy-failure", allow_active=True)


async def test_untrusted_earlier_source_timestamp_cannot_force_active_mode(sending):
    from dataclasses import replace

    from v2.errors import V2Error
    s = sending
    chat, _ = s.observe()
    source = replace(chat.source, sent_at=chat.source.sent_at - 500)
    with pytest.raises(V2Error) as exc:
        await s.core.send(chat.route, "no fabricated expiry", source=source)
    assert exc.value.code == "reply_expired" and not s.calls


async def test_active_preflight_failure_is_not_unknown_and_keeps_two_attempt_diagnostics(sending, monkeypatch):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    original = s.store.switch_active
    def switch(*args, **kwargs):
        s.clock[0] += 86401
        return original(*args, **kwargs)
    monkeypatch.setattr(s.store, "switch_active", switch)
    s.modes.append(40034128)
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "expired target", operation_id="preflight")
    assert exc.value.code == "identity_not_observed" and exc.value.phase == "not_sent"
    assert len(s.calls) == 1 and s.store.operation(chat.route.robot, "preflight")["state"] == "not_sent"
    saved = s.store.operation(chat.route.robot, "preflight")["error"]["details"]["delivery"]
    assert [a["state"] for a in saved["attempts"]] == ["rejected", "not_sent"]
    assert s.store.db.execute("SELECT used FROM sources").fetchone()[0] == 0


@pytest.mark.parametrize("stage", ["passive", "active"])
@pytest.mark.parametrize("cancel", [False, True])
async def test_timeout_or_cancellation_after_wire_never_replays(sending, stage, cancel):
    import asyncio

    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    s.modes[:] = ([40034128] if stage == "active" else []) + ["wait"]
    s.http.timeout = 0.05 if not cancel else 10
    task = asyncio.create_task(client.send(chat.route, "uncertain", operation_id="uncertain"))
    try:
        await asyncio.wait_for(s.entered.wait(), 2)
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else V2Error):
            await task
        assert len(s.calls) == (2 if stage == "active" else 1)
        row = s.store.operation(chat.route.robot, "uncertain")
        assert row["state"] == "unknown" and row["error"]["details"]["delivery"]["attempts"][-1]["state"] == "unknown"
        with pytest.raises(V2Error) as exc:
            await client.send(chat.route, "uncertain", operation_id="uncertain")
        assert exc.value.code == "send_result_unknown"
    finally:
        s.release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_event_source_drops_event_id_and_sequence_on_active_conversion(sending):
    from v2.extensions.events import EventReplySource
    s = sending
    chat, _ = s.observe()
    source = EventReplySource(chat.route, s.core.identity.generation, "real-event", "interaction", s.clock[0], s.clock[0])
    s.store.register_event_source(source)
    s.modes.append(40034026)
    result = await s.core.send(chat.route, "event reply", source=source)
    assert s.calls[0][1]["event_id"] == "real-event" and s.calls[0][1]["msg_seq"] == 1
    assert not {"msg_id", "event_id", "msg_seq"}.intersection(s.calls[1][1])
    assert result["delivery"]["reason"]["business_code"] == 40034026


@pytest.mark.parametrize("stage", ["valid", "blocked", "expired"])
async def test_typing_never_uses_active_fallback(sending, stage):
    from v2.errors import V2Error
    from v2.messaging.typing import TypingCore
    s = sending
    chat, _ = s.observe("C2C_MESSAGE_CREATE")
    typing = TypingCore(s.core, settings=lambda: {"typing_enabled": True})
    if stage == "blocked":
        s.store.block_source(chat.route, chat.source, 40034128, allow_active=True)
    elif stage == "expired":
        s.clock[0] += 3601
    else:
        s.modes.append((400, 40034128))
    try:
        with pytest.raises(V2Error):
            await typing.start(chat.route, chat.source)
        assert len(s.calls) == (1 if stage == "valid" else 0)
        assert all(b["msg_type"] == 6 and b["msg_id"] == "msg-one" for _, b in s.calls)
    finally:
        await typing.close()
    assert not typing.jobs


async def test_time_error_first_frame_fences_are_mode_specific_and_rate_retry_remains_bounded(streaming):
    import json
    s = streaming
    chat = s.observe()
    s.modes[:] = ["event_expired", "429", "ok"]
    result = await s.core.send(chat.route, chains("frame"), source=chat.source, operation_id="frame-modes")
    assert result["state"] == "sent" and len(s.calls) == 4
    rows = s.store.db.execute("SELECT op_id,state,context FROM extension_ops WHERE kind='stream_frame'").fetchall()
    assert len({row["op_id"] for row in rows}) == 4
    scopes = [(row["state"], json.loads(row["context"])["delivery_mode"]) for row in rows]
    assert scopes.count(("rejected", "passive")) == 1 and scopes.count(("rejected", "active")) == 1
    assert scopes.count(("succeeded", "active")) == 2


@pytest.mark.parametrize("mode,state", [((500, 40034128), "unknown"), ((408, 40034005), "unknown"), ((400, 40034024), "rejected"), ("missing", "unknown")])
async def test_native_first_frame_uncertain_or_invalid_never_converts(streaming, mode, state):
    from v2.errors import V2Error
    s = streaming
    chat = s.observe()
    s.modes.append(mode)
    with pytest.raises(V2Error):
        await s.core.send(chat.route, chains("not repeated"), source=chat.source, operation_id="no-conversion")
    assert len(s.calls) == 1
    assert s.store.operation(chat.route.robot, "no-conversion")["state"] == state
    assert s.store.db.execute("SELECT state FROM extension_ops WHERE kind='stream_frame'").fetchone()[0] == state


async def test_native_local_expiry_while_waiting_token_converts_before_first_wire(streaming, monkeypatch):
    s = streaming
    chat = s.observe()
    original = s.http.token
    async def token(**kwargs):
        s.clock[0] += 3601
        return await original(**kwargs)
    monkeypatch.setattr(s.http, "token", token)
    result = await s.core.send(chat.route, chains("late output"), source=chat.source)
    assert result["state"] == "sent" and len(s.calls) == 2
    assert all("msg_id" not in body and "msg_seq" not in body for _, body in s.calls)
    assert [a["state"] for a in result["delivery"]["attempts"]] == ["not_sent", "sent"]


async def test_native_converted_stream_keeps_later_auth_retry_on_same_frame(streaming):
    s = streaming
    chat = s.observe()
    s.modes[:] = ["event_expired", "ok", "401", "ok"]
    result = await s.core.send(chat.route, chains("first", "second"), source=chat.source)
    assert result["state"] == "sent" and len(s.calls) == 5
    assert s.calls[2][1] == s.calls[3][1]
    assert s.calls[2][1]["index"] == 1 and s.calls[2][1]["stream_msg_id"] == "real-stream-id"


@pytest.mark.parametrize("event", ["GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"])
async def test_aggregate_handles_source_blocked_by_another_operation(streaming, event):
    s = streaming
    chat = s.observe(event)
    s.store.block_source(chat.route, chat.source, 40034128, allow_active=True)
    result = await s.core.send(chat.route, chains("first", " second"), source=chat.source)
    assert result["mode"] == "aggregate" and result["delivery"]["mode"] == "active"
    assert len(s.calls) == 1 and s.calls[0][1]["content"] == "first second"
    assert "msg_id" not in s.calls[0][1]


@pytest.mark.parametrize("event,code", [("GROUP_AT_MESSAGE_CREATE", "transport_not_ready"), ("AT_MESSAGE_CREATE", "channel_ws_required"), ("DIRECT_MESSAGE_CREATE", "channel_ws_required")])
async def test_active_fallback_does_not_bypass_transport_readiness(sending, event, code):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe(event)
    s.store.block_source(chat.route, chat.source, 40034128, allow_active=True)
    s.core.is_online = s.core.ws_online = lambda: False
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "not online")
    assert exc.value.code == code and not s.calls


async def test_compacted_fallback_result_cannot_restart_operation(sending):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    s.store.operation_capacity = 1
    s.modes.append(40034128)
    await client.send(chat.route, "old", operation_id="compacted")
    s.clock[0] += 61
    await client.send(chat.route, "new", operation_id="later")
    with pytest.raises(V2Error) as status:
        s.store.operation(chat.route.robot, "compacted")
    assert status.value.code == "operation_history_evicted"
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "old", operation_id="compacted")
    assert exc.value.code == "operation_history_evicted" and len(s.calls) == 3


@pytest.mark.parametrize("revoked", [False, True])
async def test_owned_keyboard_is_preserved_and_rechecked_before_active_attempt(sending, monkeypatch, revoked):
    from types import SimpleNamespace

    from v2.errors import V2Error
    from v2.extensions.keyboard import OwnedKeyboard
    s = sending
    chat, client = s.observe()
    settings = {"applied_revision": 1}
    service = SimpleNamespace(adapter=SimpleNamespace(identity=s.core.identity), settings=lambda: settings)
    body = {"content": {"rows": [{"buttons": [{"id": "prefill", "render_data": {"label": "help", "style": 0},
        "action": {"type": 2, "data": "/help", "enter": False, "permission": {"type": 0, "specify_user_ids": ["user-one"]}}}]}]}}
    keyboard = OwnedKeyboard(service, chat.route, 1, s.core.identity.generation, body, [])
    original = s.store.block_source
    def block(*args, **kwargs):
        original(*args, **kwargs)
        if revoked:
            settings["applied_revision"] = 2
    monkeypatch.setattr(s.store, "block_source", block)
    s.modes.append(40034128)
    if revoked:
        with pytest.raises(V2Error) as exc:
            await client.send(chat.route, "card", markdown=True, keyboard=keyboard)
        assert exc.value.code == "keyboard_scope_mismatch" and exc.value.phase == "not_sent"
        assert len(s.calls) == 1
    else:
        await client.send(chat.route, "card", markdown=True, keyboard=keyboard)
        assert len(s.calls) == 2 and all(b["keyboard"] == body and b["markdown"] == {"content": "card"} for _, b in s.calls)
        assert "msg_id" not in s.calls[1][1]


@pytest.mark.parametrize("release_first", [False, True])
async def test_existing_live_delivery_pin_is_bounded_expiry_evidence_without_holding_source_capacity(sending, release_first):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    release = s.store.pin_delivery(chat)
    s.clock[0] += 301
    s.store.prune()
    assert s.store.db.execute("SELECT count(*) FROM sources").fetchone()[0] == 0
    assert s.store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
    try:
        if release_first:
            release()
            with pytest.raises(V2Error) as exc:
                await client.send(chat.route, "evidence evicted")
            assert exc.value.code == "reply_source_unavailable" and not s.calls
        else:
            result = await client.send(chat.route, "live pipeline")
            assert result["state"] == "sent" and result["delivery"]["reason"]["code"] == "reply_window_expired"
            assert len(s.calls) == 1 and "msg_id" not in s.calls[0][1]
    finally:
        release()


async def test_live_pin_does_not_forget_invalid_source_when_history_expires(sending):
    from v2.errors import V2Error
    s = sending
    chat, client = s.observe()
    release = s.store.pin_delivery(chat)
    try:
        s.modes.append(40034024)
        with pytest.raises(V2Error):
            await client.send(chat.route, "invalid source")
        s.clock[0] += 86401
        s.observe(message_id="fresh-target-observation")
        s.store.prune()
        with pytest.raises(V2Error) as exc:
            await client.send(chat.route, "still invalid")
        assert exc.value.code == "reply_source_rejected" and len(s.calls) == 1
    finally:
        release()
    s.store.prune()
    with pytest.raises(V2Error) as exc:
        await client.send(chat.route, "evidence evicted")
    assert exc.value.code == "reply_source_unavailable" and len(s.calls) == 1
