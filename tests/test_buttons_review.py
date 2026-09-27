"""Public buttons preserve host access gates and independent interaction intake."""
import asyncio
from types import SimpleNamespace

from astrbot.core.message.components import Image, Json
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
from test_button_callbacks import callback_case as callback_case
from test_button_callbacks import card, settle
from test_extension_dispatch import dispatch as dispatch
from test_interactions import interaction
from test_lifecycle import plugin_module as plugin_module
from test_media_cards import KEYBOARD
from test_media_upload import media as media
from test_media_upload import sending_core
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW

from v2.messaging.cards import media_card


async def test_slow_callback_business_does_not_occupy_ack_intake(dispatch, monkeypatch):
    s = dispatch
    all_started, release, fresh_acked = asyncio.Event(), asyncio.Event(), asyncio.Event()
    started = 0
    original_acknowledge = s.service.acknowledge

    async def track_ack(key, event):
        result = await original_acknowledge(key, event)
        if event.interaction_id == "fresh-menu" and result:
            fresh_acked.set()
        return result

    async def slow_business(event, token):
        nonlocal started
        started += 1
        if started == 8:
            all_started.set()
        await release.wait()
        return "finished_unconfirmed"

    monkeypatch.setattr(s.service.callbacks, "dispatch", slow_business)
    monkeypatch.setattr(s.service, "acknowledge", track_ack)
    try:
        for index in range(8):
            assert s.service.accept(interaction(
                s.config, token="qv2cb.slow-fixture", interaction_id=f"slow-{index}",
                event_id=f"slow-envelope-{index}",
            ), NOW)
        await asyncio.wait_for(all_started.wait(), 2)
        assert len([path for method, path, _ in s.calls if method == "PUT"]) == 8
        assert s.service.accept(interaction(
            s.config, kind=12, interaction_id="fresh-menu", event_id="fresh-menu-envelope",
        ), NOW)
        await asyncio.wait_for(fresh_acked.wait(), 2)
        assert ("PUT", "/interactions/fresh-menu", {"code": 0}) in s.calls
    finally:
        release.set()
        await settle(s)


async def test_callback_obeys_host_session_allowlist(callback_case):
    s, event, star, seen = callback_case
    button = event.qq.callback_button(star.direct, label="执行", data="blocked")
    await event.send(MessageChain([Json(card(button))]))
    config = s.owner.context.get_config()
    config.setdefault("platform_settings", {}).update({
        "enable_id_white_list": True,
        "id_whitelist": ["another-group"],
        "wl_ignore_admin_on_group": False,
        "wl_ignore_admin_on_friend": False,
        "id_whitelist_log": False,
    })
    gate = WhitelistCheckStage()
    await gate.initialize(SimpleNamespace(astrbot_config=config))
    await gate.process(event)
    assert event.is_stopped()
    assert s.service.accept(interaction(s.config, token=button["action"]["data"]), NOW)
    await settle(s)
    assert not seen, "A real callback executed even though the host rejects this session"
    assert s.service.records()[0]["ack"] == "succeeded"
    assert s.service.records()[0]["business"] == "rejected"
    assert len([path for method, path, _ in s.calls if method == "POST"]) == 1


async def test_media_card_accepts_native_astrbot_image_component(media):
    core, chat = sending_core(media)
    component = media_card(Image.fromURL("https://assets.test/native-card.png"), KEYBOARD)
    assert type(component) is Json
    result = await core.send(chat.route, MessageChain([component]), source=chat.source)
    assert result["message_id"] == "actual-media-message"
    assert media.calls[-1] == ("/v2/groups/group-one/messages", {
        "msg_type": 7, "keyboard": KEYBOARD, "media": {"file_info": "actual-file-receipt"},
        "msg_id": "msg-one", "msg_seq": 1,
    })
