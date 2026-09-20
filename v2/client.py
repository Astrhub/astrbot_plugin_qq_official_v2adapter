"""One client, one generation and one explicit capability boundary."""

from . import PLATFORM_TYPE, VERSION
from .errors import V2Error, not_ready, unsupported
from .models import SessionRoute, text_id
from .protocol import IdentityCache, avatar_url

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


class ClientState:
    def __init__(self, identity):
        self.identity = identity
        self.closed = False
        self.cache = IdentityCache()
        self.guard = lambda: None
        self.status = None
        self.http = None
        self.sender = None

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

    def send_status(self, operation_id):
        self._client.check()
        if self._client._state.sender is None:
            raise not_ready()
        result = self._client._state.sender.store.operation(self._client.identity.robot, operation_id)
        return {k: result[k] for k in ("op_id", "scene", "target", "source", "state", "seq", "result", "error")}

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
        if isinstance(self._state.cache, IdentityCache):
            self._state.cache.items.clear()

    def capabilities(self):
        return {
            "compatibility": "OneBot v11-style OpenID subset; string IDs, not QQ numbers",
            "transport": "implemented" if self._state.http else "not_implemented", "network_api": "not_implemented",
            "basic_scenes": ["group", "c2c", "channel", "dm"] if self._state.sender else [],
            "sending": {"source": "exact event or conservative active policy", "permission": "unknown",
                        "markdown_segment": "nonstandard extension", "max_characters": 4096,
                        "channel_dm": "online WebSocket required", "media": "not_implemented"},
            "actions": {
                **{name: {"support": "unsupported", "reason": "complete_action_contract_unavailable" if self._state.http else "transport_not_ready", "permission": "unknown"} for name in sorted(REMOTE_ACTIONS)},
                **{name: {"support": "unsupported", "reason": "no_equivalent_or_not_implemented", "permission": "unknown"} for name in sorted(UNSUPPORTED_ACTIONS)},
                **{name: {"support": "supported" if name in {"get_status", "get_version_info", "_qq_get_capabilities"} else "partial",
                          "permission": "unknown", "reason": "local_only"} for name in sorted(LOCAL_ACTIONS)},
                **({name: {"support": "conditional", "permission": "unknown", "reason": "observed_route_and_shared_quota_required"}
                    for name in ("send_group_msg", "send_private_msg", "send_msg")} if self._state.sender else {}),
            },
            "identity_cache": "durable, robot/kind/scene/target-scoped chat observations" if self._state.sender else "volatile contract cache",
        }

    async def call_action(self, action, **params):
        self.check()
        if not isinstance(action, str) or not action:
            raise V2Error("invalid_action", "Action must be a nonempty string.")
        if action in {"send_group_msg", "send_private_msg", "send_msg"} and self._state.sender:
            allowed = {"group_id", "message", "auto_escape", "_qq_operation_id"} if action == "send_group_msg" else {"user_id", "message", "auto_escape", "_qq_operation_id"}
            if action == "send_msg":
                allowed |= {"group_id", "message_type"}
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
        if action in REMOTE_ACTIONS:
            if self._state.http:
                raise unsupported("This action needs a complete contract beyond basic message sending.")
            raise not_ready()
        if action in UNSUPPORTED_ACTIONS or action not in LOCAL_ACTIONS:
            raise unsupported("Unknown or unsupported OneBot action.")
        allowed = {
            "get_stranger_info": {"user_id", "no_cache", "id_kind", "scope"},
            "_qq_get_avatar": {"user_id", "group_id", "id_kind", "scope", "size", "kind"},
        }.get(action, set())
        if params.keys() - allowed:
            raise V2Error("invalid_params", "Unsupported action parameters.")
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
            return {"yes": False, "reason": "media_not_implemented" if self._state.http else "transport_not_ready", "permission": "unknown"}
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

    async def send(self, route, message, **options):
        if options.keys() - {"onebot", "auto_escape", "markdown", "operation_id"}:
            raise V2Error("invalid_params", "Unsupported send options.")
        self.check()
        if route.robot != self.identity.robot:
            raise V2Error("identity_mismatch", "Cannot send into another robot's session.", status=409)
        if self._state.sender:
            source = self._source if self._source and route == self._source.route else None
            return await self._state.sender.send(route, message, source=source, **options)
        if self._state.http:
            raise unsupported("This client is not attached to the shared sending service.")
        raise not_ready()
