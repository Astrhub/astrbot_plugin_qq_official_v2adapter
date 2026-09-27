"""Durable callback tickets and an isolated Star handler bridge."""
import copy
import functools
import hashlib
import inspect
import json
import secrets
import weakref
from types import SimpleNamespace

from astrbot.core.config.default import DEFAULT_CONFIG
from astrbot.core.pipeline.context_utils import call_handler
from astrbot.core.pipeline.whitelist_check.stage import WhitelistCheckStage
from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
from astrbot.core.platform.message_type import MessageType
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.session_llm_manager import SessionServiceManager
from astrbot.core.star.session_plugin_manager import SessionPluginManager
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from ..commands import binding_fingerprint
from ..errors import V2Error, unsupported
from ..event import V2MessageEvent
from ..messaging.store import robot_key
from ..models import SessionRoute, text_id
from .events import EventReplySource

_INSTANCES = {}


def _instance_key(instance):
    key = id(instance)
    entry = _INSTANCES.get(key)
    if entry is not None and entry[0]() is instance:
        return entry[1]
    def forget(ref):
        current = _INSTANCES.get(key)
        if current is not None and current[0] is ref:
            _INSTANCES.pop(key, None)
    try:
        ref = weakref.ref(instance, forget)
    except TypeError:
        raise V2Error("callback_unavailable", "Callback Star instance cannot be weakly keyed.", status=409) from None
    token = secrets.token_hex(16)
    _INSTANCES[key] = ref, token
    return token


class CallbackEvent(V2MessageEvent):
    def __init__(self, adapter, interaction, data):
        route = interaction.route(adapter.identity, isolated=adapter.session_isolated)
        source = EventReplySource(route, adapter.identity.generation, interaction.event_id, interaction.interaction_id,
                                  interaction.sent_at, interaction.received_at)
        message = AstrBotMessage()
        message.type = MessageType.GROUP_MESSAGE if route.scene == "group" else MessageType.FRIEND_MESSAGE
        message.session_id = route.public_session(adapter.identity.platform_id, sender=interaction.actor).session_id
        message.self_id, message.message_id = adapter.bot_id, ""
        message.sender = MessageMember(interaction.actor, None)
        message.group_id = route.target if route.scene == "group" else ""
        message.message, message.message_str = [], ""
        message.raw_message = {**copy.deepcopy(interaction.payload), "derived_from_interaction": True}
        message.timestamp, message.v2_source = int(interaction.sent_at), source
        super().__init__(message, adapter.meta(), adapter.client, route)
        self.set_extra("qq_interaction", interaction.metadata())
        self.set_extra("qq_button_data", copy.deepcopy(data))
        self.set_extra("qq_source_kind", "interaction_callback")
        self.should_call_llm(False)

    def should_call_llm(self, call_llm):
        super().should_call_llm(False)


class CallbackTickets:
    def __init__(self, adapter):
        self.adapter, self.store = adapter, adapter.owner.messages
        self.store.db.execute("CREATE TABLE IF NOT EXISTS callback_tickets(token TEXT PRIMARY KEY,robot TEXT NOT NULL,expires REAL NOT NULL,used INTEGER NOT NULL,published INTEGER NOT NULL,operation_id TEXT,body TEXT NOT NULL)")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS callback_ticket_expiry ON callback_tickets(expires)")
        self.store.db.commit()

    def _config(self, route):
        sender = self.store.target(route)["sender"] if route.scene == "dm" else None
        return self.adapter.owner.context.get_config(str(route.public_session(self.adapter.identity.platform_id, sender=sender)))

    def _settings(self):
        self.adapter.check_generation()
        value = self.adapter.owner.store.get(self.adapter.identity.settings_key)
        if not value["applied"].get("extensions", {}).get("keyboard_enabled", False):
            raise V2Error("keyboard_disabled", "Enable QQ V2 keyboards before issuing callback buttons.", status=403)
        return value

    def _handler(self, function=None, full_name=None, name=None):
        handler = star_handlers_registry.get_handler_by_full_name(full_name) if full_name else None
        if function is not None:
            if not inspect.ismethod(function):
                raise V2Error("callback_unavailable", "Pass a bound Star callback method, not a bare function.", status=409)
            candidates = [h for h in star_handlers_registry if h.event_type == EventType.AdapterMessageEvent
                          and h.handler_module_path == function.__func__.__module__
                          and star_handlers_registry.get_handler_by_full_name(h.handler_full_name) is h
                          and type(h.handler) is functools.partial and h.handler.func is function.__func__]
            if len(candidates) != 1:
                raise V2Error("callback_unavailable", "The callback has no unique live Star registration.", status=409)
            handler = candidates[0]
        if handler is None or handler.event_type != EventType.AdapterMessageEvent or not handler.enabled or star_handlers_registry.get_handler_by_full_name(handler.handler_full_name) is not handler:
            raise V2Error("callback_unavailable", "The registered callback is no longer active.", status=409)
        plugin = star_map.get(handler.handler_module_path)
        bound = handler.handler
        if (not plugin or not plugin.activated or plugin.star_cls is None or
                handler.handler_full_name not in plugin.star_handler_full_names or
                type(bound) is not functools.partial or len(bound.args) != 1 or
                bound.args[0] is not plugin.star_cls or bound.keywords):
            raise V2Error("callback_unavailable", "The callback Star instance is not active.", status=409)
        if function is not None and function.__self__ is not plugin.star_cls:
            raise V2Error("callback_unavailable", "A callback must belong to its live Star instance.", status=409)
        callback_name = getattr(bound.func, "__qq_v2_callback_name__", None)
        if not callback_name or name is not None and callback_name != name or any(isinstance(f, (CommandFilter, CommandGroupFilter)) for f in handler.event_filters):
            raise V2Error("callback_unavailable", "The registered callback declaration changed.", status=409)
        fingerprint = binding_fingerprint(plugin, handler, [], [])
        if not fingerprint:
            raise V2Error("callback_unavailable", "Callback binding cannot be verified.", status=409)
        return plugin, handler, callback_name, fingerprint, _instance_key(plugin.star_cls)

    def _contract(self, body):
        settings = self._settings()
        identity = self.adapter.identity
        if body["platform"] != identity.platform_id or body["generation"] != identity.generation or body["revision"] != settings["applied_revision"]:
            raise V2Error("ticket_stale", "Callback platform, generation or settings changed.", status=409)
        route = SessionRoute.decode(body["route"])
        if route.robot != identity.robot or route.scene not in {"group", "c2c"}:
            raise V2Error("identity_mismatch", "Callback ticket belongs to another robot.", status=403)
        plugin, handler, _, fingerprint, instance_key = self._handler(full_name=body["handler"], name=body["name"])
        if (plugin.name, fingerprint, instance_key) != (body["plugin"], body["binding"], body["instance"]):
            raise V2Error("ticket_stale", "Callback Star was reloaded or replaced.", status=409)
        config = self._config(route)
        allowed = config.get("plugin_set", ["*"])
        if allowed != ["*"] and plugin.name not in allowed and not plugin.reserved:
            raise V2Error("callback_unavailable", "Callback plugin is not enabled for this session.", status=403)
        return route, plugin, handler, config

    def issue(self, route, actor, function, *, label, data=None, audience="actor"):
        settings = self._settings()
        if route.robot != self.adapter.identity.robot or route.scene not in {"group", "c2c"}:
            raise unsupported("Callbacks require an owned group/C2C event route.")
        actor = text_id(actor)
        if not isinstance(label, str) or not 1 <= len(label) <= 10 or audience not in {"actor", "all"}:
            raise V2Error("invalid_callback", "Use a short button label and an explicit actor or all audience.")
        if data is not None:
            try:
                data = json.loads(json.dumps(data, ensure_ascii=False, allow_nan=False))
                encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
            except (TypeError, ValueError, RecursionError):
                raise V2Error("invalid_callback", "Callback data must be bounded JSON.") from None
            if len(encoded.encode()) > 2048:
                raise V2Error("invalid_callback", "Callback data exceeds 2 KiB.")
        plugin, handler, name, fingerprint, instance_key = self._handler(function=function)
        config = self._config(route)
        allowed = config.get("plugin_set", ["*"])
        if allowed != ["*"] and plugin.name not in allowed and not plugin.reserved:
            raise V2Error("callback_unavailable", "Callback plugin is disabled for this session.", status=403)
        permission = {"type": 0, "specify_user_ids": [actor]} if audience == "actor" else {"type": 2}
        body = {"platform": self.adapter.identity.platform_id, "generation": self.adapter.identity.generation,
                "route": route.encode(), "actor": actor, "audience": audience, "plugin": plugin.name,
                "handler": handler.handler_full_name, "name": name, "binding": fingerprint,
                "instance": instance_key, "revision": settings["applied_revision"], "data": data}
        token = "qv2cb." + secrets.token_urlsafe(24)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM callback_tickets WHERE expires<=?", (self.store.now(),))
            if self.store.db.execute("SELECT count(*) FROM callback_tickets WHERE robot=?", (robot_key(route.robot),)).fetchone()[0] >= 4096:
                raise V2Error("ticket_capacity", "Callback tickets reached their bounded capacity.", status=429)
            self.store.db.execute("INSERT INTO callback_tickets VALUES(?,?,?,?,?,?,?)", (hashlib.sha256(token.encode()).hexdigest(), robot_key(route.robot), self.store.now() + settings["applied"]["extensions"].get("ticket_ttl", 120), 0, 0, None, json.dumps(body, ensure_ascii=False)))
        return {"id": secrets.token_hex(6), "render_data": {"label": label, "style": 1},
                "action": {"type": 1, "permission": permission, "data": token}}

    def _lookup(self, token):
        if not isinstance(token, str) or not token.startswith("qv2cb.") or len(token) > 80:
            raise V2Error("unowned_callback", "Callback is not an issued button ticket.", status=404)
        key = hashlib.sha256(token.encode()).hexdigest()
        row = self.store.db.execute("SELECT * FROM callback_tickets WHERE token=? AND robot=?", (key, robot_key(self.adapter.identity.robot))).fetchone()
        if not row or row["used"] or row["expires"] <= self.store.now():
            raise V2Error("ticket_unavailable", "Callback ticket expired or was consumed.", status=409)
        return key, row, json.loads(row["body"])

    def validate(self, route, token, permission=None, *, allow_published=False, operation_id=None):
        _, row, body = self._lookup(token)
        if row["published"] and (not allow_published or row["operation_id"] != operation_id):
            raise V2Error("ticket_already_published", "Callback ticket cannot be attached to another message.", status=409)
        expected, _, _, _ = self._contract(body)
        if route != expected:
            raise V2Error("ticket_scope_mismatch", "Callback button cannot cross event routes.", status=403)
        wire = {"type": 0, "specify_user_ids": [body["actor"]]} if body["audience"] == "actor" else {"type": 2}
        if permission is not None and permission != wire:
            raise V2Error("ticket_scope_mismatch", "Callback wire permission differs from the local ticket.", status=403)
        return body

    def publish(self, tokens, operation_id):
        with self.store.transaction():
            changed = []
            for token in tokens:
                key = hashlib.sha256(token.encode()).hexdigest()
                row = self.store.db.execute("SELECT used,published,operation_id FROM callback_tickets WHERE token=? AND robot=?",
                                            (key, robot_key(self.adapter.identity.robot))).fetchone()
                if not row or row["used"] or row["operation_id"] not in (None, operation_id):
                    raise V2Error("ticket_already_published", "Callback ticket is bound to another send operation.", status=409)
                changed.append(not row["published"])
                self.store.db.execute("UPDATE callback_tickets SET published=1,operation_id=? WHERE token=?", (operation_id, key))
        return all(changed)

    def revoke(self, tokens, *, definite=False, operation_id=None):
        if self.store.closed:
            return
        with self.store.transaction():
            clause = " AND operation_id=?" if definite else " AND published=0"
            args = ((hashlib.sha256(t.encode()).hexdigest(), operation_id) if definite
                    else (hashlib.sha256(t.encode()).hexdigest(),) for t in tokens)
            self.store.db.executemany("DELETE FROM callback_tickets WHERE token=? AND used=0" + clause, args)

    def redeem(self, token, event):
        with self.store.transaction():
            key, row, body = self._lookup(token)
            route, plugin, handler, config = self._contract(body)
            if (route.scene, route.target) != (event.scene, event.target) or body["audience"] == "actor" and event.actor != body["actor"]:
                raise V2Error("ticket_scope_mismatch", "Callback actor or destination changed.", status=403)
            if not row["published"]:
                raise V2Error("ticket_unpublished", "Button was not submitted for delivery.", status=409)
            self.store.db.execute("UPDATE callback_tickets SET used=1 WHERE token=? AND used=0", (key,))
        return body, plugin, handler, config

    async def dispatch(self, event, token):
        body, plugin, handler, _ = self.redeem(token, event)
        projected = CallbackEvent(self.adapter, event, body["data"])
        try:
            config = self._config(projected.route)
            projected.role = "admin" if event.actor in config.get("admins_id", []) else "member"
            if not await SessionServiceManager.is_session_enabled(projected.unified_msg_origin):
                raise V2Error("callback_session_disabled", "The host disabled this session.", status=403)
            settings = {**DEFAULT_CONFIG["platform_settings"], **config.get("platform_settings", {})}
            gate = WhitelistCheckStage()
            await gate.initialize(SimpleNamespace(astrbot_config={"platform_settings": settings}))
            await gate.process(projected)
            if projected.is_stopped():
                raise V2Error("callback_session_denied", "The host session allowlist denied this callback.", status=403)
            allowed = config.get("plugin_set", ["*"])
            if allowed != ["*"] and plugin.name not in allowed and not plugin.reserved:
                raise V2Error("callback_unavailable", "Callback plugin is disabled for this session.", status=403)
            projected.set_extra("_session_isolated", self.adapter.session_isolated)
            self._contract(body)
            if not await SessionPluginManager.filter_handlers_by_session(projected, [handler]):
                raise V2Error("callback_unavailable", "Plugin is disabled for this session.", status=403)
            for item in handler.event_filters:
                if not item.filter(projected, config):
                    raise V2Error("callback_filtered", "The registered callback filter denied this event.", status=403)
            self._contract(body)
            self.store.register_event_source(projected.bot._source)
            async for _ in call_handler(projected, handler.handler):
                result = projected.get_result()
                if result and result.chain:
                    await projected.send(result)
                projected.clear_result()
            return "finished_unconfirmed"
        finally:
            projected.cleanup_temporary_local_files()
