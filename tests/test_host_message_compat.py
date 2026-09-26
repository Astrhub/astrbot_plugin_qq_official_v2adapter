"""V2 outline views leave real host message/configuration consumers untouched."""
import copy

import pytest
from astrbot.core.event_bus import EventBus
from astrbot.core.message.components import At, AtAll, Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.platform_metadata import PlatformMetadata
from test_lifecycle import context as host_context
from test_lifecycle import plugin_module as plugin_module
from test_messaging_send import sending as sending
from test_messaging_state import NOW, chat_payload
from test_messaging_wakeup import GROUP_BOT, READY_ID

from v2 import PLATFORM_TYPE, WEBHOOK_TYPE
from v2.client import V2Client
from v2.event import V2MessageEvent
from v2.messaging.convert import convert_chat
from v2.models import InstanceKey
from v2.protocol import RawEnvelope


def make_chat(config, *, own=GROUP_BOT, bot_id=READY_ID, text="hello", sender="user-one"):
    identity = InstanceKey.from_config(config)
    payload = chat_payload("GROUP_MESSAGE_CREATE", text=text, sender=sender)
    payload["d"]["mentions"] = [{"id": own, "is_you": True}] if own else []
    return convert_chat(identity, RawEnvelope(payload, NOW), bot_id=bot_id)


def make_event(chat, client=None, platform=PLATFORM_TYPE):
    return V2MessageEvent(chat.message, PlatformMetadata(platform, "QQ V2", id=chat.identity.platform_id),
                          client or V2Client(chat.identity), chat.route)


@pytest.mark.parametrize("platform", [PLATFORM_TYPE, WEBHOOK_TYPE])
@pytest.mark.parametrize("own", [GROUP_BOT, "000123"])
def test_outline_and_trace_are_read_only_views_using_event_local_self(config, monkeypatch, caplog, platform, own):
    text = f"literal [At:{own}]"
    chat = make_chat(config, own=own, text=text)
    message = chat.message
    message.message.extend([At(qq="other"), AtAll(), Image.fromURL("https://fixture.invalid/image.png"),
                            Reply(id="quote", sender_nickname="quoted", message_str="original quote")])
    chain, parts, raw = message.message, tuple(message.message), message.raw_message
    before = copy.deepcopy(vars(message))
    original = AstrMessageEvent.get_message_outline
    calls = []
    def outline(view):
        # Also checked during BaseEvent.__init__, before trace/span exist.
        assert message.message is chain and all(a is b for a, b in zip(chain, parts, strict=True))
        assert vars(message) == before and message.raw_message is raw
        calls.append(view)
        return original(view)
    monkeypatch.setattr(AstrMessageEvent, "get_message_outline", outline)
    event = make_event(chat, platform=platform)
    expected = f"literal [At:{own}] [At:other] [At:全体成员] [图片] [引用消息(quoted: original quote)]"
    assert event.trace.message_outline == expected and event.span is event.trace
    for _ in range(3):
        assert event.get_message_outline() == expected
    EventBus._print_event(None, event, "selected profile")
    logs = [r.getMessage() for r in caplog.records if getattr(r, "category", None) == "user_chat"]
    assert logs[-1].endswith(expected) and logs[-1].count(f"[At:{own}]") == 1
    assert len(calls) == 5 and all(v is not event and v.message_obj is not message for v in calls)
    assert event.message_obj is message and event.get_messages() is chain
    assert event.message_str == text and vars(message) == before
    assert event.raw_data == raw and event.bot._source is chat.source and event.route is chat.route
    assert [p.qq for p in chain if isinstance(p, At) and not isinstance(p, AtAll)] == [own, "other"]


@pytest.mark.parametrize("self_id,expected", [("", "[At:000123] [At:123] [At:全体成员]"),
    (None, "[At:000123] [At:123] [At:全体成员]"),
    ("000123", "[At:123] [At:全体成员]"), ("all", "[At:000123] [At:123] [At:全体成员]")])
def test_outline_exact_ids_unknown_self_and_at_all(config, self_id, expected):
    chat = make_chat(config, own=None, bot_id="", text="")
    first = At(qq="000123")
    first.qq = "000123"
    chat.message.message = [first, At(qq="123"), AtAll()]
    chat.message.self_id = self_id
    event = make_event(chat)
    assert event.get_message_outline() == event.trace.message_outline == expected
    assert len(event.get_messages()) == 3 and first.qq == "000123"


def test_outline_uses_public_host_api_without_requiring_outline_chain(config, monkeypatch):
    chat = make_chat(config, text="body")
    calls = []
    def public_outline(view):
        calls.append(tuple(view.get_messages()))
        return "/".join(p.text if isinstance(p, Plain) else f"mention:{p.qq}" for p in view.get_messages())
    monkeypatch.delattr(AstrMessageEvent, "_outline_chain")
    monkeypatch.setattr(AstrMessageEvent, "get_message_outline", public_outline)
    event = make_event(chat)
    assert event.trace.message_outline == event.get_message_outline() == "body"
    assert len(calls) == 2 and len(event.get_messages()) == 2


@pytest.mark.parametrize("name", ["AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"])
def test_other_scenes_use_real_self_and_do_not_clean_literal_text(config, name):
    identity = InstanceKey.from_config(config)
    text = f"<@{READY_ID}> literal [At:{READY_ID}]"
    payload = chat_payload(name, text=text)
    chat = convert_chat(identity, RawEnvelope(payload, NOW), bot_id=READY_ID)
    chain, raw = copy.deepcopy(chat.message.message), copy.deepcopy(chat.message.raw_message)
    event = make_event(chat)
    expected = text if name == "C2C_MESSAGE_CREATE" else f" literal [At:{READY_ID}]"
    assert event.trace.message_outline == event.get_message_outline() == expected
    assert event.get_messages() == chain and event.message_obj.raw_message == event.raw_data == raw
    assert event.message_str == chat.message.message_str and event.bot._source is chat.source


@pytest.fixture
def host_config():
    from astrbot.core.config.default import DEFAULT_CONFIG
    value = copy.deepcopy(DEFAULT_CONFIG)
    value["wake_prefix"] = ["#"]
    value["plugin_set"] = ["another-plugin"]
    value["platform_settings"].update(ignore_bot_self_message=False, ignore_at_all=True, empty_mention_waiting=True)
    return value


async def wake(event, host_config, monkeypatch):
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.waking_check import stage as waking
    from astrbot.core.star.star_handler import StarHandlerRegistry
    monkeypatch.setattr(waking, "star_handlers_registry", StarHandlerRegistry())
    stage = waking.WakingCheckStage()
    await stage.initialize(PipelineContext(host_config, None, "selected-profile"))
    await stage.process(event)
    return stage


@pytest.mark.parametrize("ignore", [False, True])
@pytest.mark.parametrize("sender", [GROUP_BOT, "user-one"])
async def test_ignore_bot_self_message_is_not_ignore_user_mention(config, host_config, monkeypatch, ignore, sender):
    host_config["platform_settings"]["ignore_bot_self_message"] = ignore
    event = make_event(make_chat(config, sender=sender))
    assert event.get_message_outline() == "hello"
    await wake(event, host_config, monkeypatch)
    assert event.is_wake is not (ignore and sender == GROUP_BOT)
    assert event.is_stopped() is (ignore and sender == GROUP_BOT)
    assert any(isinstance(p, At) and p.qq == GROUP_BOT for p in event.get_messages())


@pytest.mark.parametrize("ignore", [False, True])
async def test_host_at_all_policy_still_reads_real_chain(config, host_config, monkeypatch, ignore):
    host_config["platform_settings"]["ignore_at_all"] = ignore
    chat = make_chat(config, own=None)
    # Host/plugin-inserted AtAll stays visible; no unsupported QQ inbound mapping is invented.
    chat.message.message.append(AtAll())
    event = make_event(chat)
    assert event.get_message_outline() == "hello [At:全体成员]"
    await wake(event, host_config, monkeypatch)
    assert event.is_wake is not ignore and event.is_stopped() is ignore


@pytest.mark.parametrize("enabled", [False, True])
async def test_real_empty_mention_waiter_consumes_v2_chain(config, host_config, monkeypatch, enabled):
    import asyncio
    from types import SimpleNamespace

    from astrbot.builtin_stars.astrbot.main import Main
    from astrbot.core.utils.session_waiter import FILTERS, USER_SESSIONS, SessionWaiter
    host_config["platform_settings"].update(empty_mention_waiting=enabled, empty_mention_waiting_need_reply=False)
    queue, registered = asyncio.Queue(), asyncio.Event()
    builtin = Main(SimpleNamespace(get_config=lambda **kwargs: host_config, astrbot_config_mgr=None, get_event_queue=lambda: queue))
    chat = make_chat(config, text="")
    event = make_event(chat)
    await wake(event, host_config, monkeypatch)
    assert event.is_wake and event.trace.message_outline == "" and len(event.get_messages()) == 1
    original = SessionWaiter.register_wait
    async def register(self, *args, **kwargs):
        asyncio.get_running_loop().call_soon(registered.set)
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(SessionWaiter, "register_wait", register)
    async def consume():
        return [result async for result in builtin.handle_empty_mention(event)]
    before_filters, before_sessions = list(FILTERS), dict(USER_SESSIONS)
    task = asyncio.create_task(consume())
    try:
        if enabled:
            await asyncio.wait_for(registered.wait(), 2)
            waiter = USER_SESSIONS[event.unified_msg_origin]
            followup = make_event(make_chat(config, own=None, bot_id=GROUP_BOT, text="next message"))
            await builtin.handle_session_control_agent(followup)
            resumed = await asyncio.wait_for(queue.get(), 2)
            assert resumed is not followup and resumed.message_str == "next message"
            assert isinstance(resumed.get_messages()[0], At) and resumed.get_messages()[0].qq == GROUP_BOT
            assert resumed.get_message_outline() == "next message"
            # Release the host waiter's timer after its real completion, not a real-time sleep.
            waiter.session_controller.current_event.set()
        assert await asyncio.wait_for(task, 2) == []
        assert registered.is_set() is enabled
        assert FILTERS == before_filters and USER_SESSIONS == before_sessions
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("scene,mention,quote,segmented", [
    ("GROUP_MESSAGE_CREATE", False, False, False),
    ("GROUP_MESSAGE_CREATE", True, True, False),
    ("AT_MESSAGE_CREATE", True, True, False),
    ("C2C_MESSAGE_CREATE", True, True, False),
    ("DIRECT_MESSAGE_CREATE", True, True, False),
    ("GROUP_MESSAGE_CREATE", True, True, True),
    ("C2C_MESSAGE_CREATE", False, False, True),
])
async def test_real_result_decorate_and_respond_reach_v2_http(sending, host_config, monkeypatch, scene, mention, quote, segmented):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from astrbot.core.pipeline import context_utils
    from astrbot.core.pipeline.context import PipelineContext
    from astrbot.core.pipeline.respond.stage import RespondStage
    from astrbot.core.pipeline.result_decorate import stage as decorating
    from astrbot.core.star.star_handler import StarHandlerRegistry
    registry = StarHandlerRegistry()
    monkeypatch.setattr(decorating, "star_handlers_registry", registry)
    monkeypatch.setattr(context_utils, "star_handlers_registry", registry)
    chat, client = sending.observe(scene)
    event = make_event(chat, client)
    host_config["platform_settings"].update(reply_prefix="prefix:", reply_with_mention=mention, reply_with_quote=quote)
    host_config["platform_settings"]["segmented_reply"].update(enable=segmented, only_llm_result=False, interval="0,0")
    ctx = PipelineContext(host_config, SimpleNamespace(context=SimpleNamespace(get_using_tts_provider_async=AsyncMock(return_value=None))), "selected-profile")
    decorate, respond = decorating.ResultDecorateStage(), RespondStage()
    await decorate.initialize(ctx)
    await respond.initialize(ctx)
    event.set_result(event.plain_result("first。second！"))
    assert [part async for part in decorate.process(event)] == []
    decorated = copy.deepcopy(event.get_result().chain)
    has_mention = mention and chat.route.scene in {"group", "channel"}
    assert any(isinstance(p, At) for p in decorated) is has_mention
    assert any(isinstance(p, Reply) for p in decorated) is quote
    assert len([p for p in decorated if isinstance(p, Plain)]) == (2 if segmented else 1)
    await respond.process(event)
    path = {"group": "/v2/groups/group-one/messages", "c2c": "/v2/users/user-one/messages",
            "channel": "/channels/group-one/messages", "dm": "/dms/group-one/messages"}[chat.route.scene]
    assert [p for p, _ in sending.calls] == [path] * (2 if segmented else 1)
    for index, (_, body) in enumerate(sending.calls):
        expected = ("prefix:first。" if index == 0 else "second！") if segmented else "prefix:first。second！"
        if has_mention and index == 0:
            expected = '<qqbot-at-user id="user-one" />\n' + expected
        assert body["content"] == expected and body["msg_id"] == "msg-one"
        if quote and index == 0:
            assert body["message_reference"] == {"message_id": "REFIDX_msg-one" if chat.route.scene in {"group", "c2c"} else "msg-one"}
        else:
            assert "message_reference" not in body
    assert event._has_send_oper and event.get_result() is None


async def test_unsupported_group_at_all_remains_explicit(sending):
    from astrbot.core.message.message_event_result import MessageChain

    from v2.errors import V2Error
    chat, client = sending.observe()
    event = make_event(chat, client)
    with pytest.raises(V2Error) as exc:
        await event.send(MessageChain([AtAll(), Plain("not fabricated")]))
    assert exc.value.code == "unsupported" and not sending.calls and not event._has_send_oper


@pytest.mark.parametrize("isolated", [False, True])
async def test_default_unique_session_passes_through_real_platform_manager(plugin_module, config, sending, host_config, monkeypatch, isolated):
    import asyncio
    import importlib
    import time

    ctx = host_context()
    saved = ctx.get_config()
    saved["platform_settings"]["unique_session"] = isolated
    http = importlib.import_module(plugin_module.__package__ + ".v2.transport.http")
    monkeypatch.setattr(http.HTTPTransport, "_make_session", lambda self: sending.http._factory())
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    platform = {**config, "id": "default-isolation", "type": WEBHOOK_TYPE, "shard_mode": "auto"}
    platform.pop("shard", None)
    saved["platform"].append(platform)
    saved.save_config()
    try:
        await ctx.platform_manager.load_platform(platform)
        instance = next(iter(owner.instances))
        await asyncio.wait_for(instance.ready.wait(), 2)
        assert ctx.platform_manager.settings is saved["platform_settings"]
        assert instance.session_isolated is isolated
        payload = chat_payload("GROUP_MESSAGE_CREATE", message_id=f"default-isolation-{isolated}", timestamp=time.time(), text="#body")
        owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, time.time()))
        event = await asyncio.wait_for(ctx.platform_manager.event_queue.get(), 2)
        expected = "user-one_group-one" if isolated else "group-one"
        assert event.unified_msg_origin == "default-isolation:GroupMessage:" + expected
        # A profile-specific override must not undo the adapter's default-setting isolation.
        host_config["platform_settings"]["unique_session"] = not isolated
        await wake(event, host_config, monkeypatch)
        assert event.is_wake and event.message_str == "body"
        assert event.get_session_id() == event.message_obj.session_id == expected
        assert event.route.user == ("user-one" if isolated else None)
        event.cleanup_temporary_local_files()
    finally:
        await owner.terminate()
    assert not owner.instances and not ctx.platform_manager._platform_tasks
