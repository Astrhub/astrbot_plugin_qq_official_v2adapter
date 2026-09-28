"""Cached profile reads and explicit coalesced QQ refreshes."""

import asyncio
import json

from ..errors import V2Error
from ..sdk.identifiers import text_id


class Profiles:
    def __init__(self, identity, store, reads, *, cooldown=600, continuity_check=lambda: False, guard=lambda: None):
        self.identity, self.store, self.reads = identity, store, reads
        self.continuity_check, self.guard = continuity_check, guard
        self.cooldown = cooldown
        self.pending = {}
        self.roster_pending = {}
        self.closed = False

    def _check(self):
        if self.closed:
            raise V2Error("service_stopped", "Profile view has closed.", status=503)
        self.guard()
        self.store._checked()

    async def get_member(self, group_openid: str, member_openid: str, *, mode="prefer_cache") -> dict:
        """Read historical cache or explicitly query an official current member."""
        self._check()
        group, member = text_id(group_openid), text_id(member_openid)
        if mode not in {"cache_only", "prefer_cache", "refresh"}:
            raise V2Error("invalid_profile_mode", "Use cache_only, prefer_cache or refresh.")
        cached = None
        try:
            cached = self.store.get_member(self.identity.robot, "group", group, member)
        except V2Error as exc:
            if exc.code != "identity_not_observed" or mode == "cache_only":
                raise
        if mode == "cache_only" or mode == "prefer_cache" and cached and not cached["missing_fields"]:
            return cached
        key = (self.identity.robot, group, member)
        if mode == "prefer_cache":
            row = self.store.db.execute("SELECT code,until FROM refresh_state WHERE robot=? AND scene='group' AND scope=? AND kind='member_openid' AND subject=?",
                                        (self._robot_key(), group, member)).fetchone()
            if row and row[1] > self.store.clock():
                if cached:
                    return {**cached, "refresh_error": row[0]}
                raise V2Error(row[0], "QQ profile lookup is cooling down after an earlier response.", status=503)
        future = self.pending.get(key)
        if future is None:
            if len(self.pending) >= 256:
                raise V2Error("profile_capacity", "Too many profile refreshes are pending.", status=503)
            future = asyncio.create_task(self._refresh(group, member), name="qq-v2-profile-refresh")
            self.pending[key] = future
            def settled(completed):
                if self.pending.get(key) is completed:
                    self.pending.pop(key, None)
                if not completed.cancelled():
                    completed.exception()
            future.add_done_callback(settled)
        # Keep a coalesced request alive if one waiter cancels.
        await asyncio.shield(future)
        return self.store.get_member(self.identity.robot, "group", group, member)

    def _robot_key(self):
        from .store import key_of
        return key_of(self.identity.robot)

    async def _refresh(self, group, member):
        robot = self.identity.robot
        revision = self.store.revision(robot, "group", group)
        started = self.store.clock()
        try:
            data = await self.reads.get_group_member_info(group, member)
        except V2Error as exc:
            if exc.business_code == 11253 or exc.code in {"qq_rate_limited", "passive_quota_exhausted"}:
                from ..transport.http import retry_after_seconds
                delay = retry_after_seconds(exc.retry_after) if exc.code == "qq_rate_limited" else None
                with self.store.db:
                    self.store.db.execute("INSERT OR REPLACE INTO refresh_state VALUES(?,?,?,?,?,?,?)",
                                          (self._robot_key(), "group", group, "member_openid", member, exc.code,
                                           started + (delay if delay is not None else self.cooldown)))
            raise
        self.store.query_member(robot, "group", group, member, started_revision=revision, started_at=started,
            fields={"nickname": data.get("username"), "last_known_role": data.get("member_role"),
                    "joined_at": data.get("joined_at"), "bot": data.get("bot"), "union_openid": data.get("union_openid")})
        with self.store.db:
            self.store.db.execute("DELETE FROM refresh_state WHERE robot=? AND scene='group' AND scope=? AND kind='member_openid' AND subject=?",
                                  (self._robot_key(), group, member))

    async def refresh_roster(self, group_openid: str, *, with_rows=False) -> dict:
        """Coalesce a full native roster read for this bot and group."""
        self._check()
        group = text_id(group_openid)
        key = (self.identity.robot, group)
        future = self.roster_pending.get(key)
        if future is None:
            if len(self.roster_pending) >= 32:
                raise V2Error("profile_capacity", "Too many roster reads are pending.", status=503)
            future = asyncio.create_task(self._refresh_roster(group), name="qq-v2-roster-refresh")
            self.roster_pending[key] = future
            def settled(completed):
                if self.roster_pending.get(key) is completed:
                    self.roster_pending.pop(key, None)
                if not completed.cancelled():
                    completed.exception()
            future.add_done_callback(settled)
        result = await asyncio.shield(future)
        return result if with_rows else {key: value for key, value in result.items() if key != "rows"}

    async def _refresh_roster(self, group):
        self._check()
        robot = self.identity.robot
        revision = self.store.revision(robot, "group", group)
        started = self.store.clock()
        continuity = self.continuity_check()
        rows, seen, cursors, cursor, size = [], set(), set(), "", 0
        try:
            async with asyncio.timeout(120):
                for _ in range(200):
                    page = await self.reads.get_group_member_list(group, cursor)
                    size += len(json.dumps(page, ensure_ascii=False).encode())
                    if size > 4 * 1024 * 1024:
                        raise V2Error("pagination_incomplete", "Member roster exceeds 4 MiB.", status=413)
                    for row in page["members"]:
                        if not isinstance(row, dict) or not isinstance(row.get("member_openid"), str):
                            raise V2Error("invalid_management_response", "Member roster contains an invalid ID.", status=502)
                        user = text_id(row["member_openid"])
                        if user not in seen:
                            seen.add(user)
                            rows.append(row)
                        if len(rows) > 5000:
                            raise V2Error("pagination_incomplete", "Member roster exceeds 5000 identities.", status=413)
                    cursor = page["next_cursor"]
                    if not cursor:
                        break
                    if cursor in cursors:
                        raise V2Error("pagination_incomplete", "QQ repeated a member cursor.", status=502)
                    cursors.add(cursor)
                else:
                    raise V2Error("pagination_incomplete", "Member roster exceeded 200 pages.", status=502)
        except TimeoutError:
            raise V2Error("pagination_incomplete", "Member roster timed out.", status=504) from None
        current_connection = self.continuity_check()
        if continuity and current_connection != continuity:
            self.store.mark_gap(robot, "group", group, reason="receiver_changed_during_roster")
        if continuity and current_connection == continuity and self.store.revision(robot, "group", group) == revision:
            self.store.set_continuity(robot, "group", group, True)
        self.store.record_roster(robot, "group", group, rows, started_revision=revision, started_at=started)
        return {**self.get_roster_status(group), "rows": rows}

    def cached_roster(self, group_openid: str):
        """Return only a fresh, receiver-continuous complete roster snapshot."""
        status = self.get_roster_status(group_openid)
        if (not status["complete"] or not status["continuous"] or
                status.get("finished", 0) < self.store.clock() - self.store.stale_seconds):
            return None
        robot = self.identity.robot
        from .store import key_of
        members = self.store.db.execute("SELECT subject FROM membership WHERE robot=? AND scene='group' AND scope=? AND kind='member_openid' AND revision=? AND state='present' ORDER BY subject",
                                        (key_of(robot), group_openid, status["revision"])).fetchall()
        if len(members) != status["count"]:
            return None
        return [self.store.get_member(robot, "group", group_openid, row[0]) for row in members]



    def list_known_members(self, group_openid: str, *, cursor="", limit=100) -> dict:
        self._check()
        page = self.store.list_known_members(self.identity.robot, "group", group_openid, cursor=cursor, limit=limit)
        page["roster"] = self.get_roster_status(group_openid)
        return page

    def get_roster_status(self, group_openid: str) -> dict:
        self._check()
        status = self.store.roster_status(self.identity.robot, "group", group_openid)
        if status["revision"] != self.store.revision(self.identity.robot, "group", group_openid):
            status["complete"] = False
            status["reason"] = "roster_revision_changed"
        scoped_error = (self.store.scope_error(self.identity.robot, "group", group_openid)
                        or self.store.robot_gap(self.identity.robot, "group", group_openid))
        if scoped_error:
            status.update(complete=False, continuous=False, reason=scoped_error)
        elif not self.continuity_check():
            status["continuous"] = False
            if status["complete"]:
                status["reason"] = "receiver_unavailable"
        return status

    def diagnostics(self) -> dict:
        """Return bounded profile capacity and continuity diagnostics on demand."""
        self._check()
        return self.store.diagnostics()


    async def close(self):
        self.closed = True
        tasks = list(self.pending.values()) + list(self.roster_pending.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
