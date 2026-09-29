"""Host session sends convert to passive once, only on an explicit 40034105."""
import asyncio
import base64
from types import SimpleNamespace

import pytest
from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.platform.platform_metadata import PlatformMetadata
from test_media_boundary import PNG
from test_media_upload import media as media
from test_messaging_send import sending as sending
from test_messaging_state import NOW, chat_payload
from test_interactions import interaction

from v2 import PLATFORM_TYPE
from v2.adapter import V2Adapter
from v2.errors import V2Error
from v2.media.types import MediaInput
from v2.messaging.convert import convert_chat
from v2.messaging.outbound import SendingCore
from v2.messaging.session_sources import ACTIVE_DENIED_CODE, SessionSendPolicy, SessionSourceIndex
from v2.models import InstanceKey
from v2.protocol import RawEnvelope


@pytest.fixture
def adapter(sending):
    instance = object.__new__(V2Adapter)
    instance.identity = sending.client.identity
    instance.client = sending.client
    instance.owner = SimpleNamespace(messages=sending.store)
    instance.session_sources = SessionSourceIndex(sending.client.identity, capacity=sending.store.source_capacity)
    instance.meta = lambda: PlatformMetadata(PLATFORM_TYPE, "test", instance.identity.platform_id)
    return instance


def group_session(session_id="group-one"):
    return MessageSession("test-v2", MessageType.GROUP_MESSAGE, session_id)


async def test_final_failure_summary_is_safe_and_success_never_logs_failure(sending, adapter, caplog, config):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    caplog.clear()
    with caplog.at_level("WARNING"):
        s.modes.append(ACTIVE_DENIED_CODE)
        await adapter.send_by_session(group_session(), MessageChain([Plain("secret-body-text")]))
    warnings = [r for r in caplog.records if r.getMessage().startswith("QQ V2 send failed")]
    assert not warnings, "a successful conversion must not log a failure"
    caplog.clear()
    with caplog.at_level("WARNING"):
        s.modes.append((400, 40034100))
        with pytest.raises(V2Error):
            await adapter.send_by_session(group_session(), MessageChain([Plain("secret-body-text")]))
    warnings = [r for r in caplog.records if r.getMessage().startswith("QQ V2 send failed")]
    assert len(warnings) == 1
    text = warnings[0].getMessage()
    assert "secret-body-text" not in text and config["secret"] not in text
    assert "'business_code': 40034100" in text and "'operation_id'" in text and "'phase': 'rejected'" in text


def last_operation(s):
    row = s.store.db.execute("SELECT op_id FROM operations ORDER BY rowid DESC LIMIT 1").fetchone()
    return s.store.operation(s.client.identity.robot, row[0])


async def test_command_reply_succeeds_passively_and_session_send_falls_back_once(sending, adapter):
    s = sending
    chat, bound = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    # A bound command reply keeps its passive-first contract and sequence.
    assert (await bound.send(chat.route, MessageChain([Plain("command reply")])))["msg_seq"] == 1
    s.modes.append(ACTIVE_DENIED_CODE)
    await adapter.send_by_session(group_session(), MessageChain([Plain("session reply")]))
    sent = [(path, body) for path, body in s.calls]
    assert len(sent) == 3
    assert sent[1][1] == {"content": "session reply", "msg_type": 0}
    assert sent[2][1] == {"content": "session reply", "msg_type": 0, "msg_id": "msg-one", "msg_seq": 2}
    operation = last_operation(s)
    result = operation["result"]
    assert operation["state"] == "sent" and result["msg_seq"] == 2 and result["state"] == "sent"
    delivery = result["delivery"]
    assert delivery["mode"] == "passive" and delivery["source_origin"] == {"kind": "session_index", "message_id": "msg-one"}
    assert delivery["reason"] == {"code": "active_source_rejected", "business_code": ACTIVE_DENIED_CODE}
    assert [(a["mode"], a["state"]) for a in delivery["attempts"]] == [("active", "rejected"), ("passive", "sent")]
    assert operation["source"] == "msg-one" and operation["seq"] == 2
    # The next session send starts active again; one conversion never becomes a loop.
    await adapter.send_by_session(group_session(), MessageChain([Plain("again")]))
    assert last_operation(s)["result"]["delivery"]["mode"] == "active"
    assert len(s.calls) == 4 and "msg_id" not in s.calls[-1][1]


async def test_fixed_candidate_is_not_replaced_by_a_newer_inbound_message(sending, adapter, monkeypatch):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    original = s.http.request
    seen = []
    async def hooked(spec, **kwargs):
        if spec.path.endswith("/messages") and not seen:
            seen.append(spec.path)
            newer, _ = s.observe(message_id="msg-two", text="newer")
            adapter.session_sources.record(newer, s.store.now())
            s.modes.append(ACTIVE_DENIED_CODE)
        return await original(spec, **kwargs)
    monkeypatch.setattr(s.http, "request", hooked)
    await adapter.send_by_session(group_session(), MessageChain([Plain("fixed")]))
    assert s.calls[-1][1]["msg_id"] == "msg-one" and s.calls[-1][1]["msg_seq"] == 1
    assert last_operation(s)["result"]["delivery"]["source_origin"]["message_id"] == "msg-one"


async def test_latest_inbound_candidate_is_used_when_the_send_starts_later(sending, adapter):
    s = sending
    first, _ = s.observe()
    adapter.session_sources.record(first, s.store.now())
    second, _ = s.observe(message_id="msg-two")
    adapter.session_sources.record(second, s.store.now())
    s.modes.append(ACTIVE_DENIED_CODE)
    await adapter.send_by_session(group_session(), MessageChain([Plain("latest")]))
    assert s.calls[-1][1]["msg_id"] == "msg-two"
    assert last_operation(s)["result"]["delivery"]["source_origin"]["message_id"] == "msg-two"


async def test_bound_event_session_uses_its_exact_source_not_the_index(sending, adapter):
    s = sending
    chat, _ = s.observe()
    event = adapter.create_event(chat.message)
    newer, _ = s.observe(message_id="msg-two")
    adapter.session_sources.record(newer, s.store.now())
    s.modes.append(ACTIVE_DENIED_CODE)
    await adapter.send_by_session(event.session, MessageChain([Plain("bound")]))
    assert s.calls[-1][1]["msg_id"] == "msg-one"
    operation = next(row for row in s.store.db.execute(
        "SELECT result FROM operations WHERE result LIKE '%event_session%'").fetchall())
    assert "msg-one" in operation[0]


async def test_expired_bound_source_never_borrows_the_index_candidate(sending, adapter):
    s = sending
    chat, _ = s.observe()
    event = adapter.create_event(chat.message)
    newer, _ = s.observe(message_id="msg-two")
    adapter.session_sources.record(newer, s.store.now())
    s.clock[0] += 301
    s.modes.append(ACTIVE_DENIED_CODE)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(event.session, MessageChain([Plain("expired bound")]))
    assert exc.value.business_code == ACTIVE_DENIED_CODE and len(s.calls) == 1
    assert exc.value.details["delivery"]["fallback_skipped"] == {"code": "reply_expired"}


async def test_no_candidate_keeps_one_rejection_with_an_explicit_reason(sending, adapter):
    s = sending
    chat, _ = s.observe()  # Observed target, but nothing entered the candidate index.
    s.modes.append(ACTIVE_DENIED_CODE)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(group_session(), MessageChain([Plain("no source")]))
    assert exc.value.business_code == ACTIVE_DENIED_CODE and exc.value.phase == "rejected"
    assert len(s.calls) == 1 and "msg_id" not in s.calls[0][1]
    assert exc.value.details["delivery"]["fallback_skipped"] == {"code": "no_session_source"}
    assert "source_origin" not in exc.value.details["delivery"]
    assert s.store.db.execute("SELECT source FROM operations").fetchone()[0] is None


@pytest.mark.parametrize("mode,phase,state", [
    ("500", "result_unknown", "unknown"), ("no-id", "result_unknown", "unknown"),
    ("ambiguous_429", "result_unknown", "unknown"), ((408, 50055001), "result_unknown", "unknown"),
    (40034100, "rejected", "rejected"), (50002, "rejected", "rejected"), (304023, "result_unknown", "unknown"),
])
async def test_ambiguous_or_other_rejections_never_convert(sending, adapter, mode, phase, state):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    s.modes.append(mode)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(group_session(), MessageChain([Plain("once")]))
    assert exc.value.business_code != ACTIVE_DENIED_CODE or exc.value.phase != "rejected"
    assert exc.value.phase == phase and len(s.calls) == 1
    operation = s.store.db.execute("SELECT state, result FROM operations WHERE state != 'sent'").fetchone()
    assert operation[0] == state and "converted" not in (operation[1] or "")
    assert tuple(s.store.db.execute("SELECT used, seq FROM sources").fetchone()) == (0, 0)


async def test_blocked_candidate_is_skipped_with_reason(sending, adapter):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    s.store.block_source(chat.route, chat.source, 40034128, allow_active=True)
    s.modes.append(ACTIVE_DENIED_CODE)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(group_session(), MessageChain([Plain("blocked")]))
    assert exc.value.business_code == ACTIVE_DENIED_CODE and len(s.calls) == 1
    assert exc.value.details["delivery"]["fallback_skipped"] == {"code": "reply_source_rejected"}


async def test_conversion_never_returns_to_active_after_a_passive_rejection(sending, adapter):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    s.modes.extend([ACTIVE_DENIED_CODE, 40034128])
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(group_session(), MessageChain([Plain("both denied")]))
    assert exc.value.business_code == 40034128 and len(s.calls) == 2
    delivery = exc.value.details["delivery"]
    assert [(a["mode"], a["state"]) for a in delivery["attempts"]] == [("active", "rejected"), ("passive", "rejected")]
    assert delivery["converted"] == "active_to_passive" and "fallback_skipped" not in delivery
    assert s.store.db.execute("SELECT blocked FROM sources").fetchone()[0] == "active:40034128"


async def test_isolated_sessions_and_other_groups_do_not_borrow_candidates(sending, adapter):
    s = sending
    chat, _ = s.observe()  # Non-isolated observation keys this candidate to (group, group-one, None).
    adapter.session_sources.record(chat, s.store.now())
    isolated = convert_chat(chat.identity, RawEnvelope(chat_payload(message_id="iso-one"), NOW), isolated=True)
    s.store.observe(isolated)
    other, _ = s.observe(target="group-two", message_id="other-one")
    s.modes.extend([ACTIVE_DENIED_CODE, ACTIVE_DENIED_CODE])
    for session, reason in ((MessageSession("test-v2", MessageType.GROUP_MESSAGE, "user-one_group-one"), "isolated"),
                            (MessageSession("test-v2", MessageType.GROUP_MESSAGE, "group-two"), "other group")):
        with pytest.raises(V2Error) as exc:
            await adapter.send_by_session(session, MessageChain([Plain("no borrow")]))
        assert exc.value.details["delivery"]["fallback_skipped"] == {"code": "no_session_source"}, reason
    assert len(s.calls) == 2 and all("msg_id" not in body for _, body in s.calls)


async def test_revoked_generation_loses_all_candidates(sending, adapter):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    assert adapter.session_sources.policy_for_route(chat.route).source is chat.source
    adapter.session_sources.revoke()
    assert adapter.session_sources.policy_for_route(chat.route) is None
    s.modes.append(ACTIVE_DENIED_CODE)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(group_session(), MessageChain([Plain("after revoke")]))
    assert exc.value.details["delivery"]["fallback_skipped"] == {"code": "no_session_source"}


async def test_own_send_results_never_become_candidates(sending, adapter):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    await adapter.send_by_session(group_session(), MessageChain([Plain("active ok")]))
    sent_ids = [row[0] for row in s.store.db.execute(
        "SELECT json_extract(result,'$.message_id') FROM operations WHERE json_extract(result,'$.message_id') IS NOT NULL")]
    assert sent_ids and all(mid != "msg-one" for mid in sent_ids)
    s.modes.append(ACTIVE_DENIED_CODE)
    await adapter.send_by_session(group_session(), MessageChain([Plain("reuse inbound")]))
    assert last_operation(s)["result"]["delivery"]["source_origin"]["message_id"] == "msg-one"


async def test_restart_does_not_rebuild_candidates_from_disk(config, tmp_path):
    from v2.messaging.store import MessageStore

    identity = InstanceKey.from_config(config)
    chat = convert_chat(identity, RawEnvelope(chat_payload(), NOW))
    store = MessageStore(tmp_path / "state", clock=lambda: NOW)
    try:
        store.observe(chat)
    finally:
        store.close()
    fresh = SessionSourceIndex(identity, capacity=store.source_capacity)
    assert fresh.policy_for_route(chat.route) is None


async def test_sequences_stay_unique_across_reply_fallback_and_native_sdk(sending, adapter):
    s = sending
    chat, bound = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    await bound.send(chat.route, MessageChain([Plain("passive one")]))
    assert s.store.claim_sequence(chat.route, "message", "msg-one", "native-op", lane="send", binding="digest-native") == 2
    s.modes.append(ACTIVE_DENIED_CODE)
    await adapter.send_by_session(group_session(), MessageChain([Plain("fallback three")]))
    assert s.calls[-1][1]["msg_seq"] == 3 and last_operation(s)["seq"] == 3
    assert s.store.claim_sequence(chat.route, "message", "msg-one", "native-two", lane="send", binding="digest-native-2") == 4


async def test_unknown_result_after_conversion_is_never_replayed(sending, adapter):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    s.modes.extend([ACTIVE_DENIED_CODE, "no-id"])
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(group_session(), MessageChain([Plain("unknown tail")]))
    assert exc.value.phase == "result_unknown" and len(s.calls) == 2
    operation_id = exc.value.operation_id
    assert s.store.operation(chat.route.robot, operation_id)["state"] == "unknown"
    digest = s.store.db.execute("SELECT digest FROM operations WHERE op_id=?", (operation_id,)).fetchone()[0]
    with pytest.raises(V2Error) as replay:
        s.store.reserve(chat.route, chat.source, digest, operation_id)
    assert replay.value.code == "send_result_unknown"
    assert s.store.db.execute("SELECT count(*) FROM operations WHERE state='unknown'").fetchone()[0] == 1


async def test_crash_between_conversion_and_wire_recovers_without_remaining_fallback(sending, adapter, monkeypatch):
    s = sending
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    holder = {}
    original_switch = s.store.switch_passive
    def switch(route, source, op_id, delivery):
        seq = original_switch(route, source, op_id, delivery)
        holder["op"] = op_id
        raise asyncio.CancelledError
    monkeypatch.setattr(s.store, "switch_passive", switch)
    s.modes.append(ACTIVE_DENIED_CODE)
    with pytest.raises(asyncio.CancelledError):
        await adapter.send_by_session(group_session(), MessageChain([Plain("crash window")]))
    operation = s.store.operation(chat.route.robot, holder["op"])
    assert operation["state"] == "unknown" and operation["seq"] == 1
    with pytest.raises(V2Error) as replay:
        s.store.reserve(chat.route, chat.source, operation["digest"], holder["op"])
    assert replay.value.code == "send_result_unknown"
    assert s.store.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 1


async def test_media_session_send_reuses_one_upload_across_the_conversion(media):
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), m.clock[0]))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, ws_online=lambda: True, media=m.service)
    try:
        await sender.send(chat.route, MessageChain([Plain("command image")]).use_markdown(False), source=chat.source,
                          operation_id="command-image")
        m.modes.append(ACTIVE_DENIED_CODE)
        result = await sender.send(chat.route, [MediaInput("image", "https://fixture.invalid/image.png")],
                                   session=SessionSendPolicy(chat.source, "session_index"), operation_id="session-image")
        sent = [body for path, body in m.calls if path.endswith("/messages")]
        assert len(sent) == 3
        assert sent[1]["media"] == {"file_info": "actual-file-receipt"} and "msg_id" not in sent[1]
        assert sent[2]["msg_id"] == "msg-one" and sent[2]["msg_seq"] == 2 and sent[2]["media"] == sent[1]["media"]
        assert len([p for p, _ in m.calls if p.endswith("/files")]) == 1
        assert result["state"] == "sent" and result["delivery"]["attempts"][0]["mode"] == "active"
        assert result["media"]["upload_operation_id"] is not None
    finally:
        await sender.close()


async def test_upload_failure_does_not_trigger_any_message_fallback(media):
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), m.clock[0]))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, ws_online=lambda: True, media=m.service)
    try:
        m.modes.append("files_http_500")
        with pytest.raises(V2Error) as exc:
            await sender.send(chat.route, [MediaInput("image", "https://fixture.invalid/image.png")],
                              session=SessionSendPolicy(chat.source, "session_index"), operation_id="upload-fail")
        assert exc.value.phase == "not_sent" and not any(p.endswith("/messages") for p, _ in m.calls)
    finally:
        await sender.close()


async def test_media_keeps_its_native_type_when_markdown_defaults_on(media):
    m = media
    chat = convert_chat(m.identity, RawEnvelope(chat_payload(), m.clock[0]))
    m.store.observe(chat)
    sender = SendingCore(m.identity, m.http, m.store, is_online=lambda: True, ws_online=lambda: True,
                         media=m.service, markdown_default=True)
    try:
        from astrbot.core.message.components import Image
        await sender.send(chat.route, MessageChain([Image.fromURL("https://fixture.invalid/image.png")]),
                          session=SessionSendPolicy(chat.source, "session_index"), operation_id="md-media")
        body = next(body for path, body in m.calls if path.endswith("/messages"))
        assert body["msg_type"] == 7 and "markdown" not in body and body["media"] == {"file_info": "actual-file-receipt"}
    finally:
        await sender.close()


async def test_public_client_send_rejects_the_internal_session_policy(sending):
    s = sending
    chat, _ = s.observe()
    with pytest.raises(V2Error) as exc:
        await s.client.send(chat.route, MessageChain([Plain("public API")]),
                            session=SessionSendPolicy(chat.source, "session_index"))
    assert exc.value.code == "invalid_params" and not s.calls


async def test_interaction_reply_source_is_not_a_chat_fallback_candidate(config):
    from v2.extensions.events import EventReplySource

    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(chat_payload(), NOW))
    projected = EventReplySource(chat.route, chat.identity.generation,
                                 "observed-event", "observed-interaction", NOW, NOW)
    index = SessionSourceIndex(chat.identity, capacity=10)
    assert index.policy_for_bound_session(chat.route, projected) is None
    assert index.policy_for_bound_session(chat.route, chat.source).source is chat.source


async def test_command_projection_session_sends_active_without_borrowing_the_index(sending, adapter, config):
    from v2.extensions.events import ExtensionEvent
    from v2.extensions.keyboard import TicketStore
    from v2.extensions.projection import CommandProjection

    s = sending
    adapter.session_isolated = False
    adapter.bot_id = "projection-bot"
    service = TicketStore(SimpleNamespace(identity=adapter.identity, owner=adapter.owner, check_generation=lambda: None),
                          catalog_provider=lambda route: None)
    event = ExtensionEvent.parse(adapter.identity, interaction(config), NOW)
    projection = CommandProjection(adapter, event, {"command": "/run", "handler": "fixture.command"}, service)
    assert projection.bot._source.event_id == "outer-event"
    # A real inbound chat exists for the same route; the bound interaction source must not use it.
    chat, _ = s.observe()
    adapter.session_sources.record(chat, s.store.now())
    await adapter.send_by_session(projection.session, MessageChain([Plain("projection active")]))
    assert len(s.calls) == 1 and s.calls[0][1] == {"content": "projection active", "msg_type": 0}
    s.modes.append(ACTIVE_DENIED_CODE)
    with pytest.raises(V2Error) as exc:
        await adapter.send_by_session(projection.session, MessageChain([Plain("projection denied")]))
    assert exc.value.business_code == ACTIVE_DENIED_CODE and exc.value.phase == "rejected"
    assert exc.value.details["delivery"]["fallback_skipped"] == {"code": "no_session_source"}
    assert len(s.calls) == 2 and "msg_id" not in s.calls[-1][1]
