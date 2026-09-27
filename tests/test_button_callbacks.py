"""Real host registrations, isolated ACKs and single-use callback execution."""
import asyncio
import copy
import functools
import importlib
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from aiohttp import web
from astrbot.api.event import filter
from astrbot.core.message.components import Json, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.base import Star
from astrbot.core.star.filter.permission import PermissionType
from astrbot.core.star.star import star_map, star_registry
from astrbot.core.star.star_handler import star_handlers_registry
from test_extension_dispatch import dispatch as dispatch
from test_interactions import interaction
from test_lifecycle import plugin_module as plugin_module
from test_media_boundary import PNG
from test_media_upload import media as media
from test_messaging_delivery import accept
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW
from test_transport_http import MappedSession, upstream


def test_equal_star_instances_get_distinct_callback_keys():
    from v2.extensions.callbacks import _instance_key
    class EqualStar:
        def __eq__(self, other):
            return isinstance(other, EqualStar)
        def __hash__(self):
            return 1
    first, second = EqualStar(), EqualStar()
    assert first == second
    assert _instance_key(first) == _instance_key(first)
    assert _instance_key(first) != _instance_key(second)

@pytest.fixture
async def callback_case(dispatch, plugin_module):
    s = dispatch
    module = importlib.import_module(plugin_module.__package__ + ".api")
    seen = []

    class ThirdParty(Star):
        @module.button_callback("confirm")
        async def confirm(self, event):
            seen.append((event.get_extra("qq_button_data"), event.message_str,
                         event.message_obj.message_id, event.call_llm, event.raw_data["t"]))
            yield event.plain_result("callback accepted")

        @module.button_callback("direct")
        async def direct(self, event):
            seen.append(event.get_extra("qq_button_data"))
            await event.send(MessageChain([Plain("direct response")]))


        @filter.permission_type(PermissionType.ADMIN)
        @filter.command("sensitive-callback-unrelated")
        async def unrelated(self, event):
            raise AssertionError("An unrelated handler must not see the callback")

        @module.button_callback("admin")
        @filter.permission_type(PermissionType.ADMIN)
        async def admin(self, event):
            seen.append("admin callback ran")
            return event.plain_result("admin")
    plugin = star_map[ThirdParty.__module__]
    plugin.name = "third-party-buttons"
    plugin.star_cls = ThirdParty(s.owner.context)
    plugin.activated = True
    handlers = [h for h in star_handlers_registry if h.handler_module_path == ThirdParty.__module__
                and h.handler_name in {"confirm", "direct", "unrelated", "admin"}]
    assert len(handlers) == 4
    for handler in handlers:
        handler.handler = functools.partial(handler.handler, plugin.star_cls)
        plugin.star_handler_full_names.append(handler.handler_full_name)

    key = s.instance.identity.settings_key
    current = s.owner.store.get(key)
    saved = s.owner.store.mutate(key, current["revision"], "fixture", operation="save", patch={"extensions": {"keyboard_enabled": True}})
    s.owner.store.mutate(key, saved["revision"], "fixture", operation="apply")
    accept(s.owner, s.instance)
    assert s.instance.consumer.step()
    event = s.instance._event_queue.get_nowait()
    try:
        yield s, event, plugin.star_cls, seen
    finally:
        event.cleanup_temporary_local_files()
        for handler in handlers:
            star_handlers_registry.remove(handler)
        star_map.pop(ThirdParty.__module__, None)
        star_registry[:] = [item for item in star_registry if item is not plugin]


def card(button):
    return {"msg_type": 2, "markdown": {"content": "Choose"},
            "keyboard": {"content": {"rows": [{"buttons": [button]}]}}}


async def settle(s):
    await asyncio.wait_for(asyncio.gather(*tuple(s.service.tasks)), 3)


async def test_callback_ack_precedes_handler_and_response_uses_outer_event(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.confirm, label="确认", data={"item_id": "42"})
    assert button["action"]["permission"] == {"type": 0, "specify_user_ids": ["user-one"]}
    await event.send(MessageChain([Json(card(button))]))
    token = button["action"]["data"]
    assert token.startswith("qv2cb.") and "42" not in token
    frame = interaction(s.config, token=token)
    assert s.service.accept(frame, NOW)
    await settle(s)
    assert seen == [({"item_id": "42"}, "", "", False, "INTERACTION_CREATE")]
    assert s.service.records()[0]["ack"] == "succeeded"
    assert s.service.records()[0]["business"] == "finished_unconfirmed"
    assert [(m, path) for m, path, _ in s.calls] == [
        ("POST", "/v2/groups/group-one/messages"), ("PUT", "/interactions/inner-interaction"),
        ("POST", "/v2/groups/group-one/messages")]
    assert s.calls[-1][2]["event_id"] == "outer-event" and "msg_id" not in s.calls[-1][2]
    assert [(method, url) for method, url, _ in s.instance.http.session.calls if "/messages" in url] == [
        ("POST", "https://api.bot.qq.com/v2/groups/group-one/messages"),
        ("POST", "https://api.bot.qq.com/v2/groups/group-one/messages")]
    assert [(method, url) for method, url, _ in s.instance.ack_http.session.calls] == [
        ("PUT", "https://api.bot.qq.com/interactions/inner-interaction")]
    assert s.service.accept(frame, NOW)
    await settle(s)
    assert len(seen) == 1 and len(s.calls) == 3


async def test_callback_rejects_actor_tampering_and_unpublished_ticket(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.confirm, label="确认", data="secret")
    frame = interaction(s.config, token=button["action"]["data"])
    s.service.accept(frame, NOW)
    await settle(s)
    assert s.service.records()[0]["business"] == "rejected" and not seen
    _, _, issued = s.service.callbacks._lookup(button["action"]["data"])
    plugin, _, _, fingerprint, instance_key = s.service.callbacks._handler(function=star.confirm)
    assert issued["plugin"] == plugin.name
    assert issued["binding"] == fingerprint
    assert issued["instance"] == instance_key
    await event.send(MessageChain([Json(card(button))]))
    other = interaction(s.config, token=button["action"]["data"], actor="other", interaction_id="second", event_id="outer-second")
    s.service.accept(other, NOW)
    await settle(s)
    assert s.service.records()[0]["business"] == "rejected" and not seen
    legitimate = interaction(s.config, token=button["action"]["data"], interaction_id="third", event_id="outer-third")
    s.service.accept(legitimate, NOW)
    await settle(s)
    assert seen[0][0] == "secret"


async def test_callback_disabled_plugin_and_wire_permission_mismatch(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.confirm, label="确认", audience="all")
    altered = copy.deepcopy(button)
    altered["action"]["permission"] = {"type": 0, "specify_user_ids": ["user-one"]}
    with pytest.raises(RuntimeError) as mismatch:
        await event.send(MessageChain([Json(card(altered))]))
    assert mismatch.value.code == "ticket_scope_mismatch"
    assert s.owner.messages.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    assert s.owner.messages.db.execute("SELECT used,published FROM callback_tickets").fetchone()[:] == (0, 0)
    await event.send(MessageChain([Json(card(button))]))
    plugin = star_map[star.__class__.__module__]
    plugin.activated = False
    frame = interaction(s.config, token=button["action"]["data"])
    s.service.accept(frame, NOW)
    await settle(s)
    assert not seen and s.service.records()[0]["business"] == "rejected"

async def test_all_audience_is_explicit_and_uses_the_actual_click_actor(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="共同执行", data={"scope": "all"}, audience="all")
    await event.send(MessageChain([Json(card(button))]))
    frame = interaction(s.config, token=button["action"]["data"], actor="another-user")
    s.service.accept(frame, NOW)
    await settle(s)
    assert seen == [{"scope": "all"}]
    assert s.service.records()[0]["business"] == "finished_unconfirmed"
    assert s.calls[-1][2]["event_id"] == "outer-event" and "msg_id" not in s.calls[-1][2]
    assert s.owner.messages.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 1

async def test_callback_token_is_namespaced_by_robot(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    cfg = {**s.config, "id": "other-platform", "appid": "other-robot"}
    s.owner.context.get_config()["platform"].append(cfg)
    other = s.owner.adapter_class(cfg, {}, asyncio.Queue())
    with pytest.raises(RuntimeError) as error:
        other.extensions.callbacks.validate(other.client.route_for("group", "group-one"), button["action"]["data"])
    assert error.value.code == "ticket_unavailable" and not seen




async def test_direct_coroutine_callback_and_stale_hot_reload(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行", data={"id": 1})
    await event.send(MessageChain([Json(card(button))]))
    frame = interaction(s.config, token=button["action"]["data"])
    s.service.accept(frame, NOW)
    await settle(s)
    assert seen == [{"id": 1}] and s.calls[-1][2]["event_id"] == "outer-event"
    another = event.qq.callback_button(star.confirm, label="确认")
    await event.send(MessageChain([Json(card(another))]))
    star_map[star.__class__.__module__].star_cls = star.__class__(s.owner.context)
    fresh = interaction(s.config, token=another["action"]["data"], interaction_id="reloaded", event_id="outer-reloaded")
    s.service.accept(fresh, NOW)
    await settle(s)
    assert s.service.records()[0]["business"] == "rejected" and len(seen) == 1

async def test_published_ticket_cannot_be_reused_in_parallel_message(callback_case):
    s, event, star, _ = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    item = MessageChain([Json(card(button))])
    s.modes[:] = ["wait"]
    task = asyncio.create_task(event.send(item))
    try:
        await asyncio.wait_for(s.entered.wait(), 2)
        with pytest.raises(RuntimeError) as error:
            await event.bot.send(event.route, item, operation_id="different-message")
        assert error.value.code == "ticket_already_published"
    finally:
        s.release.set()
        await task
    assert [path for _, path, _ in s.calls] == ["/v2/groups/group-one/messages"]

@pytest.mark.parametrize("auth_retry", [False, True])
async def test_callback_publication_failure_is_not_sent_and_releases_reply_charge(callback_case, monkeypatch, auth_retry):
    s, event, star, seen = callback_case
    store = s.owner.messages
    button = event.qq.callback_button(star.direct, label="执行")
    item = MessageChain([Json(card(button))])

    def fail_publication():
        store.db.execute("CREATE TEMP TRIGGER fail_callback_publication BEFORE UPDATE OF published ON callback_tickets "
                         "BEGIN SELECT RAISE(ABORT, 'publication fixture failure'); END")

    if auth_retry:
        original_exchange = s.instance.http._exchange

        async def exchange(*args, **kwargs):
            response = await original_exchange(*args, **kwargs)
            if response[0] == 401:
                fail_publication()
            return response

        monkeypatch.setattr(s.instance.http, "_exchange", exchange)
        s.modes[:] = ["auth_rejected"]
    else:
        fail_publication()

    with pytest.raises(RuntimeError) as error:
        await event.bot.send(event.route, item, operation_id="publication-failure")
    assert len(s.calls) == int(auth_retry)
    assert error.value.phase == "not_sent"
    operation = event.bot.qq.send_status("publication-failure")
    assert operation["state"] == "not_sent"
    assert operation["error"]["details"]["delivery"]["attempts"][-1]["wire_attempts"] == int(auth_retry)
    assert store.db.execute("SELECT used FROM sources").fetchone()[0] == 0
    assert store.db.execute("SELECT count(*) FROM callback_tickets").fetchone()[0] == 0
    assert not seen

    store.db.execute("DROP TRIGGER fail_callback_publication")
    fresh = event.qq.callback_button(star.direct, label="执行")
    result = await event.bot.send(event.route, MessageChain([Json(card(fresh))]), operation_id="publication-recovered")
    assert result["state"] == "sent" and len(s.calls) == int(auth_retry) + 1


async def test_same_operation_reuses_sent_callback_card_receipt_without_second_wire(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    item = MessageChain([Json(card(button))])
    first = await event.bot.send(event.route, item, operation_id="same-card")
    second = await event.bot.send(event.route, item, operation_id="same-card")
    assert first == second and first["message_id"] == "actual-event-reply"
    assert [path for _, path, _ in s.calls] == ["/v2/groups/group-one/messages"]
    with pytest.raises(RuntimeError) as error:
        await event.bot.send(event.route, item, operation_id="other-card")
    assert error.value.code == "ticket_already_published" and len(s.calls) == 1




@pytest.mark.parametrize("lifecycle", ["clicked", "expired", "pruned"])
async def test_sent_card_receipt_survives_callback_ticket_lifecycle(callback_case, lifecycle):
    s, event, star, seen = callback_case
    clock = [NOW]
    s.owner.messages.clock = lambda: clock[0]
    button = event.qq.callback_button(star.direct, label="执行", data="once")
    chain = MessageChain([Json(card(button))])
    first = await event.bot.send(event.route, chain, operation_id="retained-card")
    assert first["state"] == "sent"
    token = button["action"]["data"]
    if lifecycle == "clicked":
        assert s.service.accept(interaction(s.config, token=token), NOW)
        await settle(s)
        assert seen == ["once"]
    else:
        clock[0] += 121
        if lifecycle == "pruned":
            event.qq.callback_button(star.direct, label="other")
            with pytest.raises(RuntimeError) as missing:
                s.service.callbacks._lookup(token)
            assert missing.value.code == "ticket_unavailable"
    before = list(s.calls)
    assert await event.bot.send(event.route, chain, operation_id="retained-card") == first
    assert s.calls == before and seen == (["once"] if lifecycle == "clicked" else [])
    with pytest.raises(RuntimeError) as fresh:
        await event.bot.send(event.route, chain, operation_id="fresh-card")
    assert fresh.value.code == "ticket_unavailable" and s.calls == before
    assert s.owner.messages.db.execute("SELECT 1 FROM operations WHERE op_id='fresh-card'").fetchone() is None


async def test_consumed_card_receipt_rejects_changed_content_target_source_and_other_robot(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行", data="once")
    chain = MessageChain([Json(card(button))])
    result = await event.bot.send(event.route, chain, operation_id="bound-card")
    assert s.service.accept(interaction(s.config, token=button["action"]["data"]), NOW)
    await settle(s)
    assert seen == ["once"]
    before = list(s.calls)
    for change in ("markdown", "button"):
        payload = copy.deepcopy(card(button))
        if change == "markdown":
            payload["markdown"]["content"] = "Another message"
        else:
            payload["keyboard"]["content"]["rows"][0]["buttons"][0]["render_data"]["label"] = "别的按钮"
        with pytest.raises(RuntimeError) as conflict:
            await event.bot.send(event.route, MessageChain([Json(payload)]), operation_id="bound-card")
        assert conflict.value.code == "operation_conflict"
    other_route = event.bot.route_for("group", "another-group")
    malformed = copy.deepcopy(card(button))
    malformed["msg_id"] = "forged"
    with pytest.raises(RuntimeError) as invalid:
        await event.bot.send(event.route, MessageChain([Json(malformed)]), operation_id="bound-card")
    assert invalid.value.code == "invalid_message"
    with pytest.raises(RuntimeError) as wrong_target:
        await event.bot.send(other_route, chain, operation_id="bound-card")
    assert wrong_target.value.code == "operation_conflict"
    other_source = replace(event.bot._source, message_id="different-source")
    with pytest.raises(RuntimeError) as wrong_source:
        await s.instance.sender.send(event.route, chain, source=other_source, operation_id="bound-card")
    assert wrong_source.value.code == "operation_conflict"
    assert s.calls == before and event.bot.qq.send_status("bound-card")["result"] == result

    config = {**s.config, "id": "receipt-other-platform", "appid": "receipt-other-robot"}
    s.owner.context.get_config()["platform"].append(config)
    other = s.owner.adapter_class(config, {}, asyncio.Queue())
    other_route = other.client.route_for("group", "group-one")
    with pytest.raises(RuntimeError) as cross_robot:
        await other.client.send(other_route, chain, operation_id="bound-card")
    assert cross_robot.value.code == "ticket_unavailable"
    assert other.http.session is None and s.calls == before
    assert s.owner.messages.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 2

async def test_unknown_or_evicted_card_operation_cannot_replay_after_ticket_lifecycle(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    chain = MessageChain([Json(card(button))])
    s.modes[:] = ["wait"]
    sending = asyncio.create_task(event.bot.send(event.route, chain, operation_id="unknown-receipt"))
    try:
        await asyncio.wait_for(s.entered.wait(), 2)
        sending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sending
    finally:
        s.release.set()
    assert event.bot.qq.send_status("unknown-receipt")["state"] == "unknown"
    before = list(s.calls)
    with pytest.raises(RuntimeError) as unknown:
        await event.bot.send(event.route, chain, operation_id="unknown-receipt")
    assert unknown.value.code == "send_result_unknown" and s.calls == before and not seen

    new_button = event.qq.callback_button(star.direct, label="执行")
    new_chain = MessageChain([Json(card(new_button))])
    await event.bot.send(event.route, new_chain, operation_id="evicted-receipt")
    assert s.service.accept(interaction(s.config, token=new_button["action"]["data"]), NOW)
    await settle(s)
    with s.owner.messages.transaction():
        s.owner.messages.db.execute(
            "UPDATE operations SET state='history_evicted',result=NULL WHERE op_id='evicted-receipt'")
    before = list(s.calls)
    with pytest.raises(RuntimeError) as evicted:
        await event.bot.send(event.route, new_chain, operation_id="evicted-receipt")
    assert evicted.value.code == "operation_history_evicted" and s.calls == before


async def test_in_flight_card_operation_is_not_replayed(callback_case):
    s, event, star, _ = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    chain = MessageChain([Json(card(button))])
    s.modes[:] = ["wait"]
    sending = asyncio.create_task(event.bot.send(event.route, chain, operation_id="in-flight-receipt"))
    try:
        await asyncio.wait_for(s.entered.wait(), 2)
        before = list(s.calls)
        with pytest.raises(RuntimeError) as in_flight:
            await event.bot.send(event.route, chain, operation_id="in-flight-receipt")
        assert in_flight.value.code == "operation_already_attempted" and s.calls == before
    finally:
        s.release.set()
        await sending
    assert event.bot.qq.send_status("in-flight-receipt")["state"] == "sent"
    assert len(s.calls) == 1


async def test_media_callback_receipt_rechecks_owned_local_file_hash_before_reuse(callback_case, plugin_module, media, tmp_path):
    s, event, star, _ = callback_case
    api = importlib.import_module(plugin_module.__package__ + ".api")
    s.instance.http._factory = media.http._factory
    s.instance.media.transfer = type(s.instance.media.transfer)(
        s.owner.media_pool, session_factory=media.service.transfer.session_factory)
    file_path = tmp_path / "card-image.png"
    file_path.write_bytes(PNG)
    button = event.qq.callback_button(star.direct, label="执行")
    item = api.media_card(api.MediaInput("image", file_path.as_uri()),
                          {"content": {"rows": [{"buttons": [button]}]}})
    chain = MessageChain([item])
    first = await event.bot.send(event.route, chain, operation_id="file-card-receipt")
    assert first["state"] == "sent" and first["media"]["source"] == "bytes"
    before_calls, before_puts = list(media.calls), list(media.puts)
    clock = [NOW + 121]
    s.owner.messages.clock = lambda: clock[0]
    retained = await event.bot.send(event.route, chain, operation_id="file-card-receipt")
    assert retained["message_id"] == first["message_id"] and retained["operation_id"] == first["operation_id"]
    assert retained["media"] == {key: value for key, value in first["media"].items() if key != "raw_url"}
    assert "raw_url" not in retained["media"]
    assert media.calls == before_calls and media.puts == before_puts
    file_path.write_bytes(PNG + b"changed")
    with pytest.raises(RuntimeError) as conflict:
        await event.bot.send(event.route, chain, operation_id="file-card-receipt")
    assert conflict.value.code == "operation_conflict"
    assert media.calls == before_calls and media.puts == before_puts
    with pytest.raises(RuntimeError) as fresh:
        await event.bot.send(event.route, chain, operation_id="new-file-card")
    assert fresh.value.code == "ticket_unavailable"
    assert media.calls == before_calls and media.puts == before_puts


async def test_permission_filter_and_effective_plugin_config_are_checked_before_business(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.admin, label="审批")
    await event.send(MessageChain([Json(card(button))]))
    s.service.accept(interaction(s.config, token=button["action"]["data"]), NOW)
    await settle(s)
    assert not seen and s.service.records()[0]["business"] == "rejected"
    s.owner.context.get_config()["admins_id"] = ["user-one"]
    button = event.qq.callback_button(star.admin, label="审批")
    await event.send(MessageChain([Json(card(button))]))
    s.service.accept(interaction(s.config, token=button["action"]["data"], interaction_id="admin", event_id="admin-outer"), NOW)
    await settle(s)
    assert seen == ["admin callback ran"] and s.service.records()[0]["business"] == "finished_unconfirmed"
    button = event.qq.callback_button(star.direct, label="执行")
    await event.send(MessageChain([Json(card(button))]))
    s.owner.context.get_config()["plugin_set"] = ["different-plugin"]
    s.service.accept(interaction(s.config, token=button["action"]["data"], interaction_id="disabled", event_id="disabled-outer"), NOW)
    await settle(s)
    assert seen == ["admin callback ran"] and s.service.records()[0]["business"] == "rejected"


async def test_callback_different_target_and_aged_event_never_execute(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    await event.send(MessageChain([Json(card(button))]))
    s.service.accept(interaction(s.config, token=button["action"]["data"], target="other-group"), NOW)
    await settle(s)
    assert not seen and s.service.records()[0]["business"] == "rejected"
    s.owner.messages.clock = lambda: NOW + 4
    with pytest.raises(RuntimeError) as error:
        s.service.accept(interaction(s.config, token=button["action"]["data"], interaction_id="late", event_id="late-outer"), NOW)
    assert error.value.code == "interaction_expired" and not seen


async def test_expired_ticket_rejects_fresh_event_and_never_executes(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    await event.send(MessageChain([Json(card(button))]))
    s.owner.messages.clock = lambda: NOW + 121
    frame = interaction(s.config, token=button["action"]["data"], interaction_id="fresh-event", event_id="fresh-envelope")
    frame["d"]["timestamp"] = datetime.fromtimestamp(NOW + 121, UTC).isoformat()
    s.service.accept(frame, NOW + 121)
    await settle(s)
    assert s.service.records()[0]["ack"] == "succeeded"
    assert s.service.records()[0]["business"] == "rejected" and not seen


async def test_reconstructed_ticket_store_cannot_replay_consumed_business(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行", data="once")
    await event.send(MessageChain([Json(card(button))]))
    token = button["action"]["data"]
    s.service.accept(interaction(s.config, token=token), NOW)
    await settle(s)
    assert seen == ["once"]
    s.service.callbacks = type(s.service.callbacks)(s.instance)
    s.service.accept(interaction(s.config, token=token, interaction_id="different-click", event_id="different-envelope"), NOW)
    await settle(s)
    assert seen == ["once"] and s.service.records()[0]["business"] == "rejected"


async def test_applied_settings_change_invalidates_issued_callback(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行")
    await event.send(MessageChain([Json(card(button))]))
    key = s.instance.identity.settings_key
    value = s.owner.store.get(key)
    saved = s.owner.store.mutate(key, value["revision"], "fixture", operation="save", patch={"title": "changed"})
    s.owner.store.mutate(key, saved["revision"], "fixture", operation="apply")
    s.service.accept(interaction(s.config, token=button["action"]["data"]), NOW)
    await settle(s)
    assert not seen and s.service.records()[0]["business"] == "rejected"

async def test_keyboard_switch_disables_owned_callbacks_but_not_native_prefill_cards(callback_case):
    s, event, star, _ = callback_case
    key = s.instance.identity.settings_key
    value = s.owner.store.get(key)
    saved = s.owner.store.mutate(key, value["revision"], "fixture", operation="save", patch={"extensions": {"keyboard_enabled": False}})
    s.owner.store.mutate(key, saved["revision"], "fixture", operation="apply")
    with pytest.raises(RuntimeError) as error:
        event.qq.callback_button(star.direct, label="执行")
    assert error.value.code == "keyboard_disabled"
    payload = {"msg_type": 2, "markdown": {"content": "prefill"},
               "keyboard": {"content": {"rows": [{"buttons": [{
                   "id": "start", "render_data": {"label": "帮助", "style": 1},
                   "action": {"type": 2, "permission": {"type": 2}, "data": "/帮助", "enter": False},
               }]}]}}}
    await event.send(MessageChain([Json(payload)]))
    assert s.calls[-1][2]["keyboard"] == payload["keyboard"]



async def test_c2c_callback_uses_user_route_and_direct_result(callback_case):
    s, _, star, seen = callback_case
    accept(s.owner, s.instance, event="C2C_MESSAGE_CREATE", target="user-one", message_id="c2c-button")
    assert s.instance.consumer.step()
    event = s.instance._event_queue.get_nowait()
    calls = []
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "c2c-fixture", "expires_in": 7200})
        calls.append((request.method, request.path, await request.json()))
        return web.Response(status=204) if request.method == "PUT" else web.json_response({"id": "c2c-card"})
    try:
        async with upstream(handler) as base:
            s.instance.http._factory = lambda: MappedSession(base)
            s.instance.ack_http._factory = lambda: MappedSession(base)
            button = event.qq.callback_button(star.direct, label="执行", data="c2c")
            await event.send(MessageChain([Json(card(button))]))
            frame = interaction(s.config, token=button["action"]["data"])
            frame["d"].update(scene="c2c", chat_type=2, user_openid="user-one")
            frame["d"].pop("group_openid")
            frame["d"].pop("group_member_openid")
            s.service.accept(frame, NOW)
            await settle(s)
        assert seen == ["c2c"]
        assert [(method, path) for method, path, _ in calls] == [
            ("POST", "/v2/users/user-one/messages"), ("PUT", "/interactions/inner-interaction"),
            ("POST", "/v2/users/user-one/messages")]
        assert calls[-1][2]["event_id"] == "outer-event"
    finally:
        event.cleanup_temporary_local_files()


@pytest.mark.parametrize("response_mode", ["files_rejected", "send_unknown"])
async def test_media_callback_ticket_lifecycle_tracks_message_outcome(callback_case, plugin_module, response_mode):
    s, event, star, seen = callback_case
    api = importlib.import_module(plugin_module.__package__ + ".api")
    types = importlib.import_module(plugin_module.__package__ + ".v2.media.types")
    calls = []
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "media-fixture", "expires_in": 7200})
        calls.append((request.method, request.path, await request.json()))
        if request.path.endswith("/files"):
            if response_mode == "files_rejected":
                return web.json_response({"code": 850026}, status=400)
            return web.json_response({"file_info": "qq-owned", "ttl": 60})
        assert request.path == "/v2/groups/group-one/messages"
        return web.json_response({})
    async with upstream(handler) as base:
        s.instance.http._factory = lambda: MappedSession(base)
        button = event.qq.callback_button(star.direct, label="执行")
        image = types.MediaInput("image", "https://assets.test/photo.png")
        item = api.media_card(image, {"content": {"rows": [{"buttons": [button]}]}})
        with pytest.raises(RuntimeError) as error:
            await event.bot.send(event.route, MessageChain([item]), operation_id="media-card-op")
        if response_mode == "files_rejected":
            assert error.value.phase == "not_sent"
            assert [path for _, path, _ in calls] == ["/v2/groups/group-one/files"]
            assert s.owner.messages.db.execute("SELECT count(*) FROM callback_tickets").fetchone()[0] == 0
        else:
            assert error.value.phase == "result_unknown"
            assert [path for _, path, _ in calls] == ["/v2/groups/group-one/files", "/v2/groups/group-one/messages"]
            assert s.owner.messages.db.execute("SELECT published FROM callback_tickets").fetchone()[0] == 1
            with pytest.raises(RuntimeError) as unknown:
                await event.bot.send(event.route, MessageChain([item]), operation_id="media-card-op")
            assert unknown.value.code == "send_result_unknown"
            assert len(calls) == 2 and not seen
