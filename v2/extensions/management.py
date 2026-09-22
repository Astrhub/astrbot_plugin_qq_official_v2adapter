"""Named QQ operations with complete pagination and non-replayable write receipts."""
import asyncio
import copy
import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit
from uuid import uuid4

from ..errors import V2Error, unsupported
from ..messaging.store import robot_key
from ..models import text_id
from ..protocol import RequestSpec

MANAGEMENT_ACTIONS = {"get_group_info", "get_group_member_info", "get_group_member_list", "get_login_info",
                      "set_group_ban", "set_group_kick", "set_group_add_request", "delete_msg"}
NATIVE_ACTIONS = {"group_info", "bot_state", "group_member", "group_members", "group_ban", "group_kick", "group_mutes",
                  "join_requests", "approve", "login_info", "share", "delete_message", "guild_info", "channels",
                  "channel_info", "channel_create", "channel_update", "channel_delete", "guild_member", "guild_members",
                  "guild_kick", "guild_mute", "guild_mute_batch"}


def ident(value):
    return quote(text_id(value), safe="")


def invalid(message="QQ response does not satisfy the required operation contract."):
    raise V2Error("invalid_management_response", message, status=502)


def empty(data):
    if data not in (None, {}):
        invalid()
    return {"state": "succeeded"}


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError
        return result.timestamp()
    except (AttributeError, ValueError, OverflowError):
        invalid("A real timezone-aware timestamp is required.")


@asynccontextmanager
async def read_deadline():
    try:
        async with asyncio.timeout(120):
            yield
    except TimeoutError:
        raise V2Error("management_deadline", "The bounded read timed out; no partial list was returned.", status=504) from None


class Management:
    def __init__(self, identity, http, state, store, *, settings=lambda: {}, sleep=asyncio.sleep):
        self.identity, self.http, self.state, self.store = identity, http, state, store
        self.settings, self.sleep = settings, sleep
        self.tasks, self.closed = set(), False
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS join_flags(robot TEXT,target TEXT,member TEXT,request_id TEXT,
            flag_hash TEXT,expires REAL,observed REAL,state TEXT,op_id TEXT,
            PRIMARY KEY(robot,target,member,request_id))""")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS join_flag_pending_expiry ON join_flags(expires) WHERE state IN ('pending','retryable')")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS join_flag_history_age ON join_flags(observed) WHERE state IN ('succeeded','observed_approved')")
        self.store.db.execute("CREATE INDEX IF NOT EXISTS join_flag_protected ON join_flags(state) WHERE state NOT IN ('succeeded','observed_approved')")
        self.store.db.commit()

    def check(self, *, write=False):
        if self.closed:
            raise V2Error("service_stopped", "Management service is stopped.", status=503)
        self.http.check()
        if write and not self.settings().get("management_writes", False):
            raise V2Error("management_disabled", "Enable named management writes in the advanced WebUI first.", status=403)

    async def _read(self, path, *, params=None, kind, limit=30, seconds=60):
        self.check()
        task = asyncio.current_task()
        if task not in self.tasks and len(self.tasks) >= 16:
            raise V2Error("management_capacity", "Too many management requests.", status=429)
        self.tasks.add(task)
        try:
            async with read_deadline():
                delay = self.state.rate_delay(self.identity.robot, kind, limit, seconds)
                if delay:
                    await self.sleep(delay)
                def before():
                    self.check()
                    self.state.rate(self.identity.robot, kind, limit, seconds)
                return (await self.http.request(RequestSpec(self.identity.robot.environment, "GET", path, params=params), before_send=before)).data
        finally:
            self.tasks.discard(task)

    async def _write(self, method, path, body, *, kind, operation_id=None, validate=empty, params=None, limit=30, seconds=60, guard=lambda: None):
        self.check(write=True)
        body, params = copy.deepcopy(body), copy.deepcopy(params)
        task = asyncio.current_task()
        if task not in self.tasks and len(self.tasks) >= 16:
            raise V2Error("management_capacity", "Too many management writes.", status=429)
        self.tasks.add(task)
        try:
            def before():
                self.check(write=True)
                guard()
            return await self.state.execute(self.http, RequestSpec(self.identity.robot.environment, method, path, json_body=body, params=params),
                op_id=text_id(operation_id) if operation_id else uuid4().hex, kind=kind, validate=validate, before_send=before, rate=(kind, limit, seconds),
                context={"request": body, "params": params} if kind != "share" else {})
        finally:
            self.tasks.discard(task)

    async def group_info(self, group):
        data = await self._read(f"/v2/groups/{ident(group)}/info", kind="group_info")
        if not isinstance(data, dict) or data.get("group_openid") != group or not isinstance(data.get("group_name"), str) or type(data.get("group_member_num")) is not int or data["group_member_num"] < 0:
            invalid()
        return data

    async def bot_state(self, group):
        data = await self._read(f"/v2/groups/{ident(group)}/bot_state", kind="bot_state")
        if not isinstance(data, dict) or data.get("member_role") not in ("member", "admin", "owner"):
            invalid()
        text_id(data.get("member_openid"))
        return data

    async def _admin(self, group):
        if (await self.bot_state(group))["member_role"] not in {"admin", "owner"}:
            raise V2Error("group_admin_required", "The current bot-state response does not grant an administrator role.", status=403)

    @staticmethod
    def _member(data, expected=None):
        if not isinstance(data, dict) or not isinstance(data.get("username"), str) or data.get("member_role") not in ("member", "admin", "owner") or type(data.get("bot")) is not bool:
            invalid()
        user = text_id(data.get("member_openid"))
        if expected is not None and user != expected:
            invalid("Member response belongs to another requested identity.")
        return data

    async def group_member(self, group, user):
        return self._member(await self._read(f"/v2/groups/{ident(group)}/members/{ident(user)}", kind="group_member"), user)

    async def _pages(self, path, *, kind, field, page_limit, limit, key):
        cursor, cursors, rows, seen, size = "", set(), [], set(), 0
        async with read_deadline():
            for _ in range(200):
                data = await self._read(path, params={"cursor": cursor}, kind=kind, limit=limit)
                if not isinstance(data, dict) or not isinstance(data.get(field), list) or len(data[field]) > page_limit or not isinstance(data.get("next_cursor"), str) or len(data["next_cursor"]) > 4096:
                    invalid()
                size += len(json.dumps(data).encode())
                if size > 4 * 1024 * 1024:
                    raise V2Error("pagination_incomplete", "Complete listing exceeds its retained byte bound.", status=413)
                for row in data[field]:
                    if not isinstance(row, dict):
                        invalid()
                    identity = text_id(row.get(key))
                    if identity not in seen:
                        seen.add(identity)
                        rows.append(row)
                    if len(rows) > 5000:
                        raise V2Error("pagination_incomplete", "Complete listing exceeds its item bound.", status=413)
                cursor = data["next_cursor"]
                if not cursor:
                    return rows
                if cursor in cursors:
                    raise V2Error("pagination_incomplete", "QQ repeated a pagination cursor; no partial list was returned.", status=502)
                cursors.add(cursor)
        raise V2Error("pagination_incomplete", "Complete listing exceeded the page budget.", status=502)

    async def group_members(self, group):
        rows = await self._pages(f"/v2/groups/{ident(group)}/members", kind="group_members", field="members", page_limit=30, limit=60, key="member_openid")
        return [self._member(row) for row in rows]

    async def group_mutes(self, group):
        data = await self._read(f"/v2/groups/{ident(group)}/restrict_chat_setting", kind="group_mutes")
        if not isinstance(data, dict) or not isinstance(data.get("members"), list):
            invalid()
        return data

    async def group_ban(self, group, user, duration, *, operation_id=None):
        ident(group)
        ident(user)
        if type(duration) is not int or not 0 <= duration <= 30 * 86400:
            raise V2Error("invalid_duration", "Group mute duration must be 0..2592000 seconds.")
        self.check(write=True)
        await self._admin(group)
        op = "del"
        if duration:
            member = await self.group_member(group, user)
            if member["member_role"] != "member" or member["bot"]:
                raise V2Error("member_not_mutable", "Only ordinary non-bot group members can be muted.", status=403)
            data = await self._read(f"/v2/groups/{ident(group)}/restrict_chat_setting", kind="group_mutes")
            if not isinstance(data, dict) or not isinstance(data.get("members"), list):
                invalid()
            for row in data["members"]:
                if not isinstance(row, dict):
                    invalid()
                text_id(row.get("member_openid"))
            op = "update" if any(row["member_openid"] == user for row in data["members"]) else "add"
        expiry = datetime.fromtimestamp(self.store.now() + duration, UTC).isoformat() if duration else ""
        return await self._write("POST", f"/v2/groups/{ident(group)}/restrict_chat_setting", {"members": [{"op": op, "member_openid": user, "mute_expire_at": expiry}]}, kind="group_ban", operation_id=operation_id, limit=60)

    async def group_kick(self, group, users, *, blacklist=False, operation_id=None):
        ident(group)
        if not isinstance(users, list) or not 1 <= len(users) <= 20 or type(blacklist) is not bool:
            raise V2Error("invalid_members", "Remove 1..20 explicit members and supply a boolean blacklist choice.")
        for user in users:
            ident(user)
        if len(set(users)) != len(users):
            raise V2Error("invalid_members", "Duplicate members are not accepted.")
        users = list(users)
        self.check(write=True)
        await self._admin(group)
        def validate(data):
            if not isinstance(data, dict) or data.get("remove_members_result") != "success" or not isinstance(data.get("add_to_member_blacklist_fail_openids"), list):
                invalid()
            failed = data["add_to_member_blacklist_fail_openids"]
            if any(not isinstance(user, str) or user not in users for user in failed):
                invalid()
            if failed:
                raise V2Error("partial_failure", "Removal succeeded but some blacklist writes failed; do not replay removal.", status=502, phase="partial", details={"blacklist_failed": failed})
            return {"state": "succeeded", "removed": users}
        return await self._write("POST", f"/v2/groups/{ident(group)}/batch_remove_members", {"member_openids": users, "add_to_member_blacklist": blacklist}, kind="group_kick", operation_id=operation_id, validate=validate)

    def observe_request(self, group, data, *, received=None, fresh_read=False):
        self.check()
        if not isinstance(data, dict) or data.get("apply_source") not in ("self_apply", "invited"):
            return None
        if data.get("auto_approved") is not None:
            with self.store.transaction():
                self.store.db.execute("UPDATE join_flags SET state='observed_approved' WHERE robot=? AND target=? AND member=? AND request_id=?",
                    (robot_key(self.identity.robot), text_id(group), text_id(data.get("member_openid")), text_id(data.get("join_request_id"))))
            return None
        key = robot_key(self.identity.robot), text_id(group), text_id(data.get("member_openid")), text_id(data.get("join_request_id"))
        now = self.store.now()
        applied = timestamp(data.get("apply_at"))
        if applied > now + 30:
            invalid("Application timestamp is in the future.")
        received = now if received is None else min(now, received)
        with self.store.transaction():
            self.store.db.execute("DELETE FROM join_flags WHERE expires<=? AND state IN ('pending','retryable')", (now,))
            self.store.db.execute("DELETE FROM join_flags WHERE observed<=? AND expires<=? AND state IN ('succeeded','observed_approved')", (now - 86400, now))
            old = self.store.db.execute("SELECT state,observed FROM join_flags WHERE robot=? AND target=? AND member=? AND request_id=?", key).fetchone()
            if old and (old["state"] not in {"pending", "retryable"} or not fresh_read):
                return None
            # Settled request fences do not occupy the live/uncertain approval budget.
            if not old and self.store.db.execute("SELECT count(*) FROM join_flags WHERE state NOT IN ('succeeded','observed_approved')").fetchone()[0] >= 4096:
                raise V2Error("request_flag_capacity", "Request flags hold capacity; attempted approvals were retained.", status=503)
            flag = secrets.token_urlsafe(24)
            self.store.db.execute("INSERT OR REPLACE INTO join_flags VALUES(?,?,?,?,?,?,?,?,?)", (*key, hashlib.sha256(flag.encode()).hexdigest(), received + 300, received, "pending", None))
        return flag

    async def join_requests(self, group):
        rows = await self._pages(f"/v2/groups/{ident(group)}/join_request_list", kind="join_requests", field="list", page_limit=50, limit=30, key="join_request_id")
        return [{**row, "flag": self.observe_request(group, row, fresh_read=True)} for row in rows]

    async def approve(self, flag, *, approve, reason="", blacklist=False):
        self.check(write=True)
        if type(approve) is not bool or type(blacklist) is not bool or not isinstance(reason, str) or len(reason) > 200 or approve and (reason or blacklist):
            raise V2Error("invalid_approval", "Approval requires explicit booleans; rejection-only fields cannot accompany approval.")
        if not isinstance(flag, str) or not 1 <= len(flag) <= 512:
            raise V2Error("request_flag_invalid", "Use an observed, unexpired request flag.", status=404)
        robot = robot_key(self.identity.robot)
        row = self.store.db.execute("SELECT * FROM join_flags WHERE robot=? AND flag_hash=?", (robot, hashlib.sha256(flag.encode()).hexdigest())).fetchone()
        if not row or row["expires"] <= self.store.now() or row["state"] != "pending":
            raise V2Error("request_flag_invalid", "Use an observed, unexpired, unused request flag for this robot.", status=404)
        await self._admin(row["target"])
        op_id = "approve-" + row["flag_hash"]
        body = {"op": "approve" if approve else "decline", "join_request_id": row["request_id"]}
        if not approve:
            body.update({"reject_reason": reason, "add_to_member_blacklist": blacklist})
        def claim():
            with self.store.transaction():
                count = self.store.db.execute("UPDATE join_flags SET state='attempted',op_id=? WHERE robot=? AND flag_hash=? AND expires>? AND (state='pending' OR (state='attempted' AND op_id=?))", (op_id, robot, row["flag_hash"], self.store.now(), op_id)).rowcount
                if count != 1:
                    raise V2Error("request_flag_invalid", "Request flag changed or expired before sending.", status=409)
        try:
            result = await self._write("POST", f"/v2/groups/{ident(row['target'])}/approval_join_request/{ident(row['member'])}", body, kind="approve_request", operation_id=op_id, limit=60, guard=claim)
        except V2Error as exc:
            if exc.phase in {"not_sent", "rejected"}:
                with self.store.transaction():
                    self.store.db.execute("UPDATE join_flags SET state='retryable' WHERE robot=? AND flag_hash=? AND state IN ('pending','attempted')", (robot, row["flag_hash"]))
            raise
        with self.store.transaction():
            self.store.db.execute("UPDATE join_flags SET state='succeeded' WHERE robot=? AND flag_hash=? AND state='attempted'", (robot, row["flag_hash"]))
        return result

    async def login_info(self):
        data = await self._read("/users/@me", kind="profile", limit=50, seconds=1)
        if not isinstance(data, dict) or data.get("bot") is not True or not isinstance(data.get("username"), str) or not data["username"]:
            invalid("A complete real bot ID and username are required for login_info.")
        return {"user_id": text_id(data.get("id")), "nickname": data["username"], "id_kind": "channel_user_id", "source": "official_profile"}

    async def share(self, callback_data="", *, operation_id=None):
        if not isinstance(callback_data, str) or len(callback_data) > 32:
            raise V2Error("invalid_callback_data", "Share callback_data is at most 32 characters.")
        def validate(data):
            value = data.get("data", {}).get("url") if isinstance(data, dict) and isinstance(data.get("data"), dict) else None
            try:
                parsed = urlsplit(value)
                if not isinstance(value, str) or len(value) > 8192 or parsed.scheme != "https" or parsed.hostname != "qun.qq.com" or parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443) or any(ord(c) < 32 for c in value):
                    raise ValueError
            except (ValueError, TypeError):
                invalid("QQ did not return a valid official share link.")
            return {"url": value}
        return await self._write("POST", "/v2/generate_url_link", {"callback_data": callback_data}, kind="share", operation_id=operation_id, validate=validate, limit=50, seconds=1)

    async def delete_message(self, message_id, *, operation_id=None):
        text_id(message_id)
        rows = self.store.db.execute("SELECT scene,target,started,result FROM operations WHERE robot=? AND state='sent' AND json_extract(result,'$.message_id')=?", (robot_key(self.identity.robot), message_id)).fetchall()
        routes = {(r["scene"], r["target"]) for r in rows}
        if len(routes) != 1:
            raise V2Error("message_route_unavailable", "A unique retained real send route is required; reference indices are not deletion IDs.", status=404)
        scene, target = next(iter(routes))
        started = min(json.loads(r["result"]).get("wire_started", r["started"]) for r in rows)
        def deadline():
            if scene in {"c2c", "group"} and self.store.now() >= started + 120:
                raise V2Error("delete_expired", "The conservative two-minute deletion deadline has expired.")
        deadline()
        path = {"c2c": "/v2/users/", "group": "/v2/groups/", "channel": "/channels/", "dm": "/dms/"}[scene] + ident(target) + "/messages/" + ident(message_id)
        return await self._write("DELETE", path, None, kind="delete_message", operation_id=operation_id or "delete-" + hashlib.sha256((scene + target + message_id).encode()).hexdigest(),
                                 params={"hidetip": "false"} if scene in {"channel", "dm"} else None, guard=deadline, limit=10, seconds=1)

    async def onebot(self, action, params):
        from ..client import ACTION_PARAMS
        allowed = ACTION_PARAMS
        if action not in MANAGEMENT_ACTIONS or params.keys() - allowed[action]:
            raise V2Error("invalid_params", "Unsupported management action parameters.")
        if "no_cache" in params and type(params["no_cache"]) is not bool:
            raise V2Error("invalid_params", "no_cache must be boolean; official reads are always fresh.")
        if action == "get_login_info":
            return await self.login_info()
        if action == "delete_msg":
            return await self.delete_message(params.get("message_id"), operation_id=params.get("_qq_operation_id"))
        if action == "set_group_add_request":
            if params.get("sub_type", "add") != "add":
                raise unsupported("Observed member applications map to add requests, not invitations for the bot to join.")
            return await self.approve(params.get("flag"), approve=params.get("approve", True), reason=params.get("reason", ""))
        group = text_id(params.get("group_id"))
        if action == "get_group_info":
            data = await self.group_info(group)
            return {"group_id": data["group_openid"], "group_name": data["group_name"], "member_count": data["group_member_num"], "partial": True, "permission": "unknown"}
        if action in {"get_group_member_info", "get_group_member_list"}:
            rows = [await self.group_member(group, text_id(params.get("user_id")))] if action.endswith("info") else await self.group_members(group)
            result = [{"group_id": group, "user_id": row["member_openid"], "nickname": row["username"], "role": row["member_role"], "bot": row["bot"], "partial": True, "id_kind": "member_openid"} for row in rows]
            return result[0] if action.endswith("info") else result
        user = text_id(params.get("user_id"))
        if action == "set_group_ban":
            return await self.group_ban(group, user, params.get("duration", 1800), operation_id=params.get("_qq_operation_id"))
        return await self.group_kick(group, [user], blacklist=params.get("reject_add_request", False), operation_id=params.get("_qq_operation_id"))

    async def guild_info(self, guild):
        data = await self._read(f"/guilds/{ident(guild)}", kind="guild_info", limit=50, seconds=1)
        if not isinstance(data, dict) or data.get("id") != guild:
            invalid()
        return data

    async def channels(self, guild):
        data = await self._read(f"/guilds/{ident(guild)}/channels", kind="channels", limit=50, seconds=1)
        rows = data.get("channels") if isinstance(data, dict) else data
        if not isinstance(rows, list) or len(rows) > 5000:
            invalid()
        for row in rows:
            if not isinstance(row, dict) or row.get("guild_id") != guild:
                invalid()
            text_id(row.get("id"))
        return rows

    async def channel_info(self, channel):
        data = await self._read(f"/channels/{ident(channel)}", kind="channel_info", limit=50, seconds=1)
        if not isinstance(data, dict) or data.get("id") != channel:
            invalid()
        return data

    async def channel_create(self, guild, fields, *, operation_id=None):
        self._channel_fields(fields, create=True)
        def validate(data):
            if not isinstance(data, dict) or data.get("guild_id") != guild:
                invalid()
            text_id(data.get("id"))
            return data
        return await self._write("POST", f"/guilds/{ident(guild)}/channels", fields, kind="channel_create", operation_id=operation_id, validate=validate, limit=50, seconds=1)

    async def channel_update(self, channel, fields, *, operation_id=None):
        self._channel_fields(fields, create=False)
        def validate(data):
            if not isinstance(data, dict) or data.get("id") != channel:
                invalid()
            return data
        return await self._write("PATCH", f"/channels/{ident(channel)}", fields, kind="channel_update", operation_id=operation_id, validate=validate, limit=50, seconds=1)

    @staticmethod
    def _channel_fields(fields, *, create):
        allowed = {"name", "position", "parent_id", "private_type", "speak_permission"}
        if create:
            allowed |= {"type", "sub_type", "private_user_ids", "application_id"}
        if not isinstance(fields, dict) or not fields or fields.keys() - allowed:
            raise V2Error("invalid_channel_fields", "Only documented channel fields are accepted.")
        if "name" in fields and (not isinstance(fields["name"], str) or not 1 <= len(fields["name"]) <= 100):
            raise V2Error("invalid_channel_fields", "Channel name is outside the local 1..100 character bound.")
        for key, values in {"type": {0, 2, 4, 10005, 10006, 10007}, "sub_type": {0, 1, 2, 3}, "private_type": {0, 1, 2}, "speak_permission": {0, 1, 2}}.items():
            if key in fields and (type(fields[key]) is not int or fields[key] not in values):
                raise V2Error("invalid_channel_fields", "Channel enum value is invalid.")
        if "position" in fields and (type(fields["position"]) is not int or not (2 if fields.get("type") == 4 else 1) <= fields["position"] <= 10000):
            raise V2Error("invalid_channel_fields", "Channel position is invalid.")
        for key in ("parent_id", "application_id"):
            if key in fields:
                text_id(fields[key])
        if "private_user_ids" in fields:
            users = fields["private_user_ids"]
            if not isinstance(users, list) or len(users) > 100:
                raise V2Error("invalid_channel_fields", "Private users exceed the local bound.")
            for user in users:
                text_id(user)

    async def channel_delete(self, channel, *, operation_id=None):
        return await self._write("DELETE", f"/channels/{ident(channel)}", None, kind="channel_delete", operation_id=operation_id, limit=50, seconds=1)

    async def guild_member(self, guild, user):
        data = await self._read(f"/guilds/{ident(guild)}/members/{ident(user)}", kind="guild_member", limit=50, seconds=1)
        if not isinstance(data, dict) or not isinstance(data.get("user"), dict) or data["user"].get("id") != user:
            invalid()
        return data

    async def guild_members(self, guild):
        after, cursors, seen, rows, size = "0", set(), set(), [], 0
        async with read_deadline():
            for _ in range(200):
                page = await self._read(f"/guilds/{ident(guild)}/members", params={"after": after, "limit": 400}, kind="guild_members", limit=50, seconds=1)
                if not isinstance(page, list) or len(page) > 400:
                    invalid()
                if not page:
                    return rows
                size += len(json.dumps(page).encode())
                if size > 4 * 1024 * 1024:
                    raise V2Error("pagination_incomplete", "Guild member bytes exceed the complete-list bound.", status=413)
                for row in page:
                    if not isinstance(row, dict) or not isinstance(row.get("user"), dict):
                        invalid()
                    user = text_id(row["user"].get("id"))
                    if user not in seen:
                        seen.add(user)
                        rows.append(row)
                    if len(rows) > 20000:
                        raise V2Error("pagination_incomplete", "Guild member count exceeds the complete-list bound.", status=413)
                after = text_id(page[-1]["user"]["id"])
                if after in cursors:
                    raise V2Error("pagination_incomplete", "Guild member cursor did not progress.", status=502)
                cursors.add(after)
        raise V2Error("pagination_incomplete", "Guild member page budget exceeded.", status=502)

    async def guild_kick(self, guild, user, *, blacklist=False, delete_history_days=0, operation_id=None):
        if type(blacklist) is not bool or type(delete_history_days) is not int or delete_history_days not in {-1, 0, 3, 7, 15, 30}:
            raise V2Error("invalid_kick_options", "Guild deletion options are invalid.")
        return await self._write("DELETE", f"/guilds/{ident(guild)}/members/{ident(user)}",
            {"add_blacklist": blacklist, "delete_history_msg_days": delete_history_days}, kind="guild_kick", operation_id=operation_id, limit=50, seconds=1)

    async def guild_mute(self, guild, user, duration, *, operation_id=None):
        if type(duration) is not int or not 0 <= duration <= 2592000:
            raise V2Error("invalid_duration", "Guild mute exceeds the local 0..30 day bound.")
        return await self._write("PATCH", f"/guilds/{ident(guild)}/members/{ident(user)}/mute", {"mute_seconds": str(duration)}, kind="guild_mute", operation_id=operation_id, limit=50, seconds=1)

    async def guild_mute_batch(self, guild, users, duration, *, operation_id=None):
        if not isinstance(users, list) or not 1 <= len(users) <= 20 or type(duration) is not int or not 0 <= duration <= 2592000:
            raise V2Error("invalid_mute_options", "Batch mute is locally limited to 20 explicit members and 30 days.")
        for user in users:
            text_id(user)
        users = list(users)
        if len(set(users)) != len(users):
            raise V2Error("invalid_members", "Duplicate members are not accepted.")
        def validate(data):
            if not isinstance(data, dict) or not isinstance(data.get("user_ids"), list) or any(user not in users for user in data["user_ids"]):
                invalid()
            if set(data["user_ids"]) != set(users):
                raise V2Error("partial_failure", "Only some guild members were muted; the batch must not be replayed.", phase="partial", status=502, details={"succeeded": data["user_ids"]})
            return {"state": "succeeded", "user_ids": data["user_ids"]}
        return await self._write("PATCH", f"/guilds/{ident(guild)}/mute", {"mute_seconds": str(duration), "user_ids": users}, kind="guild_mute_batch", operation_id=operation_id, validate=validate, limit=50, seconds=1)

    async def close(self):
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
