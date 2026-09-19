import asyncio
import builtins
import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest
from astrbot.core.platform.register import (
    platform_cls_map,
    register_platform_adapter,
    unregister_platform_adapters_by_module,
)
from astrbot.core.star.context import Context

from v2 import PLATFORM_TYPE, PLUGIN_NAME


@pytest.fixture
def plugin_module(monkeypatch):
    package = ModuleType("fixture_v2_plugin")
    package.__path__ = ["/plugin"]
    monkeypatch.setitem(sys.modules, package.__name__, package)
    original = builtins.__import__

    def guarded(name, globals=None, locals=None, fromlist=(), level=0):
        if (globals or {}).get("__name__", "").startswith(package.__name__):
            assert not any(part in name for part in ("qqofficial", "qqoffice_expand", "aiocqhttp", "qqbot_agent_sdk"))
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded)
    module = importlib.import_module(package.__name__ + ".main")
    assert PLATFORM_TYPE not in platform_cls_map
    yield module
    for name in list(sys.modules):
        if name.startswith(package.__name__ + "."):
            sys.modules.pop(name)
    from astrbot.core.star.star import star_map, star_registry
    from astrbot.core.star.star_handler import star_handlers_registry
    star_map.pop(module.__name__, None)
    star_registry[:] = [m for m in star_registry if m.module_path != module.__name__]
    for h in list(star_handlers_registry):
        if h.handler_module_path == module.__name__:
            star_handlers_registry.remove(h)


def context():
    value = {"platform": [], "platform_settings": {}}
    ctx = SimpleNamespace(registered_web_apis=[], get_config=lambda: value)
    ctx.register_web_api = lambda *args: Context.register_web_api(ctx, *args)
    from astrbot.core.platform.manager import PlatformManager
    ctx.platform_manager = PlatformManager(value, asyncio.Queue())
    return ctx


async def test_import_activation_and_init_failure_ownership(plugin_module):
    ctx = context()
    sentinel = object()
    ctx.registered_web_apis.append((f"/{PLUGIN_NAME}/config", sentinel, ["GET"], "another owner"))
    owner = plugin_module.QQOfficialV2(ctx, {})
    with pytest.raises(RuntimeError) as exc:
        await owner.initialize()
    assert exc.value.code == "route_conflict"
    assert owner.stopping and owner.store.closed and not owner.instances
    assert PLATFORM_TYPE not in platform_cls_map
    assert ctx.registered_web_apis == [(f"/{PLUGIN_NAME}/config", sentinel, ["GET"], "another owner")]
    await owner.terminate()


async def test_registration_collision_does_not_unregister_other_owner(plugin_module):
    class Other:
        pass
    register_platform_adapter(PLATFORM_TYPE, "fixture other")(Other)
    owner = plugin_module.QQOfficialV2(context(), {})
    try:
        with pytest.raises(ValueError):
            await owner.initialize()
        assert platform_cls_map[PLATFORM_TYPE] is Other and owner.store.closed
    finally:
        unregister_platform_adapters_by_module(Other.__module__)


async def test_constructor_failure_has_no_owned_instance(plugin_module, config):
    config["id"] = "constructor-failure-fixture"
    owner = plugin_module.QQOfficialV2(context(), {})
    await owner.initialize()
    try:
        with pytest.raises(RuntimeError):
            owner.adapter_class({**config, "secret": ""}, {}, asyncio.Queue())
        assert not owner.instances
        models = importlib.import_module(plugin_module.__package__ + ".v2.models")
        key = models.InstanceKey.from_config(config).settings_key
        owner.store.mutate(key, 0, "fixture", operation="save", patch={"title": "test"})
        owner.store.db.execute("UPDATE settings SET applied='broken' WHERE key=?", (key,))
        owner.store.db.commit()
        with pytest.raises(RuntimeError) as exc:
            owner.adapter_class(config, {}, asyncio.Queue())
        assert exc.value.code == "settings_corrupt" and not owner.instances
    finally:
        await owner.terminate()


@pytest.mark.parametrize("count", [1, 2, 10, 50, 100])
async def test_multi_instance_and_event_cleanup(plugin_module, config, count):
    from astrbot.core.message.message_event_result import MessageChain
    from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
    from astrbot.core.platform.message_session import MessageSession
    from astrbot.core.platform.message_type import MessageType
    ctx = context()
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    try:
        for index in range(count):
            cfg = {**config, "id": f"instance{index}", "appid": f"app{index}"}
            owner.adapter_class(cfg, {}, asyncio.Queue())
        assert len(owner.instances) == count
        instance = next(iter(owner.instances))
        with pytest.raises(RuntimeError) as exc:
            owner.adapter_class(dict(instance.config), {}, asyncio.Queue())
        assert exc.value.code == "duplicate_receiver"
        await asyncio.gather(*(asyncio.create_task(i.run()) for i in owner.instances), return_exceptions=True)
        module = importlib.import_module(plugin_module.__package__ + ".v2.models")
        route = module.SessionRoute(instance.identity.robot, "group", "group", "user")
        msg = AstrBotMessage()
        msg.type = MessageType.GROUP_MESSAGE
        msg.self_id = "observed-bot-id"
        msg.sender = MessageMember("user")
        msg.message_str = "fixture"
        msg.message = []
        msg.message_id = "actual-message"
        msg.group_id = "group"
        msg.session_id = route.encode()
        msg.raw_message = {"id": "outer-event", "d": {"id": "actual-message"}}
        event = instance.create_event(msg)
        assert event.bot.api is event.bot and event.qq is event.bot.qq
        assert event.get_platform_name() == PLATFORM_TYPE
        assert event.raw_data["id"] == "outer-event"
        message_chain = MessageChain()
        for operation in (event.send(message_chain), event.send_streaming(None), event.send_typing(),
                          instance.send_by_session(event.session, message_chain)):
            with pytest.raises(RuntimeError):
                await operation
        other_session = MessageSession("another", msg.type, route.encode())
        with pytest.raises(RuntimeError) as exc:
            await instance.send_by_session(other_session, message_chain)
        assert exc.value.code == "identity_mismatch"
        await owner.terminate()
        assert not owner.instances and owner.store.closed and not ctx.registered_web_apis
        with pytest.raises(RuntimeError) as exc:
            await event.bot.get_status()
        assert exc.value.code == "stale_generation"
        await owner.terminate()
    finally:
        await owner.terminate()
