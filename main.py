"""Register the independent QQ V2 prototype only while this plugin is active."""

import asyncio
import copy

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.register import (
    platform_cls_map,
    register_platform_adapter,
    unregister_platform_adapters_by_module,
)

from .v2 import PLATFORM_TYPE, PLUGIN_NAME
from .v2.adapter import DEFAULT_PLATFORM_CONFIG, V2Adapter
from .v2.settings import SettingsStore
from .v2.web_api import ControlAPI
from .v2.connections import Connections
from .v2.transport.inbox import RawInbox
from .v2.onboarding import Onboarding


class V2Only(filter.CustomFilter):
    def filter(self, event, cfg):
        return event.get_platform_name() == PLATFORM_TYPE


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
        self.connections = None
        self.onboarding = None
        self._termination = None

    async def initialize(self):
        self.stopping = False
        try:
            self.store = SettingsStore(StarTools.get_data_dir(PLUGIN_NAME) / "settings.sqlite3")
            self.inbox = RawInbox(StarTools.get_data_dir(PLUGIN_NAME) / "transport.sqlite3")
            owner = self

            class OwnedAdapter(V2Adapter):
                pass

            OwnedAdapter.owner = owner
            self.adapter_class = OwnedAdapter
            register_platform_adapter(
                PLATFORM_TYPE, "QQ 官方 V2（连接层，消息能力开发中）",
                default_config_tmpl=copy.deepcopy(DEFAULT_PLATFORM_CONFIG),
                adapter_display_name="QQ 官方 V2 · 原型",
                config_metadata={
                    "secret": {"description": "QQ AppSecret", "type": "string", "secret": True,
                               "hint": "仅保存在本体平台配置；启用平台将连接 QQ。"},
                    "appid": {"description": "QQ AppID", "type": "string"},
                },
                support_streaming_message=False,
            )(OwnedAdapter)
            self.control = ControlAPI(self)
            self.control.register()
            self.connections = Connections(self)
            self.connections.prepare_webhooks()
            self.onboarding = Onboarding(self)
        except BaseException:
            await self.terminate()
            raise

    @filter.custom_filter(V2Only)
    @filter.command("v2menu")
    async def menu(self, event: AstrMessageEvent):
        """QQ V2 菜单入口（原型仅提供管理页预览）。"""
        yield event.plain_result("QQ V2 原型：请在插件 Pages → control 查看指令目录与本地预览；QQ 收发尚未实现。")

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
        for service in (self.onboarding, self.connections):
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
        if errors:
            raise ExceptionGroup("QQ V2 resource cleanup failed", errors)
