"""One client, one generation and one explicit capability boundary."""

import sqlite3
import weakref

from . import PLATFORM_TYPE, VERSION
from .errors import V2Error, not_ready, unsupported
from .extensions.management import MANAGEMENT_ACTIONS, NATIVE_ACTIONS
from .messaging.reply import ACTIVE_FALLBACK_CODES
from .models import SessionRoute, text_id
from .protocol import avatar_url
from .sdk.api.group_admin import NativeGroupAdminMixin
from .sdk.api.guild_admin import NativeGuildAdminMixin
from .sdk.api.media import NativeMediaMixin
from .sdk.api.menus import NativeMenuMixin
from .sdk.api.messages import NativeMessageMixin
from .sdk.api.reads import NativeReadMixin

REMOTE_ACTIONS = {
    "send_group_msg", "send_private_msg", "send_msg", "delete_msg", "get_msg",
    "get_login_info", "get_group_info", "get_group_member_info", "get_group_member_list",
    "set_group_ban", "set_group_kick", "set_group_add_request",
    "set_group_kick_members", "get_group_shut_list", "_qq_get_group_blacklist",
    "_qq_set_group_blacklist", "_qq_get_join_approval_strategies",
}
UNSUPPORTED_ACTIONS = {
    "get_friend_list", "get_group_list", "set_group_whole_ban", "set_friend_add_request",
    "set_group_card", "set_group_admin", "set_group_name", "set_group_leave",
    "send_like", "send_group_forward_msg", "upload_group_file",
}
NATIVE_READ_ALIASES = frozenset(name for name in vars(NativeReadMixin) if not name.startswith("_")) - REMOTE_ACTIONS | {"iter_group_members"}
NATIVE_WRITE_ALIASES = frozenset(name for name in vars(NativeMessageMixin) if not name.startswith("_")) - REMOTE_ACTIONS
NATIVE_WRITE_ALIASES |= frozenset(name for name in vars(NativeMediaMixin) if not name.startswith("_"))
NATIVE_WRITE_ALIASES |= frozenset(name for mixin in (NativeGroupAdminMixin, NativeGuildAdminMixin, NativeMenuMixin)
                                for name in vars(mixin) if not name.startswith("_"))

LOCAL_ACTIONS = {"get_status", "get_version_info", "get_stranger_info", "_qq_get_avatar",
                 "_qq_get_capabilities", "can_send_image", "can_send_record"}
SEND_ACTIONS = {"send_group_msg", "send_private_msg", "send_msg"}
WRITE_ACTIONS = SEND_ACTIONS | {"delete_msg", "set_group_ban", "set_group_kick", "set_group_add_request",
                                "set_group_kick_members", "_qq_set_group_blacklist"}
LOCAL_ACTIONS |= {"_qq_get_send_status", "_qq_get_extension_status"}
# Shared descriptors are used by the network normalizer and capability projection, not a second dispatcher.
ACTION_PARAMS = {
    "send_group_msg": {"group_id": "id", "message": "message", "auto_escape": "bool", "_qq_operation_id": "id", "_qq_reply_context": "id"},
    "send_private_msg": {"user_id": "id", "message": "message", "auto_escape": "bool", "_qq_operation_id": "id", "_qq_reply_context": "id"},
    "send_msg": {"group_id": "id", "user_id": "id", "message_type": "str", "message": "message", "auto_escape": "bool", "_qq_operation_id": "id", "_qq_reply_context": "id"},
    "get_stranger_info": {"user_id": "id", "no_cache": "bool", "id_kind": "str", "scope": "str"},
    "_qq_get_avatar": {"user_id": "id", "group_id": "id", "id_kind": "str", "scope": "str", "size": "int", "kind": "str"},
    "get_login_info": {},
    "get_group_info": {"group_id": "id", "no_cache": "bool"},
    "get_group_member_info": {"group_id": "id", "user_id": "id", "no_cache": "bool"},
    "get_group_member_list": {"group_id": "id", "no_cache": "bool"},
    "set_group_ban": {"group_id": "id", "user_id": "id", "duration": "int", "_qq_operation_id": "id"},
    "set_group_kick": {"group_id": "id", "user_id": "id", "reject_add_request": "bool", "_qq_operation_id": "id"},
    "set_group_add_request": {"flag": "id", "sub_type": "str", "approve": "bool", "reason": "str"},
    "set_group_kick_members": {"group_id": "id", "user_ids": "list", "reject_add_request": "bool", "_qq_operation_id": "id"},
    "get_group_shut_list": {"group_id": "id"},
    "_qq_get_group_blacklist": {"group_id": "id", "cursor": "str", "limit": "int"},
    "_qq_set_group_blacklist": {"group_id": "id", "op": "str", "user_ids": "list", "_qq_operation_id": "id"},
    "_qq_get_join_approval_strategies": {"cursor": "str", "limit": "int"},
    "delete_msg": {"message_id": "id", "_qq_operation_id": "id"},
    "_qq_get_send_status": {"operation_id": "id"}, "_qq_get_extension_status": {"operation_id": "id"},
}
ACTION_RETURNS = {
    **{name: ["message_id", "operation_id", "msg_seq", "state", "wire_started", "delivery?", "timestamp?", "ref_idx?", "media?"] for name in SEND_ACTIONS},
    "get_status": ["online", "good", "state", "platform_id", "generation", "network"],
    "get_version_info": ["app_name", "app_version", "protocol_version"],
    "get_login_info": ["user_id", "nickname", "id_kind", "source"],
    "get_stranger_info": ["user_id", "id_kind", "scope", "nickname?", "source", "partial", "first_seen?", "last_seen?", "source_message_id?", "_qq?"],
    "get_group_info": ["group_id", "group_name", "member_count", "partial", "permission"],
    "get_group_member_info": ["group_id", "user_id", "nickname", "role", "bot", "partial", "id_kind"],
    "get_group_member_list": ["array of get_group_member_info"],
    "_qq_get_avatar": ["url", "kind", "size", "source", "verified"],
    "_qq_get_send_status": ["op_id", "scene", "target", "source", "state", "seq", "result", "error"],
    "_qq_get_extension_status": ["op_id", "kind", "state", "updated", "error"],
    "_qq_get_capabilities": ["actions", "network_api", "compatibility"],
    "set_group_ban": ["state"], "delete_msg": ["state"], "set_group_add_request": ["state"],
    "set_group_kick": ["state", "removed"],
    "set_group_kick_members": ["state", "removed", "blacklist_failed?"],
    "get_group_shut_list": ["array of real member mutes"],
    "_qq_get_group_blacklist": ["users", "next_cursor"],
    "_qq_set_group_blacklist": ["fail_openids"],
    "_qq_get_join_approval_strategies": ["list", "next_cursor"],
    "can_send_image": ["yes", "implemented", "permission", "reason"],
    "can_send_record": ["yes", "implemented", "permission", "reason"],
}


class ClientState:
    def __init__(self, identity):
        self.identity = identity
        self.closed = False
        self.cache = None
        self.guard = lambda: None
        self.status = None
        self.http = None
        self.sender = None
        self.streaming = None
        self.typing = None
        self.management = None
        self.extensions = None
        self.extension_state = None
        self.network = None
        self.panels = None
        self.revoked_owners = []
        self.events = None
        self.profiles = None
        self.reads = None

    def revoke_owner(self, owner):
        """Invalidate views of an unloaded plugin without retaining live instances."""
        self.revoked_owners = [ref for ref in self.revoked_owners if ref() is not None]
        try:
            ref = weakref.ref(owner)
        except TypeError:
            def ref(owner=owner):
                return owner
        self.revoked_owners.append(ref)


    def check(self, generation):
        if self.closed or generation != self.identity.generation:
            raise V2Error("stale_generation", "The source instance has stopped or changed.", status=409)
        self.guard()


class NativeView(NativeMenuMixin, NativeGuildAdminMixin, NativeGroupAdminMixin, NativeMediaMixin, NativeMessageMixin, NativeReadMixin):
    def __init__(self, client, *, actor=None, options=None):
        from .sdk.types import CallOptions
        self._client = client
        self._actor = actor
        self._options = options or CallOptions()

    def _check(self):
        self._client.check()
        if self._options.owner is not None and any(ref() is self._options.owner for ref in self._client._state.revoked_owners):
            raise V2Error("stale_owner", "The owning plugin was unloaded.", status=409)


    def with_options(self, *, operation_id=None, owner=None):
        """Return a per-call view without changing the shared client state."""
        from .sdk.types import CallOptions
        if operation_id is not None:
            text_id(operation_id)
        self._check()
        return NativeView(self._client, actor=self._actor, options=CallOptions(operation_id, owner))

    def _operation_id(self, explicit):
        self._check()
        default = self._options.operation_id
        if default is not None and explicit is not None and explicit != default:
            raise V2Error("operation_conflict", "Call options and explicit operation ID disagree.", status=409)
        return explicit if explicit is not None else default

    @property
    def events(self):
        self._check()
        if self._client._state.events is None:
            raise not_ready()
        return self._client._state.events

    @property
    def profiles(self):
        self._check()
        if self._client._state.profiles is None:
            raise not_ready()
        return self._client._state.profiles

    async def get_group_member_info(self, group_openid: str, member_openid: str) -> dict:
        """Read current QQ member data and merge scoped profile evidence."""
        self._check()
        if self._client._state.reads is None:
            raise not_ready()
        profiles = self._client._state.profiles
        revision = profiles.store.revision(self._client.identity.robot, "group", group_openid) if profiles else 0
        started = profiles.store.clock() if profiles else None
        data = await self._client._state.reads.get_group_member_info(group_openid, member_openid)
        self._check()
        if profiles:
            try:
                profiles.store.query_member(self._client.identity.robot, "group", group_openid, member_openid,
                    started_revision=revision, started_at=started, fields={
                        "nickname": data.get("username"), "last_known_role": data.get("member_role"),
                        "joined_at": data.get("joined_at"), "bot": data.get("bot"),
                        "union_openid": data.get("union_openid")})
            except V2Error as exc:
                if exc.code != "cache_capacity":
                    raise
            except sqlite3.Error:
                profiles.store.last_error = "cache_storage_unavailable"
        return data

    async def get_group_member_list(self, group_openid: str, cursor: str = "") -> dict:
        """Read one native member page including its official next_cursor."""
        self._check()
        if self._client._state.reads is None:
            raise not_ready()
        profiles = self._client._state.profiles
        started = profiles.store.clock() if profiles else None
        page = await self._client._state.reads.get_group_member_list(group_openid, cursor)
        self._check()
        if profiles:
            for member in page["members"]:
                if not isinstance(member, dict) or not isinstance(member.get("member_openid"), str):
                    continue
                try:
                    profiles.store.merge(self._client.identity.robot, "group", group_openid, member["member_openid"],
                        {"nickname": member.get("username"), "joined_at": member.get("joined_at"),
                         "last_known_role": member.get("member_role"), "bot": member.get("bot"),
                         "union_openid": member.get("union_openid")},
                        source="member_page", as_of=started)
                except V2Error as exc:
                    if exc.code != "cache_capacity":
                        raise
                except sqlite3.Error:
                    profiles.store.last_error = "cache_storage_unavailable"
        return page

    async def iter_group_members(self, group_openid: str):
        """Yield members across bounded native pages."""
        self._check()
        if self._client._state.reads is None:
            raise not_ready()
        async for row in self._client._state.reads.iter_group_members(group_openid, fetch=self.get_group_member_list):
            yield row

    async def get_group_info(self, group_openid: str) -> dict:
        """Read the native group response without a OneBot projection."""
        self._check()
        if self._client._state.management is None:
            raise not_ready()
        response = await self._client._state.management.group_info(group_openid)
        self._check()
        return response

    def callback_button(self, function, *, label, data=None, audience="actor"):
        self._check()
        service = self._client._state.extensions
        if service is None or self._client._route is None or self._actor is None:
            raise unsupported("Callback buttons require a live QQ V2 event and extension service.")
        return service.callbacks.issue(self._client._route, self._actor, function, label=label, data=data, audience=audience)

    def avatar_url(self, openid, size=100):
        self._check()
        return avatar_url(self._client.identity.robot, openid, size)

    user_avatar_url = avatar_url
    group_avatar_url = avatar_url

    async def send(self, scene, target, message, *, markdown=None, operation_id=None):
        route = self._client.route_for(scene, target)
        return await self._client.send(route, message, markdown=markdown, operation_id=self._operation_id(operation_id))

    def streaming_mode(self, scene, target, *, use_fallback=False):
        self._check()
        if self._client._state.streaming is None:
            raise not_ready()
        return self._client._state.streaming.mode(self._client.route_for(scene, target), use_fallback)

    async def send_streaming(self, scene, target, generator, *, input_mode="append", use_fallback=False, operation_id=None):
        return await self._client.stream(self._client.route_for(scene, target), generator, input_mode=input_mode, use_fallback=use_fallback, operation_id=self._operation_id(operation_id))

    async def send_file(self, scene, target, file, *, name="upload", kind="file", allow_file_fallback=True, operation_id=None):
        from .media.types import MediaInput
        return await self.send(scene, target, [MediaInput(kind, file, name, allow_file_fallback=allow_file_fallback)], operation_id=operation_id)

    async def typing(self, scene, target, *, seconds=10):
        client = self._client
        client.check()
        route = client.route_for(scene, target)
        if client._state.typing is None:
            raise unsupported("Typing service is not attached.")
        source = client._source if client._source and route == client._source.route else None
        return await client._state.typing.start(route, source, seconds=seconds)

    def send_status(self, operation_id):
        self._check()
        if self._client._state.sender is None:
            raise not_ready()
        result = self._client._state.sender.store.operation(self._client.identity.robot, operation_id)
        return {k: result[k] for k in ("op_id", "scene", "target", "source", "state", "seq", "result", "error")}

    def __getattr__(self, name):
        if name not in NATIVE_ACTIONS:
            raise AttributeError(name)
        async def call(*args, **kwargs):
            import inspect
            self._check()
            service = self._client._state.management
            if service is None:
                raise not_ready()
            method = getattr(service, name)
            if self._options.operation_id is not None and "operation_id" in inspect.signature(method).parameters:
                kwargs = dict(kwargs)
                kwargs["operation_id"] = self._operation_id(kwargs.get("operation_id"))
            try:
                inspect.signature(method).bind(*args, **kwargs)
            except TypeError:
                raise V2Error("invalid_params", "Unknown or missing named operation parameters.") from None
            return await method(*args, **kwargs)
        return call

    def extension_events(self, limit=32):
        self._check()
        if self._client._state.extensions is None:
            raise not_ready()
        return self._client._state.extensions.records(limit)

    def extension_status(self, operation_id):
        self._check()
        if self._client._state.extension_state is None:
            raise not_ready()
        return self._client._state.extension_state.operation(self._client.identity.robot, operation_id)

    async def request(self, spec):
        self._check()
        http = self._client._state.http
        if http is None:
            raise not_ready()
        if spec.method not in {"GET", "HEAD"}:
            raise unsupported("Arbitrary native writes cannot bypass the shared send/menu policy.")
        return await http.request(spec)


class V2Client:
    def __init__(self, identity, *, state=None, route=None, source=None):
        self.identity = identity
        self._state = state or ClientState(identity)
        self._route = route
        self._source = source
        self.api = self
        self.qq = NativeView(self)

    def check(self):
        self._state.check(self.identity.generation)

    def bind(self, route: SessionRoute, *, source=None):
        self.check()
        if route.robot != self.identity.robot:
            raise V2Error("identity_mismatch", "Session belongs to another robot.", status=409)
        if source is not None and (source.route != route or source.generation != self.identity.generation):
            raise V2Error("stale_generation", "Reply source is not owned by this event generation.", status=409)
        return V2Client(self.identity, state=self._state, route=route, source=source)

    async def close(self):
        self._state.closed = True

    def capabilities(self):
        result = {
            "compatibility": "OneBot v11-style OpenID subset; string IDs, not QQ numbers",
            "transport": "implemented" if self._state.http else "not_implemented",
            "network_api": self._state.network.capabilities() if self._state.network else {"support": "conditional", "state": "not_attached"},
            "basic_scenes": ["group", "c2c", "channel", "dm"] if self._state.sender else [],
            "sending": {"source": "exact event or conservative active policy", "permission": "unknown",
                        "active_fallback": {"default": True, "rejected_codes": sorted(ACTIVE_FALLBACK_CODES),
                                            "local_expiry": "retained_source_evidence_and_current_target_required", "unknown": "never_replay"},
                        "delivery_fields": {"mode": "passive|active", "reason": "null or code/business_code",
                                            "attempts": "at most two mode attempts: mode/state/wire_attempts/wire_started?/code?/business_code?/http_status?/trace_id?"},
                        "legacy_results": "delivery may be absent in pre-upgrade receipts",
                        "markdown_segment": "nonstandard extension", "max_characters": 4096,
                        "channel_dm": "online WebSocket required",
                        "media": {"support": "conditional", "group_c2c": ["image", "record", "video", "file"], "channel": ["image_http_url", "image_multipart"], "dm": ["image_http_url"]} if self._state.sender and self._state.sender.media else "not_implemented"},
            "streaming": {"c2c": "native", "other_scenes": "explicit_bounded_aggregate", "permission": "unknown"} if self._state.streaming else "not_implemented",
            "interaction": {"ack_types": [11, 12], "execution": "owned_menu_ticket_or_registered_star_button_callback", "permission": "unknown"} if self._state.extensions else "not_implemented",
            "actions": {
                **{name: {"support": "unsupported", "reason": "complete_action_contract_unavailable" if self._state.http else "transport_not_ready", "permission": "unknown"} for name in sorted(REMOTE_ACTIONS)},
                **{name: {"support": "unsupported", "reason": "no_equivalent_or_not_implemented", "permission": "unknown"} for name in sorted(UNSUPPORTED_ACTIONS)},
                **{name: {"support": "supported" if name in {"get_status", "get_version_info", "_qq_get_capabilities"} else "partial",
                          "permission": "unknown", "reason": "local_only"} for name in sorted(LOCAL_ACTIONS)},
                **({name: {"support": "conditional", "permission": "unknown", "reason": "observed_route_and_shared_quota_required"}
                    for name in ("send_group_msg", "send_private_msg", "send_msg")} if self._state.sender else {}),
                **({name: {"support": "conditional", "permission": "unknown", "reason": "real_response_and_retained_scope_required; writes_opt_in"}
                    for name in MANAGEMENT_ACTIONS} if self._state.management else {}),
            },
            "sdk": {"contract_version": 1, "http_targets": 96, "legacy_sdk_methods": 64,
                    "business_events": 56, "native_implemented": sorted(NATIVE_READ_ALIASES | NATIVE_WRITE_ALIASES | {
                        "get_group_info", "get_group_member_info", "get_group_member_list"}),
                    "event_observation": "live_bounded" if self._state.events else "not_attached",
                    "profiles": "durable" if self._state.profiles else "not_attached",
                    "account_permission": "unverified"},
            "identity_cache": "durable, robot/kind/scene/target-scoped chat observations" if self._state.cache is not None else "not_ready",
        }
        for name, entry in result["actions"].items():
            entry.update({"parameters": dict(ACTION_PARAMS.get(name, {})),
                          "returns": list(ACTION_RETURNS.get(name, [])),
                          "id_semantics": "AppID-scoped string OpenID / official message ID, never QQ number",
                          "permission_evidence": "per-request QQ response only; implementation does not grant permission"})
        for name in WRITE_ACTIONS - SEND_ACTIONS:
            result["actions"][name]["data_semantics"] = "adapter confirmed outcome object, not standard v11 null; partial compatibility"
        for name, missing in {"get_group_info": ["max_member_count"],
                              "get_group_member_info": ["sex", "age", "card", "title", "level"],
                              "get_group_member_list": ["sex", "age", "card", "title", "level"],
                              "get_stranger_info": ["unobserved profile fields", "live no_cache=true lookup"]}.items():
            result["actions"][name]["missing_fields"] = missing
        return result

    async def call_action(self, action, **params):
        self.check()
        if not isinstance(action, str) or not action:
            raise V2Error("invalid_action", "Action must be a nonempty string.")
        if action in SEND_ACTIONS and "_qq_reply_context" in params:
            if self._state.network is None:
                raise not_ready()
            params = dict(params)
            bound = self._state.network.bind_context(params.pop("_qq_reply_context"), action, params)
            return await bound.call_action(action, **params)
        if action in {"send_group_msg", "send_private_msg", "send_msg"} and self._state.sender:
            allowed = ACTION_PARAMS[action]
            if params.keys() - allowed or "message" not in params:
                raise V2Error("invalid_params", "Unsupported or missing send parameters.")
            scene = {"send_group_msg": "group", "send_private_msg": "c2c"}.get(action)
            if action == "send_msg":
                has_group, has_user = "group_id" in params, "user_id" in params
                if has_group == has_user:
                    raise V2Error("invalid_params", "Specify exactly one send target.")
                scene = "group" if has_group else "c2c"
                if params.get("message_type", "group" if has_group else "private") != ("group" if has_group else "private"):
                    raise V2Error("invalid_params", "Message type does not match target.")
            route = self.route_for(scene, params.get("group_id" if scene == "group" else "user_id"))
            return await self.send(route, params["message"], onebot=True, auto_escape=params.get("auto_escape", False), operation_id=params.get("_qq_operation_id"))
        if action in {"get_group_member_info", "get_group_member_list"} and self._state.profiles is not None:
            from .onebot_profiles import OneBotProfiles
            if params.keys() - ACTION_PARAMS[action] or type(params.get("no_cache", False)) is not bool:
                raise V2Error("invalid_params", "Group profile options must match the declared action.")
            compat = OneBotProfiles(self)
            group = params.get("group_id")
            if action == "get_group_member_info":
                return await compat.member(group, params.get("user_id"), refresh=params.get("no_cache", False))
            return await compat.members(group, refresh=params.get("no_cache", False))
        if action in {"set_group_kick_members", "get_group_shut_list", "_qq_get_group_blacklist",
                      "_qq_set_group_blacklist", "_qq_get_join_approval_strategies"}:
            if params.keys() - ACTION_PARAMS[action] or self._state.management is None:
                raise V2Error("invalid_params", "Unknown management parameters or unavailable service.")
            group = text_id(params.get("group_id")) if "group_id" in ACTION_PARAMS[action] else None
            if action == "set_group_kick_members":
                return await self._state.management.group_kick(group, params.get("user_ids"),
                    blacklist=params.get("reject_add_request", False), operation_id=params.get("_qq_operation_id"))
            if action == "get_group_shut_list":
                data = await self._state.management.group_mutes(group)
                return [{"group_id": group, "user_id": text_id(row.get("member_openid")),
                         "nickname": row.get("username"), "mute_expire_at": row.get("mute_expire_at"),
                         "_qq": {"source": "official_query"}} for row in data["members"]]
            if action == "_qq_get_group_blacklist":
                return await self.qq.get_group_member_blacklist(group, params.get("cursor"), params.get("limit"))
            if action == "_qq_set_group_blacklist":
                return await self.qq.set_group_member_blacklist(group, params.get("op"), params.get("user_ids"),
                    operation_id=params.get("_qq_operation_id"))
            return await self.qq.get_join_approval_strategies(params.get("cursor"), params.get("limit"))
        if action in MANAGEMENT_ACTIONS and self._state.management is not None:
            return await self._state.management.onebot(action, params)
        if action in REMOTE_ACTIONS:
            if self._state.http:
                raise unsupported("This action needs a complete contract beyond basic message sending.")
            raise not_ready()
        if action in UNSUPPORTED_ACTIONS or action not in LOCAL_ACTIONS:
            raise unsupported("Unknown or unsupported OneBot action.")
        allowed = ACTION_PARAMS.get(action, {})
        if params.keys() - allowed:
            raise V2Error("invalid_params", "Unsupported action parameters.")
        if action == "_qq_get_send_status":
            return self.qq.send_status(params.get("operation_id"))
        if action == "_qq_get_extension_status":
            record = self.qq.extension_status(params.get("operation_id"))
            return {key: record[key] for key in ("op_id", "kind", "state", "updated", "error")}
        if action == "get_status":
            if self._state.status:
                return self._state.status()
            return {"online": False, "good": False, "state": "transport_not_ready",
                    "platform_id": self.identity.platform_id, "generation": self.identity.generation}
        if action == "get_version_info":
            return {"app_name": PLATFORM_TYPE, "app_version": VERSION, "protocol_version": "v11-openid-subset"}
        if action == "_qq_get_capabilities":
            return self.capabilities()
        if action in ("can_send_image", "can_send_record"):
            implemented = bool(self._state.sender and self._state.sender.media)
            return {"yes": False, "implemented": implemented, "reason": "application_permission_unverified" if implemented else "transport_not_ready", "permission": "unknown"}
        if params.get("no_cache", False) is not False:
            raise unsupported("A live OpenID profile lookup is not available.")
        if action == "_qq_get_avatar" and params.get("kind") == "group":
            if not self._state.sender or "user_id" in params or params.get("id_kind") not in (None, "group_openid"):
                raise unsupported("Group avatars require an observed group target, not a user/channel identity.")
            target = text_id(params.get("group_id"))
            route = SessionRoute(self.identity.robot, "group", target)
            self._state.sender.store.target(route)
            size = params.get("size", 100)
            return {"url": self.qq.group_avatar_url(target, size), "kind": "group", "size": size, "source": "qqapp", "verified": False}
        user_id = text_id(params.get("user_id"))
        kind, scope = params.get("id_kind"), params.get("scope")
        if kind is None and scope is None and self._route:
            kind = {"c2c": "user_openid", "group": "member_openid", "channel": "channel_user_id", "dm": "channel_user_id"}[self._route.scene]
            scope = f"{self._route.scene}:{self._route.target}"
        if not isinstance(kind, str) or not isinstance(scope, str):
            raise V2Error("identity_scope_required", "Explicit id_kind and scope are required outside a bound chat route.")
        if action == "get_stranger_info" and self._state.profiles is not None:
            from .onebot_profiles import OneBotProfiles
            return OneBotProfiles(self).stranger(user_id, kind, scope)
        if self._state.cache is None:
            raise not_ready()
        record = self._state.cache.lookup(self.identity.robot, kind, scope, user_id)
        if action == "get_stranger_info":
            return {**record, "source": "chat_cache", "partial": True}
        if params.get("kind", "user") == "user" and record.get("avatar_url") and "size" not in params:
            return {"url": record["avatar_url"], "kind": "user", "size": None, "source": "chat_event", "verified": False}
        if params.get("kind", "user") != "user" or kind not in ("user_openid", "member_openid"):
            raise unsupported("This extension only resolves observed user/member OpenIDs.")
        size = params.get("size", 100)
        return {"url": self.qq.avatar_url(user_id, size), "kind": "user", "size": size,
                "source": "qqapp", "verified": False}

    def __getattr__(self, name):
        if name in NATIVE_READ_ALIASES | NATIVE_WRITE_ALIASES:
            return getattr(self.qq, name)
        if name not in REMOTE_ACTIONS | UNSUPPORTED_ACTIONS | LOCAL_ACTIONS:
            raise AttributeError(name)

        async def action(**params):
            return await self.call_action(name, **params)
        return action

    def route_for(self, scene, target):
        target = text_id(target)
        user = self._route.user if self._route and (self._route.scene, self._route.target) == (scene, target) else None
        return SessionRoute(self.identity.robot, scene, target, user)

    async def stream(self, route, generator, **options):
        self.check()
        if self._state.streaming is None:
            raise unsupported("Streaming service is not attached.")
        source = self._source if self._source and route == self._source.route else None
        try:
            return await self._state.streaming.send(route, generator, source=source, **options)
        finally:
            if self._state.typing and source is not None:
                await self._state.typing.stop(route, source=source)

    async def send(self, route, message, **options):
        if options.keys() - {"onebot", "auto_escape", "markdown", "operation_id", "keyboard"}:
            raise V2Error("invalid_params", "Unsupported send options.")
        self.check()
        if route.robot != self.identity.robot:
            raise V2Error("identity_mismatch", "Cannot send into another robot's session.", status=409)
        if self._state.sender:
            source = self._source if self._source and route == self._source.route else None
            try:
                return await self._state.sender.send(route, message, source=source, **options)
            finally:
                if self._state.typing and source is not None:
                    await self._state.typing.stop(route, source=source)
        if self._state.http:
            raise unsupported("This client is not attached to the shared sending service.")
        raise not_ready()
