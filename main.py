"""Register the independent QQ V2 prototype only while this plugin is active."""

import asyncio
import copy

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.register import (
    platform_cls_map,
    register_platform_adapter,
    unregister_platform_adapters_by_module,
)
from astrbot.core.star.filter.command import GreedyStr

from .v2 import PLATFORM_TYPE, PLUGIN_NAME
from .v2.adapter import DEFAULT_PLATFORM_CONFIG, V2Adapter
from .v2.connections import Connections
from .v2.extensions.state import ExtensionStore
from .v2.help import send_help
from .v2.media.io import BlobPool
from .v2.messaging.delivery import DeliverySlots
from .v2.messaging.store import MessageStore
from .v2.onboarding import Onboarding
from .v2.panels import PanelService
from .v2.settings import SettingsStore
from .v2.transport.inbox import RawInbox
from .v2.web_api import ControlAPI


class V2Only(filter.CustomFilter):
    def filter(self, event, cfg):
        return event.get_platform_name() == PLATFORM_TYPE


class V2Addressed(filter.CustomFilter):
    def filter(self, event, cfg):
        return event.get_platform_name() == PLATFORM_TYPE and event.raw_data.get("t") in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}

class QQOfficialV2(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.stopping = True
        self.instances = set()
        self.store = None
        self.control = None
        self.adapter_class = None
        self.inbox = None
        self.messages = None
        self.extension_state = None
        self.media_pool = None
        self.panels = None
        self.catalog_ready = False
        self.delivery_slots = DeliverySlots()
        self.connections = None
        self.onboarding = None
        self._termination = None

    async def initialize(self):
        self.stopping = False
        try:
            self.store = SettingsStore(StarTools.get_data_dir(PLUGIN_NAME) / "settings.sqlite3")
            self.inbox = RawInbox(StarTools.get_data_dir(PLUGIN_NAME) / "transport.sqlite3")
            self.messages = MessageStore(StarTools.get_data_dir(PLUGIN_NAME) / "messaging.sqlite3")
            self.extension_state = ExtensionStore(self.messages)
            self.media_pool = BlobPool(StarTools.get_data_dir(PLUGIN_NAME) / "media-spool")
            owner = self

            class OwnedAdapter(V2Adapter):
                pass

            OwnedAdapter.owner = owner
            self.adapter_class = OwnedAdapter
            register_platform_adapter(
                PLATFORM_TYPE, "QQ 官方 V2（收发与受控扩展）",
                default_config_tmpl=copy.deepcopy(DEFAULT_PLATFORM_CONFIG),
                adapter_display_name="QQ 官方 V2 · 原型",
                config_metadata={
                    "secret": {"description": "QQ AppSecret", "type": "string", "secret": True,
                               "hint": "仅保存在本体平台配置；启用平台将连接 QQ。"},
                    "appid": {"description": "QQ AppID", "type": "string"},
                },
                support_streaming_message=True,
            )(OwnedAdapter)
            self.control = ControlAPI(self)
            self.control.register()
            self.connections = Connections(self)
            self.connections.prepare_webhooks()
            self.onboarding = Onboarding(self)
            self.panels = PanelService(self)
            self.panels.start()
        except BaseException:
            await self.terminate()
            raise

    @filter.custom_filter(V2Only)
    @filter.command("v2menu")
    async def menu(self, event: AstrMessageEvent, query: GreedyStr):
        """浏览 QQ V2 指令分类、搜索和参数帮助。"""
        await send_help(self, event, query)

    @filter.custom_filter(V2Addressed, priority=100)
    async def addressed(self, event: AstrMessageEvent):
        """由真实@事件唤醒，不生成缺失的Bot ID或At结构。"""
        pass

    @filter.on_astrbot_loaded()
    async def host_ready(self):
        self.catalog_ready = True

    @filter.on_plugin_loaded()
    async def catalog_loaded(self, plugin):
        if self.panels:
            self.panels.stable.clear()
        if plugin is not self.context.get_registered_star(PLUGIN_NAME):
            return
        for item in self.context.get_config().get("platform", []):
            if item.get("type") != PLATFORM_TYPE or item.get("enable") is not True:
                continue
            if any(instance.identity.platform_id == item.get("id") for instance in self.instances):
                continue
            try:
                await self.context.platform_manager.load_platform(item)
            except Exception:
                logger.exception("qq-v2 failed to restore platform %s after plugin reload", item.get("id"))

    @filter.on_plugin_unloaded()
    async def catalog_unloaded(self, plugin):
        if self.panels:
            self.panels.stable.clear()

    async def terminate(self):
        self.stopping = True
        if self._termination is None or (self._termination.done() and (self._termination.cancelled() or self._termination.exception() is not None)):
            self._termination = asyncio.create_task(self._terminate_owned(), name="qq-v2-plugin-close")
            self._termination.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._termination)

    async def _terminate_owned(self):
        if self.control:
            self.control.close()
        errors = []
        self.delivery_slots.close()
        for service in (self.panels, self.onboarding, self.connections):
            if service:
                try:
                    async with asyncio.timeout(5):
                        await service.close()
                except Exception as exc:
                    errors.append(exc)
        for instance in list(self.instances):
            try:
                async with asyncio.timeout(5):
                    manager = self.context.platform_manager
                    if any(i is instance for i in manager.get_insts()):
                        await manager.terminate_platform(instance.identity.platform_id)
                    # Also close instances created before manager ownership was established.
                    await instance.terminate()
            except Exception as exc:
                errors.append(exc)
        if self.adapter_class is not None and platform_cls_map.get(PLATFORM_TYPE) is self.adapter_class:
            unregister_platform_adapters_by_module(self.adapter_class.__module__)
        if self.store:
            self.store.close()
        if self.inbox:
            self.inbox.close()
        if self.extension_state:
            await self.extension_state.close()
        if self.media_pool:
            self.media_pool.close()
        if self.messages:
            self.messages.close()
        if errors:
            raise ExceptionGroup("QQ V2 resource cleanup failed", errors)
