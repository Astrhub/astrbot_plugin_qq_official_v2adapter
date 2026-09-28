"""Group-scoped LLM tools with host session checks and durable operation ownership."""

import functools
import hashlib
import json
from datetime import UTC, datetime

from astrbot.core.provider.register import llm_tools
from astrbot.core.star.session_plugin_manager import SessionPluginManager

from . import PLATFORM_TYPES, PLUGIN_NAME
from .errors import V2Error
from .event import V2MessageEvent
from .extensions.state import digest
from .models import text_id
from .onebot_profiles import official_member
from .profiles.store import key_of


READ_TOOLS = {"get_group", "get_member", "list_members", "find_known_members"}
GROUP_ADMIN_TOOLS = {"list_mutes", "mute_members", "kick_members", "list_blacklist", "change_blacklist",
                     "list_join_requests", "approve_join_request"}
ROBOT_ADMIN_TOOLS = {"list_join_strategies"}
TOOL_NAMES = READ_TOOLS | GROUP_ADMIN_TOOLS | ROBOT_ADMIN_TOOLS | {"get_operation_status"}
WRITES = {"mute_members", "kick_members", "change_blacklist", "approve_join_request"}


def bounded_members(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 20 or any(not isinstance(value, str) for value in values):
        raise V2Error("invalid_members", "Provide 1..20 explicit member OpenIDs.")
    names = [text_id(value) for value in values]
    if len(set(names)) != len(names):
        raise V2Error("invalid_members", "Duplicate member OpenIDs are not accepted.")
    return sorted(names)


def compact_operation(row):
    result = {"operation_id": row["op_id"], "state": row["state"], "kind": row["kind"]}
    error = row.get("error") or {}
    if error:
        result["error"] = {key: error[key] for key in ("code", "phase", "business_code") if key in error}
        details = error.get("details") or {}
        for key in ("removed", "failed", "succeeded", "blacklist_failed"):
            if isinstance(details.get(key), list):
                result[key] = details[key][:20]
    safe = row.get("result") or {}
    if isinstance(safe, dict):
        for key in ("state", "removed", "user_ids"):
            if key in safe and (key == "state" or isinstance(safe[key], list)):
                result[key] = safe[key] if key == "state" else safe[key][:20]
    return result


class GroupTools:
    def __init__(self, owner, *, active=None, session_allowed=None):
        self.owner = owner
        self._active = active or self._registered_tool_active
        self._session_allowed = session_allowed or SessionPluginManager.is_plugin_enabled_for_session

    def _registered_tool_active(self, name):
        module = type(self.owner).__module__
        return any(tool.name == name and tool.active and tool.handler_module_path == module
                   and isinstance(tool.handler, functools.partial) and tool.handler.args == (self.owner,)
                   for tool in llm_tools.func_list)

    async def _bound(self, event, name):
        if name not in TOOL_NAMES or self.owner.stopping or not self._active("qq_v2_" + name):
            raise V2Error("tool_disabled", "This group tool is not currently enabled.", status=403)
        if (type(event) is not V2MessageEvent or event.get_platform_name() not in PLATFORM_TYPES or
                event.route.scene != "group" or event.message_obj.group_id != event.route.target or
                event.raw_data.get("t") not in {"GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"} or
                event.raw_data.get("derived_from_interaction") or event.bot._source is None or
                event.bot._source.route != event.route or event.bot._source.generation != event.bot.identity.generation or
                event.bot._source.message_id != event.message_obj.message_id):
            raise V2Error("tool_scope_unavailable", "A live QQ V2 group chat is required.", status=403)
        instance = next((item for item in self.owner.instances if item.identity == event.bot.identity and
                         item.client._state is event.bot._state), None)
        if instance is None:
            raise V2Error("tool_scope_unavailable", "This event is not owned by a live platform instance.", status=403)
        instance.check_generation()
        if event.bot._source.expires <= self.owner.messages.now():
            raise V2Error("tool_source_expired", "A historical event cannot initiate a new group tool call.", status=409)
        if not await self._session_allowed(event.unified_msg_origin, PLUGIN_NAME):
            raise V2Error("tool_disabled", "This plugin is disabled for the current session.", status=403)
        return event.route.target, text_id(event.get_sender_id())

    async def _authorize(self, event, name, group, actor):
        if name in ROBOT_ADMIN_TOOLS:
            if not event.is_admin():
                raise V2Error("host_admin_required", "Robot-wide strategies require an AstrBot administrator.", status=403)
        elif name in GROUP_ADMIN_TOOLS or name == "get_operation_status":
            if event.is_admin():
                return
            try:
                live = await event.qq.get_group_member_info(group, actor)
            except V2Error:
                raise V2Error("operator_verification_unavailable", "A current QQ group role could not be verified.", status=403) from None
            if live.get("member_role") not in {"owner", "admin"}:
                raise V2Error("group_admin_required", "Only a current group owner or administrator may manage this group.", status=403)

    def _operation_id(self, event, group, actor, name, logical):
        binding = [event.bot.identity.robot.appid, event.bot.identity.robot.environment, event.bot.identity.platform_id,
                   event.bot.identity.generation, event.bot._source.message_id, event.bot._source.event_id,
                   event.unified_msg_origin, group, actor, name, logical]
        return "tool-" + digest(binding)

    async def _write(self, event, name, group, actor, logical, frozen, execute, *, operation_id=None):
        op_id = operation_id or self._operation_id(event, group, actor, name, logical)
        state = self.owner.extension_state
        data = state.claim_tool(event.bot.identity.robot, op_id, event.bot.identity.platform_id, group,
                                actor, event.unified_msg_origin, name, frozen, logical=logical)
        try:
            previous = state.operation(event.bot.identity.robot, op_id)
        except V2Error as exc:
            if exc.code != "operation_not_found":
                raise
        else:
            return compact_operation(previous)
        try:
            await execute(op_id, data)
        except V2Error as original:
            try:
                return compact_operation(state.operation(event.bot.identity.robot, op_id))
            except V2Error as exc:
                if exc.code != "operation_not_found":
                    raise
                raise original
        return compact_operation(state.operation(event.bot.identity.robot, op_id))

    async def call(self, event, name, **args):
        """Execute only the explicitly registered group tool with event-bound authority."""
        group, actor = await self._bound(event, name)
        await self._authorize(event, name, group, actor)
        qq = event.qq
        if name == "get_group":
            info = await event.get_group()
            return {"group_openid": group, "name": info.group_name, "member_count": info.member_count,
                    "source": "official_query" if info.group_name is not None else "event"}
        if name == "get_member":
            user = text_id(args["member_openid"])
            refresh = args.get("refresh", False)
            if type(refresh) is not bool:
                raise V2Error("invalid_refresh", "refresh must be boolean.")
            return await event.bot.call_action("get_group_member_info", group_id=group, user_id=user, no_cache=refresh)
        if name == "list_members":
            cursor = args.get("cursor", "")
            page = await qq.get_group_member_list(group, cursor)
            return {"members": [official_member(group, member, source="official_page", current=False)
                                for member in page["members"]], "next_cursor": page["next_cursor"],
                    "pagination_done": page["next_cursor"] == "", "non_atomic": True}
        if name == "find_known_members":
            term = args["query"]
            if not isinstance(term, str) or not 1 <= len(term) <= 100:
                raise V2Error("invalid_query", "Supply a bounded name or OpenID fragment.")
            result = []
            rows = self.owner.profiles.db.execute("SELECT subject,fields FROM profiles WHERE robot=? AND scene='group' AND scope=? AND kind='member_openid' ORDER BY subject",
                                                   (key_of(event.bot.identity.robot), group))
            for row in rows:
                fields = json.loads(row["fields"])
                nickname = fields.get("nickname", {}).get("value")
                if term.casefold() not in row["subject"].casefold() and (not isinstance(nickname, str) or term.casefold() not in nickname.casefold()):
                    continue
                if len(result) == 20:
                    return {"candidates": result, "truncated": True, "source": "profile_cache"}
                profile = self.owner.profiles.get_member(event.bot.identity.robot, "group", group, row["subject"])
                result.append({"member_openid": row["subject"], "nickname": nickname,
                               "membership": profile["membership"], "as_of": profile["as_of"]})
            return {"candidates": result, "truncated": False, "source": "profile_cache"}
        if name == "list_mutes":
            data = await qq.get_group_restrict_chat_setting(group)
            members = data.get("members")
            if not isinstance(members, list):
                raise V2Error("invalid_management_response", "QQ did not supply member mute state.", status=502)
            return {"mode": (data.get("global_rule") or {}).get("mode"), "members": [
                {key: row.get(key) for key in ("member_openid", "username", "mute_expire_at")}
                for row in members[:100]], "truncated": len(members) > 100}
        if name == "list_blacklist":
            page = await qq.get_group_member_blacklist(group, args.get("cursor"), args.get("limit", 20))
            return {"users": [{key: row.get(key) for key in ("member_openid", "username", "banned_at", "bot")}
                              for row in page["users"]], "next_cursor": page["next_cursor"]}
        if name == "list_join_requests":
            page = await qq.get_group_join_requests(group, args.get("cursor"), args.get("limit", 20))
            manager = event.bot._state.management
            return {"list": [{"member_openid": row["member_openid"], "username": row.get("username"),
                              "apply_at": row.get("apply_at"), "apply_source": row.get("apply_source"),
                              "flag": manager.observe_request(group, row, fresh_read=True)} for row in page["list"]],
                    "next_cursor": page["next_cursor"]}
        if name == "list_join_strategies":
            page = await qq.get_join_approval_strategies(args.get("cursor"), args.get("limit", 20))
            return {"strategies": [{key: row.get(key) for key in ("strategy_id", "remark", "is_enable")}
                                   for row in page.get("list", [])], "next_cursor": page["next_cursor"]}
        if name == "get_operation_status":
            op_id = text_id(args["operation_id"])
            kind = self.owner.extension_state.tool_owner(event.bot.identity.robot, op_id,
                event.bot.identity.platform_id, group, actor, event.unified_msg_origin)
            if kind in ROBOT_ADMIN_TOOLS and not event.is_admin():
                raise V2Error("host_admin_required", "Robot-wide tool status requires an AstrBot administrator.", status=403)
            try:
                return compact_operation(self.owner.extension_state.operation(event.bot.identity.robot, op_id))
            except V2Error as exc:
                if exc.code != "operation_not_found":
                    raise
                return {"operation_id": op_id, "state": "not_started", "kind": kind}
        if name in {"mute_members", "kick_members", "change_blacklist"}:
            users = bounded_members(args["member_openids"])
            if name == "mute_members":
                seconds = args["duration_seconds"]
                if type(seconds) is not int or not 0 <= seconds <= 30 * 86400:
                    raise V2Error("invalid_duration", "Group mute duration is 0..2592000 seconds.")
                expiry = datetime.fromtimestamp(self.owner.messages.now() + seconds, UTC).isoformat() if seconds else ""
                frozen = {"members": [{"op": "update" if seconds else "del", "member_openid": user,
                                       "mute_expire_at": expiry} for user in users]}
                async def execute(op_id, request):
                    await qq.set_group_restrict_chat_setting(group, request["members"], operation_id=op_id)
                return await self._write(event, name, group, actor, {"users": users, "duration_seconds": seconds}, frozen, execute)
            if name == "kick_members":
                blacklist = args.get("blacklist", False)
                if type(blacklist) is not bool:
                    raise V2Error("invalid_blacklist", "blacklist must be boolean.")
                frozen = {"members": users, "blacklist": blacklist}
                async def execute(op_id, request):
                    await qq.batch_remove_group_members(group, request["members"], request["blacklist"], operation_id=op_id)
                return await self._write(event, name, group, actor, frozen, frozen, execute)
            op = args["op"]
            if op not in ("add", "del"):
                raise V2Error("invalid_blacklist", "Choose add or del.")
            frozen = {"members": users, "op": op}
            async def execute(op_id, request):
                await qq.set_group_member_blacklist(group, request["op"], request["members"], operation_id=op_id)
            return await self._write(event, name, group, actor, frozen, frozen, execute)
        if name == "approve_join_request":
            flag = text_id(args["flag"])
            approve = args["approve"]
            reason = args.get("reason", "")
            blacklist = args.get("blacklist", False)
            if type(approve) is not bool or type(blacklist) is not bool or not isinstance(reason, str) or len(reason) > 200:
                raise V2Error("invalid_approval", "Provide explicit approval and bounded rejection options.")
            fingerprint = hashlib.sha256(flag.encode()).hexdigest()
            op_id = "approve-" + fingerprint
            frozen = {"flag_hash": fingerprint, "approve": approve, "reason": reason, "blacklist": blacklist}
            manager = event.bot._state.management
            row = manager.store.db.execute("SELECT target FROM join_flags WHERE robot=? AND flag_hash=?", (key_of(event.bot.identity.robot), fingerprint)).fetchone()
            if row is None or row["target"] != group:
                raise V2Error("request_flag_invalid", "A current group application flag is required.", status=404)
            async def execute(op_id, request):
                await manager.approve(flag, approve=request["approve"], reason=request["reason"], blacklist=request["blacklist"])
            return await self._write(event, name, group, actor, frozen, frozen, execute, operation_id=op_id)
        raise V2Error("invalid_tool", "No group tool is registered under this name.")
