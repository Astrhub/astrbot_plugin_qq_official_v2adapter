"""Register the independent QQ V2 prototype only while this plugin is active."""

import asyncio
import copy
import json

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.platform.register import (
    platform_cls_map,
    platform_registry,
    register_platform_adapter,
)
from astrbot.core.star.filter.command import GreedyStr

from .v2 import PLATFORM_TYPE, PLATFORM_TYPES, PLUGIN_NAME, WEBHOOK_TYPE
from .v2.adapter import DEFAULT_PLATFORM_CONFIG, V2Adapter
from .v2.group_tools import GroupTools
from .v2.connections import Connections
from .v2.errors import V2Error
from .v2.extensions.state import ExtensionStore
from .v2.help import send_help
from .v2.media.io import BlobPool
from .v2.messaging.delivery import DeliverySlots
from .v2.messaging.store import MessageStore
from .v2.onboarding import Onboarding
from .v2.panels import PanelService
from .v2.profiles.store import ProfileStore
from .v2.settings import SettingsStore
from .v2.transport.inbox import RawInbox
from .v2.web_api import ControlAPI


class V2Only(filter.CustomFilter):
    def filter(self, event, cfg):
        return event.get_platform_name() in PLATFORM_TYPES


class V2Addressed(filter.CustomFilter):
    def filter(self, event, cfg):
        return event.get_platform_name() in PLATFORM_TYPES and event.raw_data.get("t") in {"GROUP_AT_MESSAGE_CREATE", "AT_MESSAGE_CREATE"}

class QQOfficialV2(Star):
    def __init__(self, context: Context, config):
        super().__init__(context)
        self.config = config
        self.stopping = True
        self.instances = set()
        self.store = None
        self.control = None
        self.adapter_class = None
        self.adapter_classes = {}
        self.identify_budgets = {}
        self.inbox = None
        self.messages = None
        self.extension_state = None
        self.profiles = None
        self.media_pool = None
        self.panels = None
        self.group_tools = None
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
            for name, low, high in (("profile_max_records", 1, 65536), ("profile_max_bytes", 1048576, 268435456),
                                    ("profile_stale_seconds", 1, 86400), ("profile_cooldown_seconds", 1, 86400)):
                value = self.config.get(name, {"profile_max_records": 32768, "profile_max_bytes": 67108864,
                                               "profile_stale_seconds": 300, "profile_cooldown_seconds": 600}[name])
                if type(value) is not int or not low <= value <= high:
                    raise V2Error("invalid_profile_config", f"Invalid {name} profile setting")
            self.profiles = ProfileStore(StarTools.get_data_dir(PLUGIN_NAME) / "profiles.sqlite3",
                max_profiles=self.config.get("profile_max_records", 32768),
                max_bytes=self.config.get("profile_max_bytes", 67108864),
                stale_seconds=self.config.get("profile_stale_seconds", 300))
            self.profiles.migrate_identities(self.messages)
            self.media_pool = BlobPool(StarTools.get_data_dir(PLUGIN_NAME) / "media-spool")
            for kind in PLATFORM_TYPES:
                adapter = type("OwnedWebhookAdapter" if kind == WEBHOOK_TYPE else "OwnedAdapter", (V2Adapter,),
                               {"owner": self, "__module__": __name__})
                title = "QQ 官方 V2（Webhook）" if kind == WEBHOOK_TYPE else "QQ 官方 V2（WebSocket）"
                template = {**copy.deepcopy(DEFAULT_PLATFORM_CONFIG), "type": kind, "id": kind}
                register_platform_adapter(
                    kind, title, default_config_tmpl=template, adapter_display_name=title,
                    logo_path="assets/qq.png", support_streaming_message=True,
                    config_metadata={
                        "secret": {"description": "QQ AppSecret", "type": "string", "secret": True},
                        "appid": {"description": "QQ AppID", "type": "string"},
                        **{key: {"invisible": True} for key in
                           ("environment", "transport", "intents", "shard", "shard_mode", "onebot", "logo_token")},
                    },
                )(adapter)
                self.adapter_classes[kind] = adapter
            self.adapter_class = self.adapter_classes[PLATFORM_TYPE]
            self.control = ControlAPI(self)
            self.control.register()
            self.connections = Connections(self)
            self.connections.prepare_webhooks()
            self.onboarding = Onboarding(self)
            self.panels = PanelService(self)
            self.panels.start()
            self.group_tools = GroupTools(self)
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

    async def _tool_json(self, event, name, **args):
        if self.group_tools is None:
            raise V2Error("service_stopped", "Group tools are not initialized.", status=503)
        return json.dumps(await self.group_tools.call(event, name, **args), ensure_ascii=False)

    @filter.llm_tool(name="qq_v2_get_group")
    async def tool_get_group(self, event: AstrMessageEvent):
        """Read the current QQ V2 group."""
        return await self._tool_json(event, "get_group")

    @filter.llm_tool(name="qq_v2_get_member")
    async def tool_get_member(self, event: AstrMessageEvent, member_openid: str, refresh: bool = False):
        """Read a current-group member without granting historical roles.

        Args:
            member_openid(string): Exact member OpenID.
            refresh(boolean): Require a live QQ read.
        """
        return await self._tool_json(event, "get_member", member_openid=member_openid, refresh=refresh)

    @filter.llm_tool(name="qq_v2_list_members")
    async def tool_list_members(self, event: AstrMessageEvent, cursor: str = ""):
        """Read one non-atomic current-group member page.

        Args:
            cursor(string): Official next cursor.
        """
        return await self._tool_json(event, "list_members", cursor=cursor)

    @filter.llm_tool(name="qq_v2_find_known_members")
    async def tool_find_known_members(self, event: AstrMessageEvent, query: str):
        """Find same-group historical name candidates without QQ network access.

        Args:
            query(string): Nickname or OpenID fragment.
        """
        return await self._tool_json(event, "find_known_members", query=query)

    @filter.llm_tool(name="qq_v2_list_mutes")
    async def tool_list_mutes(self, event: AstrMessageEvent):
        """Read current-group mute state after checking a live administrator role."""
        return await self._tool_json(event, "list_mutes")

    @filter.llm_tool(name="qq_v2_mute_members")
    async def tool_mute_members(self, event: AstrMessageEvent, member_openids: list[str], duration_seconds: int):
        """Mute up to 20 explicit group members; zero unmutes.

        Args:
            member_openids(list[string]): Exact member OpenIDs.
            duration_seconds(number): Mute seconds or zero.
        """
        return await self._tool_json(event, "mute_members", member_openids=member_openids, duration_seconds=duration_seconds)

    @filter.llm_tool(name="qq_v2_kick_members")
    async def tool_kick_members(self, event: AstrMessageEvent, member_openids: list[str], blacklist: bool = False):
        """Remove explicit group members without replaying partial results.

        Args:
            member_openids(list[string]): Exact member OpenIDs.
            blacklist(boolean): Also request QQ blacklisting.
        """
        return await self._tool_json(event, "kick_members", member_openids=member_openids, blacklist=blacklist)

    @filter.llm_tool(name="qq_v2_list_blacklist")
    async def tool_list_blacklist(self, event: AstrMessageEvent, cursor: str = "", limit: int = 20):
        """Read one current-group blacklist page.

        Args:
            cursor(string): Official next cursor.
            limit(number): Maximum page size.
        """
        return await self._tool_json(event, "list_blacklist", cursor=cursor, limit=limit)

    @filter.llm_tool(name="qq_v2_change_blacklist")
    async def tool_change_blacklist(self, event: AstrMessageEvent, op: str, member_openids: list[str]):
        """Add or remove explicit current-group blacklist members.

        Args:
            op(string): Add or del.
            member_openids(list[string]): Exact member OpenIDs.
        """
        return await self._tool_json(event, "change_blacklist", op=op, member_openids=member_openids)

    @filter.llm_tool(name="qq_v2_list_join_requests")
    async def tool_list_join_requests(self, event: AstrMessageEvent, cursor: str = "", limit: int = 20):
        """Read current-group applications and mint real pending flags.

        Args:
            cursor(string): Official next cursor.
            limit(number): Maximum page size.
        """
        return await self._tool_json(event, "list_join_requests", cursor=cursor, limit=limit)

    @filter.llm_tool(name="qq_v2_approve_join_request")
    async def tool_approve_join_request(self, event: AstrMessageEvent, flag: str, approve: bool,
                                        reason: str = "", blacklist: bool = False):
        """Resolve an observed pending application of this group.

        Args:
            flag(string): Real pending flag from list_join_requests.
            approve(boolean): Accept or decline.
            reason(string): Optional decline reason.
            blacklist(boolean): Blacklist on decline.
        """
        return await self._tool_json(event, "approve_join_request", flag=flag, approve=approve,
                                     reason=reason, blacklist=blacklist)

    @filter.llm_tool(name="qq_v2_list_join_strategies")
    async def tool_list_join_strategies(self, event: AstrMessageEvent, cursor: str = "", limit: int = 20):
        """Read robot-wide strategy summaries for AstrBot administrators.

        Args:
            cursor(string): Official next cursor.
            limit(number): Maximum page size.
        """
        return await self._tool_json(event, "list_join_strategies", cursor=cursor, limit=limit)

    @filter.llm_tool(name="qq_v2_get_operation_status")
    async def tool_get_operation_status(self, event: AstrMessageEvent, operation_id: str):
        """Read only this actor and session's retained group-tool outcome.

        Args:
            operation_id(string): Tool operation ID.
        """
        return await self._tool_json(event, "get_operation_status", operation_id=operation_id)


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
            if item.get("type") not in PLATFORM_TYPES or item.get("enable") is not True:
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

        owner = getattr(plugin, "star_cls", None)
        if owner is not None:
            for instance in tuple(self.instances):
                instance.client._state.events.close_owner(owner)
                instance.client._state.revoke_owner(owner)

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
        for kind, adapter in self.adapter_classes.items():
            if platform_cls_map.get(kind) is adapter:
                del platform_cls_map[kind]
                platform_registry[:] = [m for m in platform_registry if not (m.name == kind and m.module_path == adapter.__module__)]
        self.identify_budgets.clear()
        if self.store:
            self.store.close()
        if self.inbox:
            self.inbox.close()
        if self.extension_state:
            await self.extension_state.close()
        if self.profiles:
            self.profiles.close()
        if self.media_pool:
            self.media_pool.close()
        if self.messages:
            self.messages.close()
        if errors:
            raise ExceptionGroup("QQ V2 resource cleanup failed", errors)
