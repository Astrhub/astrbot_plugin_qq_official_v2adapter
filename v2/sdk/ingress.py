"""Core state consumption and live SDK publication ahead of host queue admission."""

import asyncio
import sqlite3

from ..errors import V2Error
from ..messaging.convert import convert_chat
from ..profiles.observations import merge_chat
from ..protocol import CHAT_EVENTS, RawEnvelope
from .catalog import EVENT_NAMES
from .events import EventContext, NativeEvent, validate_event
from .identifiers import text_id


class CoreConsumer:
    def __init__(self, adapter, *, sleep=asyncio.sleep):
        self.adapter, self.sleep = adapter, sleep
        self.inbox, self.owner_key = adapter.owner.inbox, adapter.identity.settings_key
        self.task = None
        self.closed = False
        self.state, self.last_error = "configured", None
        self.recovered_through = self.inbox.db.execute("SELECT coalesce(max(row_id),0) FROM inbox WHERE owner=?", (self.owner_key,)).fetchone()[0]
        self.published = set()

    def _profile_gap(self, scene=None, scope=None, reason="profile_storage_unavailable"):
        store = self.adapter.owner.profiles
        store.last_error = reason
        try:
            store.mark_gap(self.adapter.identity.robot, scene, scope, reason=reason)
        except sqlite3.Error:
            pass


    def _diagnose(self, item, payload, reason):
        identity = self.adapter.identity
        context = EventContext(identity.platform_id, identity.robot.appid, identity.robot.environment,
            item["received_generation"] or identity.generation, item["transport"], item["shard"],
            item["session_id"], item["received_at"], item["receipt"],
            item["receipt"] <= self.recovered_through, core_state="invalid", host_state="not_applicable")
        try:
            validate_event(payload)
        except V2Error:
            original = payload.get("id")
            payload = {"op": 0, "t": "RETAINED_EVENT", "d": {"receipt": item["receipt"]}}
            if isinstance(original, str) and len(original) <= 512:
                payload["id"] = original
        self.adapter.client._state.events.publish(
            NativeEvent(payload, context, client=self.adapter.client.qq, diagnostic=reason), raw_only=True)


    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="qq-v2-sdk-core-consumer")

    def step(self):
        self.adapter.check_generation()
        pending = self.inbox.core_pending(self.owner_key, 1)
        completed = (None if pending and pending[0]["payload"].get("t") == "INTERACTION_CREATE"
                     else self.inbox.completed_nonchat(self.owner_key))
        if completed:
            payload = completed["payload"]
            if payload.get("t") == "READY":
                data = payload.get("d")
                try:
                    self.adapter.bot_id = text_id(data["user"]["id"])
                except (KeyError, TypeError, V2Error):
                    self.inbox.retain(self.owner_key, completed["receipt"], "invalid_ready", invalid=True)
                    return True
            self.inbox.acknowledge(self.owner_key, completed["receipt"], host=False)
            self.published.discard(completed["receipt"])
            self.state = "core_done"
            return True
        for item in pending:
            receipt, payload = item["receipt"], item["payload"]
            if not isinstance(payload, dict) or payload.get("op") != 0 or not isinstance(payload.get("t"), str):
                self.inbox.retain(self.owner_key, receipt, "invalid_dispatch", invalid=True)
                if isinstance(payload, dict):
                    self._diagnose(item, payload, "invalid_dispatch")
                self.inbox.core_done(self.owner_key, receipt, state="invalid", error="invalid_dispatch")
                return True
            name, data = payload["t"], payload.get("d")
            if name in EVENT_NAMES and name != "RESUMED" and not isinstance(data, dict):
                self.inbox.retain(self.owner_key, receipt, "invalid_event_data", invalid=True)
                self._diagnose(item, payload, "invalid_event_data")
                self.inbox.core_done(self.owner_key, receipt, state="invalid", error="invalid_event_data")
                return True
            reply = None
            error = None
            try:
                validate_event(payload)
                if name in CHAT_EVENTS:
                    chat = convert_chat(self.adapter.identity, RawEnvelope(payload, item["received_at"]),
                        isolated=self.adapter.session_isolated, bot_id=self.adapter.bot_id)
                    self.adapter.owner.messages.observe(chat)
                    if receipt > self.recovered_through and chat.source.expires > self.adapter.owner.messages.now():
                        reply = chat.source
                    try:
                        merge_chat(self.adapter.owner.profiles, chat, payload, item["received_at"])
                    except V2Error as exc:
                        if exc.code != "cache_capacity":
                            raise
                        error = "cache_capacity"
                        self._profile_gap(chat.route.scene, chat.route.target, reason=error)
                    except sqlite3.Error:
                        error = "profile_storage_unavailable"
                        self._profile_gap(chat.route.scene, chat.route.target, reason=error)
                elif name in {"GROUP_MEMBER_ADD", "GROUP_MEMBER_REMOVE"}:
                    if not isinstance(data, dict) or type(data.get("timestamp")) is not int:
                        raise V2Error("invalid_member_event", "QQ member update lacks Unix-second time.")
                    group, user = text_id(data.get("group_openid")), text_id(data.get("member_openid"))
                    alias = data.get("user_openid")
                    if isinstance(alias, str) and alias:
                        alias = text_id(alias)
                    else:
                        alias = None
                    shard = item["shard"]
                    connection = (f"{item['session_id']}:{shard[0]}/{shard[1]}" if item["session_id"] and shard else None)
                    try:
                        self.adapter.owner.profiles.member_event(self.adapter.identity.robot, "group", group, user,
                            "present" if name.endswith("ADD") else "left", data["timestamp"],
                            connection=connection, sequence=payload.get("s"), received=item["received_at"])
                        if alias:
                            self.adapter.owner.profiles.merge(self.adapter.identity.robot, "group", group, user,
                                {"user_openid": alias}, source="member_event",
                                as_of=data["timestamp"], received=item["received_at"])
                    except V2Error as exc:
                        if exc.code != "cache_capacity":
                            raise
                        error = "cache_capacity"
                        self._profile_gap("group", group, reason=error)
                    except sqlite3.Error as exc:
                        error = ("cache_capacity" if getattr(exc, "sqlite_errorcode", None) in
                                 {sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG} else "profile_storage_unavailable")
                        self._profile_gap("group", group, reason=error)
                elif name in {"GUILD_MEMBER_ADD", "GUILD_MEMBER_UPDATE", "GUILD_MEMBER_REMOVE"}:
                    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                        raise V2Error("invalid_member_event", "Guild member update lacks structured user data.")
                    guild = text_id(data.get("guild_id"))
                    user = text_id(data["user"].get("id"))
                    try:
                        nickname = data.get("nick") or data["user"].get("username")
                        self.adapter.owner.profiles.merge(self.adapter.identity.robot, "guild", guild, user,
                            {"nickname": nickname, "avatar_url": data["user"].get("avatar"),
                             "joined_at": data.get("joined_at"), "bot": data["user"].get("bot")},
                            source="guild_member_event", as_of=item["received_at"], received=item["received_at"],
                            kind="channel_user_id")
                        if name != "GUILD_MEMBER_UPDATE":
                            self.adapter.owner.profiles.member_event(self.adapter.identity.robot, "guild", guild, user,
                                "present" if name.endswith("ADD") else "left", int(item["received_at"]),
                                sequence=payload.get("s"), received=item["received_at"])
                    except V2Error as exc:
                        if exc.code != "cache_capacity":
                            raise
                        error = "cache_capacity"
                        self._profile_gap("guild", guild, reason=error)
                    except sqlite3.Error as exc:
                        error = ("cache_capacity" if getattr(exc, "sqlite_errorcode", None) in
                                 {sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG} else "profile_storage_unavailable")
                        self._profile_gap("guild", guild, reason=error)
                elif name == "READY":
                    if not isinstance(data, dict) or not isinstance(data.get("user"), dict):
                        raise V2Error("invalid_ready", "QQ READY lacks user information.")
                    text_id(data.get("session_id"))
                    self.adapter.bot_id = text_id(data["user"].get("id"))
                    try:
                        self.adapter.owner.profiles.mark_gap(self.adapter.identity.robot, reason="new_identify")
                    except sqlite3.Error:
                        error = "profile_storage_unavailable"
                        self._profile_gap(reason=error)
                elif name == "RESUMED":
                    pass  # d may be an empty string.
                elif name in {"INTERACTION_CREATE", "GROUP_JOIN_REQUEST"}:
                    self.adapter.extensions.accept(payload, item["received_at"])
                # Recognized non-chat dispatch is consumed by this SDK lane even without listeners.
            except V2Error as exc:
                if exc.code in {"message_state_full", "extension_capacity", "extension_state_full", "service_stopped", "stale_generation"}:
                    raise
                self.inbox.retain(self.owner_key, receipt, exc.code, invalid=True)
                self._diagnose(item, payload, exc.code)
                self.inbox.core_done(self.owner_key, receipt, state="invalid", error=exc.code)
                self.last_error, self.state = exc.code, "quarantined"
                return True
            if receipt not in self.published:
                identity = self.adapter.identity
                context = EventContext(identity.platform_id, identity.robot.appid, identity.robot.environment,
                    item["received_generation"] or identity.generation, item["transport"], item["shard"],
                    item["session_id"], item["received_at"], receipt, receipt <= self.recovered_through,
                    host_state="pending" if name in CHAT_EVENTS else "not_applicable")
                event = NativeEvent(payload, context, client=self.adapter.client.qq, reply_context=reply)
                self.adapter.client._state.events.publish(event)
                self.published.add(receipt)
            self.inbox.core_done(self.owner_key, receipt, state="degraded" if error else "done", error=error)
            if name not in CHAT_EVENTS:
                if name not in EVENT_NAMES:
                    self.inbox.retain(self.owner_key, receipt, "unknown_event")
                    if network := getattr(self.adapter, "network", None):
                        network.events.retained(name)
                else:
                    self.inbox.acknowledge(self.owner_key, receipt, host=False)
            self.published.discard(receipt)
            self.last_error, self.state = error, "core_done"
            return True
        self.state = "idle"
        return False

    async def run(self):
        while not self.closed:
            try:
                self.adapter.check_generation()
                if self.step():
                    await self.sleep(0)
                    continue
            except sqlite3.Error:
                self.last_error, self.state = "core_storage_unavailable", "backpressured"
            except V2Error as exc:
                if exc.code == "stale_generation":
                    raise
                self.last_error, self.state = exc.code, "backpressured"
            self.inbox.changed.clear()
            try:
                await asyncio.wait_for(self.inbox.changed.wait(), timeout=0.25)
            except TimeoutError:
                pass

    async def close(self):
        self.closed = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
