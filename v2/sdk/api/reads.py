"""Named native QQ reads, preserving raw response fields and bounded page traversal."""

import json
from urllib.parse import quote

from ...errors import V2Error, not_ready
from ...protocol import RequestSpec
from ..identifiers import text_id
from ..pagination import page_deadline


def segment(value):
    return quote(text_id(value), safe="")


def page_cursor(value):
    if not isinstance(value, str) or len(value) > 4096:
        raise V2Error("invalid_cursor", "A bounded cursor string is required.")
    return value


def page_limit(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise V2Error("invalid_limit", f"Page size must be 1..{maximum}.")
    return value


def pagination_error(message):
    return V2Error("pagination_incomplete", message, status=502)



class NativeReadMixin:
    async def _native_read(self, path, *, params=None, json_body=None):
        self._check()
        http = self._client._state.http
        if http is None:
            raise not_ready()
        result = await http.request(RequestSpec(self._client.identity.robot.environment, "GET", path,
                                                params=params, json_body=json_body))
        self._check()
        return result.data

    async def get_gateway(self) -> dict:
        """Fetch the official general gateway response."""
        return await self._native_read("/gateway")

    async def get_gateway_bot(self) -> dict:
        """Fetch the discovered bot gateway without changing the receiver."""
        return await self._native_read("/gateway/bot")

    async def get_ws_url(self) -> dict:
        """Return the target SDK's bot gateway response."""
        return await self.get_gateway_bot()

    async def me(self) -> dict:
        """Fetch the native current-bot profile."""
        return await self._native_read("/users/@me")

    async def me_guilds(self, guild_id: str | None = None, limit: int = 100, desc: bool = False) -> list:
        """Fetch one guild-membership page with the SDK's before/after convention."""
        page_limit(limit, 100)
        if type(desc) is not bool:
            raise V2Error("invalid_params", "desc must be a boolean.")
        params = {"limit": limit}
        if guild_id is not None:
            params["before" if desc else "after"] = text_id(guild_id)
        return await self._native_read("/users/@me/guilds", params=params)

    async def get_message(self, channel_id: str, message_id: str) -> dict:
        """Read one channel message using the SDK 1.2.1 compatible endpoint."""
        return await self._native_read(f"/channels/{segment(channel_id)}/messages/{segment(message_id)}")

    async def get_group_bot_state(self, group_openid: str) -> dict:
        """Fetch the bot's present role inside a group."""
        return await self._native_read(f"/v2/groups/{segment(group_openid)}/bot_state")

    async def get_group_restrict_chat_setting(self, group_openid: str) -> dict:
        """Read one group's native mute settings."""
        return await self._native_read(f"/v2/groups/{segment(group_openid)}/restrict_chat_setting")

    async def get_group_member_blacklist(self, group_openid: str, cursor: str | None = None,
                                         limit: int | None = None) -> dict:
        """Return one native blacklist page, including next_cursor."""
        params = {}
        if cursor is not None:
            params["cursor"] = page_cursor(cursor)
        if limit is not None:
            params["limit"] = page_limit(limit, 100)
        return await self._native_read(f"/v2/groups/{segment(group_openid)}/member_blacklist", params=params)

    async def get_group_join_requests(self, group_openid: str, cursor: str | None = None,
                                      limit: int | None = None) -> dict:
        """Read one application page without inventing approval flags."""
        params = {}
        if cursor is not None:
            params["cursor"] = page_cursor(cursor)
        if limit is not None:
            params["limit"] = page_limit(limit, 50)
        return await self._native_read(f"/v2/groups/{segment(group_openid)}/join_request_list", params=params)

    async def get_join_approval_strategies(self, cursor: str | None = None, limit: int | None = None) -> dict:
        """Read one bot-wide strategy page."""
        params = {}
        if cursor is not None:
            params["cursor"] = page_cursor(cursor)
        if limit is not None:
            params["limit"] = page_limit(limit, 50)
        page = await self._native_read("/v2/groups/join_approval_strategy", params=params)
        if (not isinstance(page, dict) or not isinstance(page.get("strategies"), list)
                or not isinstance(page.get("next_cursor"), str)
                or any(not isinstance(item, dict) for item in page["strategies"])):
            raise pagination_error("QQ returned an incomplete strategy page.")
        return page

    async def get_menu(self) -> dict:
        """Fetch the current native global menu."""
        return await self._native_read("/v2/menu")

    async def get_panels(self, scope: str, cursor: str | None = None, limit: int | None = None) -> dict:
        """Read one panel page for an explicit QQ scene."""
        if not isinstance(scope, str) or scope not in {"c2c", "group", "channel", "dm"}:
            raise V2Error("invalid_scope", "Panel scope must be c2c, group, channel or dm.")
        params = {"scope": scope}
        if cursor is not None:
            params["cursor"] = page_cursor(cursor)
        if limit is not None:
            params["limit"] = page_limit(limit, 50)
        return await self._native_read("/v2/panels", params=params)

    async def get_panel(self, panel_id: str) -> dict:
        """Fetch one native panel with its version and unknown fields."""
        return await self._native_read(f"/v2/panels/{segment(panel_id)}")

    async def get_guild(self, guild_id: str) -> dict:
        """Fetch one native guild."""
        return await self._native_read(f"/guilds/{segment(guild_id)}")

    async def get_channels(self, guild_id: str) -> list:
        """Fetch all channels returned by the guild endpoint."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/channels")

    async def get_channel(self, channel_id: str) -> dict:
        """Fetch one native channel."""
        return await self._native_read(f"/channels/{segment(channel_id)}")

    async def get_guild_members(self, guild_id: str, after: str = "0", limit: int = 1) -> list:
        """Read one guild-member page with SDK-compatible defaults."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/members",
                                       params={"after": page_cursor(after), "limit": page_limit(limit, 400)})

    async def get_guild_member(self, guild_id: str, user_id: str) -> dict:
        """Fetch one guild member without a OneBot projection."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/members/{segment(user_id)}")

    async def get_guild_role_members(self, guild_id: str, role_id: str, start_index: str = "0",
                                     limit: int = 1) -> dict:
        """Read one role-member page with its official next marker."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/roles/{segment(role_id)}/members",
                                       params={"start_index": page_cursor(start_index), "limit": page_limit(limit, 400)})

    async def get_voice_members(self, channel_id: str) -> list:
        """Read voice members through the SDK 1.2.1 compatible endpoint."""
        return await self._native_read(f"/channels/{segment(channel_id)}/voice/members")

    async def get_channel_online_nums(self, channel_id: str) -> dict:
        """Read a channel's reported online count."""
        return await self._native_read(f"/channels/{segment(channel_id)}/online_nums")

    async def get_guild_message_setting(self, guild_id: str) -> dict:
        """Read guild message-frequency settings."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/message/setting")

    async def get_guild_roles(self, guild_id: str) -> dict:
        """Fetch guild role details without changing IDs."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/roles")

    async def get_channel_user_permissions(self, channel_id: str, user_id: str) -> dict:
        """Fetch explicit member permissions for one channel."""
        return await self._native_read(f"/channels/{segment(channel_id)}/members/{segment(user_id)}/permissions")

    async def get_channel_role_permissions(self, channel_id: str, role_id: str) -> dict:
        """Fetch explicit role permissions for one channel."""
        return await self._native_read(f"/channels/{segment(channel_id)}/roles/{segment(role_id)}/permissions")

    async def get_permissions(self, guild_id: str) -> list:
        """Fetch guild API permissions without fabricating account grants."""
        return await self._native_read(f"/guilds/{segment(guild_id)}/api_permission")

    async def get_pins(self, channel_id: str) -> dict:
        """Read pinned channel messages."""
        return await self._native_read(f"/channels/{segment(channel_id)}/pins")

    async def get_reaction_users(self, channel_id: str, message_id: str, emoji_type: int, emoji_id: str,
                                 cookie: str | None = None, limit: int = 20) -> dict:
        """Read one reaction-user page with an optional opaque cookie."""
        if not isinstance(emoji_type, int) or isinstance(emoji_type, bool) or emoji_type not in (1, 2):
            raise V2Error("invalid_emoji", "QQ emoji type must be 1 or 2.")
        params = {"limit": page_limit(limit, 50)}
        if cookie is not None:
            params["cookie"] = page_cursor(cookie)
        path = f"/channels/{segment(channel_id)}/messages/{segment(message_id)}/reactions/{int(emoji_type)}/{segment(emoji_id)}"
        return await self._native_read(path, params=params)

    async def get_schedules(self, channel_id: str, since: str | None = None) -> list:
        """Read schedules; the current guide sends since in the GET JSON body."""
        if since is not None and (isinstance(since, bool) or not isinstance(since, (int, str))
                                  or not str(since).isdecimal() or not 0 <= int(since) <= 2**64 - 1):
            raise V2Error("invalid_since", "Schedule since must be a uint64 millisecond timestamp.")
        body = {"since": int(since)} if since is not None else None
        return await self._native_read(f"/channels/{segment(channel_id)}/schedules", json_body=body)

    async def get_schedule(self, channel_id: str, schedule_id: str) -> dict:
        """Read one native channel schedule."""
        return await self._native_read(f"/channels/{segment(channel_id)}/schedules/{segment(schedule_id)}")

    async def get_threads(self, channel_id: str) -> dict:
        """Read the official thread response, retaining its is_finish flag."""
        return await self._native_read(f"/channels/{segment(channel_id)}/threads")

    async def get_thread_detail(self, channel_id: str, thread_id: str) -> dict:
        """Read one native thread, preserving its content structure."""
        return await self._native_read(f"/channels/{segment(channel_id)}/threads/{segment(thread_id)}")

    async def _walk(self, fetch, *, field, next_field, first, terminal="", limit=200, end_field=None):
        cursor, seen, total, size = first, set(), 0, 0
        async with page_deadline():
            for _ in range(limit):
                page = await fetch(cursor)
                if not isinstance(page, dict) or not isinstance(page.get(field), list) or not isinstance(page.get(next_field), str):
                    raise pagination_error("QQ returned an incomplete native page.")
                size += len(json.dumps(page, ensure_ascii=False).encode())
                if size > 4 * 1024 * 1024 or total + len(page[field]) > 5000:
                    raise pagination_error("Native pagination exceeded its bounded result size.")
                for item in page[field]:
                    total += 1
                    yield item
                if end_field is not None:
                    if type(page.get(end_field)) is not bool:
                        raise pagination_error("QQ page omitted its completion flag.")
                    if page[end_field]:
                        return
                cursor = page[next_field]
                if cursor == terminal:
                    return
                if not cursor or cursor in seen or len(cursor) > 4096:
                    raise pagination_error("QQ returned a missing or repeated native cursor.")
                seen.add(cursor)
        raise pagination_error("Native pagination exceeded its page limit.")

    async def iter_group_member_blacklist(self, group_openid: str):
        """Yield blacklisted users from bounded QQ pages."""
        async for item in self._walk(lambda cursor: self.get_group_member_blacklist(group_openid, cursor),
                                     field="users", next_field="next_cursor", first=""):
            yield item

    async def iter_group_join_requests(self, group_openid: str):
        """Yield application records from bounded QQ pages."""
        async for item in self._walk(lambda cursor: self.get_group_join_requests(group_openid, cursor),
                                     field="list", next_field="next_cursor", first=""):
            yield item

    async def iter_join_approval_strategies(self):
        """Yield bot-wide approval strategies from bounded QQ pages."""
        async for item in self._walk(self.get_join_approval_strategies,
                                     field="strategies", next_field="next_cursor", first=""):
            yield item

    async def iter_panels(self, scope: str):
        """Yield panel records for one scene without claiming a snapshot."""
        async for item in self._walk(lambda cursor: self.get_panels(scope, cursor),
                                     field="records", next_field="next_cursor", first="", end_field="is_end"):
            yield item

    async def iter_guild_role_members(self, guild_id: str, role_id: str, limit: int = 400):
        """Yield guild role members until the native next marker becomes 0."""
        async for item in self._walk(lambda cursor: self.get_guild_role_members(guild_id, role_id, cursor, limit),
                                     field="data", next_field="next", first="0", terminal="0"):
            yield item

    async def iter_reaction_users(self, channel_id: str, message_id: str, emoji_type: int, emoji_id: str):
        """Yield reaction users only while QQ supplies a usable cookie."""
        cursor, seen, total, size = None, set(), 0, 0
        async with page_deadline():
            for _ in range(200):
                page = await self.get_reaction_users(channel_id, message_id, emoji_type, emoji_id, cookie=cursor)
                if not isinstance(page, dict) or not isinstance(page.get("users"), list) or type(page.get("is_end")) is not bool:
                    raise pagination_error("Reaction page lacks its users or completion flag.")
                size += len(json.dumps(page, ensure_ascii=False).encode())
                if size > 4 * 1024 * 1024 or total + len(page["users"]) > 5000:
                    raise pagination_error("Reaction users exceeded their bounded result size.")
                for user in page["users"]:
                    total += 1
                    yield user
                if page["is_end"]:
                    return
                next_cookie = page.get("cookie")
                if not isinstance(next_cookie, str) or not next_cookie or next_cookie in seen or len(next_cookie) > 4096:
                    raise pagination_error("QQ returned a missing or repeated reaction cookie.")
                seen.add(next_cookie)
                cursor = next_cookie
        raise pagination_error("Reaction pagination exceeded its page limit.")

    async def iter_guild_members(self, guild_id: str, *, limit: int = 400):
        """Yield native guild members using the last real user ID as the next cursor."""
        cursor, seen, total, size = "0", set(), 0, 0
        async with page_deadline():
            for _ in range(200):
                rows = await self.get_guild_members(guild_id, cursor, limit)
                if not isinstance(rows, list):
                    raise pagination_error("Guild membership response is not a list.")
                size += len(json.dumps(rows, ensure_ascii=False).encode())
                if size > 4 * 1024 * 1024 or total + len(rows) > 5000:
                    raise pagination_error("Guild members exceeded their bounded result size.")
                if not rows:
                    return
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get("user"), dict):
                        raise pagination_error("Guild member has no cursor-bearing user.")
                    user_id = text_id(row["user"].get("id"))
                    total += 1
                    yield row
                if user_id in seen:
                    raise pagination_error("QQ repeated a guild member cursor.")
                seen.add(user_id)
                cursor = user_id
        raise pagination_error("Guild membership exceeded its page limit.")

    async def iter_me_guilds(self, *, limit: int = 100, desc: bool = False):
        """Yield bot guild memberships without pretending pages are atomic."""
        cursor, seen, total, size = None, set(), 0, 0
        async with page_deadline():
            for _ in range(200):
                rows = await self.me_guilds(cursor, limit, desc)
                if not isinstance(rows, list):
                    raise pagination_error("Bot guild response is not a list.")
                size += len(json.dumps(rows, ensure_ascii=False).encode())
                if size > 4 * 1024 * 1024 or total + len(rows) > 5000:
                    raise pagination_error("Bot guilds exceeded their bounded result size.")
                if not rows:
                    return
                for row in rows:
                    if not isinstance(row, dict):
                        raise pagination_error("Guild page contains an invalid record.")
                    guild_id = text_id(row.get("id"))
                    total += 1
                    yield row
                if guild_id in seen:
                    raise pagination_error("QQ repeated a guild cursor.")
                seen.add(guild_id)
                cursor = guild_id
        raise pagination_error("Bot guild listing exceeded its page limit.")

    async def complete_threads(self, channel_id: str) -> list:
        """Return threads only when the native endpoint reports a complete response."""
        data = await self.get_threads(channel_id)
        if (not isinstance(data, dict) or not isinstance(data.get("threads"), list) or
                type(data.get("is_finish")) is not int or data["is_finish"] != 1 or len(data["threads"]) > 5000):
            raise pagination_error("QQ thread response has no documented continuation cursor.")
        return data["threads"]
