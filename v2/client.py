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

    def check(self, generation):
        if self.closed or generation != self.identity.generation:
            raise V2Error("stale_generation", "The source instance has stopped or changed.", status=409)


class NativeView:
    def __init__(self, client):
        self._client = client

    def avatar_url(self, openid, size=100):
        self._client.check()
        return avatar_url(self._client.identity.robot, openid, size)

    user_avatar_url = avatar_url
    group_avatar_url = avatar_url

    async def request(self, *args, **kwargs):
        self._client.check()
        raise not_ready()


class V2Client:
    def __init__(self, identity, *, state=None, route=None):
        self.identity = identity
        self._state = state or ClientState(identity)
        self._route = route
        self.api = self
        self.qq = NativeView(self)

    def check(self):
        self._state.check(self.identity.generation)

    def bind(self, route: SessionRoute):
        self.check()
        if route.robot != self.identity.robot:
            raise V2Error("identity_mismatch", "Session belongs to another robot.", status=409)
        return V2Client(self.identity, state=self._state, route=route)

    async def close(self):
        self._state.closed = True
        self._state.cache.items.clear()

    def capabilities(self):
        return {
            "compatibility": "OneBot v11-style OpenID subset; string IDs, not QQ numbers",
            "transport": "not_implemented", "network_api": "not_implemented",
            "actions": {
                **{name: {"support": "unsupported", "reason": "transport_not_ready", "permission": "unknown"} for name in sorted(REMOTE_ACTIONS)},
                **{name: {"support": "unsupported", "reason": "no_equivalent_or_not_implemented", "permission": "unknown"} for name in sorted(UNSUPPORTED_ACTIONS)},
                **{name: {"support": "supported" if name in {"get_status", "get_version_info", "_qq_get_capabilities"} else "partial",
                          "permission": "unknown", "reason": "local_only"} for name in sorted(LOCAL_ACTIONS)},
            },
            "identity_cache": "chat observations only; volatile P0 boundary, no live ingestion",
        }

    async def call_action(self, action, **params):
        self.check()
        if not isinstance(action, str) or not action:
            raise V2Error("invalid_action", "Action must be a nonempty string.")
        if action in REMOTE_ACTIONS:
            raise not_ready()
        if action in UNSUPPORTED_ACTIONS or action not in LOCAL_ACTIONS:
            raise unsupported("Unknown or unsupported OneBot action.")
        allowed = {
            "get_stranger_info": {"user_id", "no_cache", "id_kind", "scope"},
            "_qq_get_avatar": {"user_id", "id_kind", "scope", "size", "kind"},
        }.get(action, set())
        if params.keys() - allowed:
            raise V2Error("invalid_params", "Unsupported action parameters.")
        if action == "get_status":
            return {"online": False, "good": False, "state": "transport_not_ready",
                    "platform_id": self.identity.platform_id, "generation": self.identity.generation}
        if action == "get_version_info":
            return {"app_name": PLATFORM_TYPE, "app_version": VERSION, "protocol_version": "v11-openid-subset"}
        if action == "_qq_get_capabilities":
            return self.capabilities()
        if action in ("can_send_image", "can_send_record"):
            return {"yes": False, "reason": "transport_not_ready", "permission": "unknown"}
        if params.get("no_cache", False) is not False:
            raise unsupported("A live OpenID profile lookup is not available.")
        user_id = text_id(params.get("user_id"))
        kind, scope = params.get("id_kind"), params.get("scope")
        if kind is None and scope is None and self._route and self._route.scene == "c2c":
            kind, scope = "user_openid", f"c2c:{self._route.target}"
        if not isinstance(kind, str) or not isinstance(scope, str):
            raise V2Error("identity_scope_required", "Explicit id_kind and scope are required outside bound C2C.")
        record = self._state.cache.lookup(self.identity.robot, kind, scope, user_id)
        if action == "get_stranger_info":
            return {**record, "source": "chat_cache", "partial": True}
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

    async def send(self, route, message):
        self.check()
        if route.robot != self.identity.robot:
            raise V2Error("identity_mismatch", "Cannot send into another robot's session.", status=409)
        raise not_ready()
