"""One client, one generation and one explicit capability boundary."""

from . import PLATFORM_TYPE, VERSION
from .errors import V2Error, not_ready, unsupported
from .extensions.management import MANAGEMENT_ACTIONS, NATIVE_ACTIONS
from .models import SessionRoute, text_id
from .protocol import avatar_url

REMOTE_ACTIONS = {
    "send_group_msg", "send_private_msg", "send_msg", "delete_msg", "get_msg",
    "get_login_info", "get_group_info", "get_group_member_info", "get_group_member_list",
    "set_group_ban", "set_group_kick", "set_group_add_request",
}
UNSUPPORTED_ACTIONS = {
    "get_friend_list", "get_group_list", "set_group_whole_ban", "set_friend_add_request",
    "set_group_card", "set_group_admin", "set_group_name", "set_group_leave",
    "send_like", "send_group_forward_msg", "upload_group_file",
}
LOCAL_ACTIONS = {"get_status", "get_version_info", "get_stranger_info", "_qq_get_avatar",
                 "_qq_get_capabilities", "can_send_image", "can_send_record"}
SEND_ACTIONS = {"send_group_msg", "send_private_msg", "send_msg"}
WRITE_ACTIONS = SEND_ACTIONS | {"delete_msg", "set_group_ban", "set_group_kick", "set_group_add_request"}
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
    "get_group_member_list": {"group_id": "id"},
    "set_group_ban": {"group_id": "id", "user_id": "id", "duration": "int", "_qq_operation_id": "id"},
    "set_group_kick": {"group_id": "id", "user_id": "id", "reject_add_request": "bool", "_qq_operation_id": "id"},
    "set_group_add_request": {"flag": "id", "sub_type": "str", "approve": "bool", "reason": "str"},
    "delete_msg": {"message_id": "id", "_qq_operation_id": "id"},
    "_qq_get_send_status": {"operation_id": "id"}, "_qq_get_extension_status": {"operation_id": "id"},
}
ACTION_RETURNS = {
    **{name: ["message_id", "operation_id", "msg_seq", "state", "wire_started", "timestamp?", "ref_idx?", "media?"] for name in SEND_ACTIONS},
    "get_status": ["online", "good", "state", "platform_id", "generation", "network"],
    "get_version_info": ["app_name", "app_version", "protocol_version"],
    "get_login_info": ["user_id", "nickname", "id_kind", "source"],
    "get_stranger_info": ["user_id", "id_kind", "scope", "nickname?", "source", "partial", "first_seen", "last_seen", "source_message_id"],
    "get_group_info": ["group_id", "group_name", "member_count", "partial", "permission"],
    "get_group_member_info": ["group_id", "user_id", "nickname", "role", "bot", "partial", "id_kind"],
    "get_group_member_list": ["array of get_group_member_info"],
    "_qq_get_avatar": ["url", "kind", "size", "source", "verified"],
    "_qq_get_send_status": ["op_id", "scene", "target", "source", "state", "seq", "result", "error"],
    "_qq_get_extension_status": ["op_id", "kind", "state", "updated", "error"],
    "_qq_get_capabilities": ["actions", "network_api", "compatibility"],
    "set_group_ban": ["state"], "delete_msg": ["state"], "set_group_add_request": ["state"],
    "set_group_kick": ["state", "removed"],
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

    def check(self, generation):
        if self.closed or generation != self.identity.generation:
            raise V2Error("stale_generation", "The source instance has stopped or changed.", status=409)
        self.guard()


class NativeView:
    def __init__(self, client):
        self._client = client

    def avatar_url(self, openid, size=100):
        self._client.check()
        return avatar_url(self._client.identity.robot, openid, size)

    user_avatar_url = avatar_url
    group_avatar_url = avatar_url

    async def send(self, scene, target, message, *, markdown=None, operation_id=None):
        route = self._client.route_for(scene, target)
        return await self._client.send(route, message, markdown=markdown, operation_id=operation_id)

    def streaming_mode(self, scene, target, *, use_fallback=False):
        self._client.check()
        if self._client._state.streaming is None:
            raise not_ready()
        return self._client._state.streaming.mode(self._client.route_for(scene, target), use_fallback)

    async def send_streaming(self, scene, target, generator, *, input_mode="append", use_fallback=False, operation_id=None):
        return await self._client.stream(self._client.route_for(scene, target), generator, input_mode=input_mode, use_fallback=use_fallback, operation_id=operation_id)

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
        self._client.check()
        if self._client._state.sender is None:
            raise not_ready()
        result = self._client._state.sender.store.operation(self._client.identity.robot, operation_id)
        return {k: result[k] for k in ("op_id", "scene", "target", "source", "state", "seq", "result", "error")}

    def __getattr__(self, name):
        if name not in NATIVE_ACTIONS:
            raise AttributeError(name)
        async def call(*args, **kwargs):
            import inspect
            self._client.check()
            service = self._client._state.management
            if service is None:
                raise not_ready()
            method = getattr(service, name)
            try:
                inspect.signature(method).bind(*args, **kwargs)
            except TypeError:
                raise V2Error("invalid_params", "Unknown or missing named operation parameters.") from None
            return await method(*args, **kwargs)
        return call

    def extension_events(self, limit=32):
        self._client.check()
        if self._client._state.extensions is None:
            raise not_ready()
        return self._client._state.extensions.records(limit)

    def extension_status(self, operation_id):
        self._client.check()
        if self._client._state.extension_state is None:
            raise not_ready()
        return self._client._state.extension_state.operation(self._client.identity.robot, operation_id)

    async def request(self, spec):
        self._client.check()
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
                        "markdown_segment": "nonstandard extension", "max_characters": 4096,
                        "channel_dm": "online WebSocket required",
                        "media": {"support": "conditional", "group_c2c": ["image", "record", "video", "file"], "channel": ["image_http_url", "image_multipart"], "dm": ["image_http_url"]} if self._state.sender and self._state.sender.media else "not_implemented"},
            "streaming": {"c2c": "native", "other_scenes": "explicit_bounded_aggregate", "permission": "unknown"} if self._state.streaming else "not_implemented",
            "interaction": {"ack_types": [11, 12], "execution": "opt_in_actor_ticket_host_pipeline", "permission": "unknown"} if self._state.extensions else "not_implemented",
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
