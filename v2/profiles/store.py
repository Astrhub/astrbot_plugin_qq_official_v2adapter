"""Bounded durable field history and independent membership revisions."""

import json
import math
import os
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from ..errors import V2Error
from ..sdk.identifiers import text_id


def key_of(robot):
    return json.dumps([robot.appid, robot.environment], separators=(",", ":"))


def scene_scope(scene, scope):
    if scene not in {"group", "guild", "c2c", "channel", "dm"}:
        raise V2Error("invalid_scope", "Unknown profile scene.")
    return text_id(scope)


def observation_time(value):
    if type(value) in (int, float) and math.isfinite(value) and value >= 0:
        return float(value)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.timestamp() >= 0:
                return parsed.timestamp()
        except (ValueError, OverflowError):
            pass
    return None


class ProfileStore:
    def __init__(self, path, *, max_profiles=32768, max_bytes=64 * 1024 * 1024, stale_seconds=300, clock=time.time):
        if (type(max_profiles) is not int or not 1 <= max_profiles <= 65536
                or type(max_bytes) is not int or not 1048576 <= max_bytes <= 268435456
                or type(stale_seconds) is not int or not 1 <= stale_seconds <= 86400):
            raise V2Error("invalid_profile_config", "Profile capacity or stale settings are invalid.")
        self.clock, self.stale_seconds = clock, stale_seconds
        self.max_profiles, self.max_bytes = max_profiles, max_bytes
        self.closed = False
        self.last_error = None
        self._scope_errors = {}
        self._robot_gaps = {}
        self.migration_skipped = 0
        path = Path(path)
        if path.is_symlink() or path.exists() and not path.is_file():
            raise V2Error("profile_path_invalid", "Profile store must be a regular private file.", status=503)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(fd)
        path.chmod(0o600)
        self.db = sqlite3.connect(path, timeout=0.1)
        self.db.row_factory = sqlite3.Row
        try:
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise V2Error("profile_schema_corrupt", "Unsupported profile schema; restore a matching backup.", status=503)
            self.db.execute("PRAGMA synchronous=FULL")
            page = self.db.execute("PRAGMA page_size").fetchone()[0]
            self.db.execute(f"PRAGMA max_page_count={max_bytes // page}")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS profiles (robot TEXT, scene TEXT, scope TEXT, kind TEXT, subject TEXT,
                    fields TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(robot,scene,scope,kind,subject));
                CREATE TABLE IF NOT EXISTS scopes (robot TEXT, scene TEXT, scope TEXT, revision INTEGER NOT NULL DEFAULT 0,
                    continuity INTEGER NOT NULL DEFAULT 0, reason TEXT, PRIMARY KEY(robot,scene,scope));
                CREATE TABLE IF NOT EXISTS membership (robot TEXT, scene TEXT, scope TEXT, kind TEXT, subject TEXT,
                    state TEXT NOT NULL, confirmed TEXT, at REAL, received REAL, connection TEXT, sequence INTEGER,
                    revision INTEGER NOT NULL, conflict TEXT, PRIMARY KEY(robot,scene,scope,kind,subject));
                CREATE TABLE IF NOT EXISTS rosters (robot TEXT, scene TEXT, scope TEXT, revision INTEGER NOT NULL,
                    complete INTEGER NOT NULL, continuous INTEGER NOT NULL, started REAL, finished REAL,
                    count INTEGER NOT NULL, PRIMARY KEY(robot,scene,scope));
                CREATE TABLE IF NOT EXISTS refresh_state (robot TEXT, scene TEXT, scope TEXT, kind TEXT, subject TEXT,
                    code TEXT, until REAL NOT NULL, PRIMARY KEY(robot,scene,scope,kind,subject));
                PRAGMA user_version=1;
            """)
            self.db.execute("UPDATE scopes SET continuity=0,reason='process_restart'")
            self.db.execute("UPDATE rosters SET continuous=0")
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def _checked(self):
        if self.closed:
            raise V2Error("service_stopped", "Profiles store has closed.", status=503)

    def _scope(self, robot, scene, scope):
        self._checked()
        scene_scope(scene, scope)
        return key_of(robot), scene, scope

    def _note_scope_error(self, key, reason):
        self.last_error = reason
        if self.db.execute("SELECT 1 FROM rosters WHERE robot=? AND scene=? AND scope=?", key).fetchone():
            self._scope_errors[key] = reason

    def scope_error(self, robot, scene, scope):
        return self._scope_errors.get(self._scope(robot, scene, scope))

    def remember_robot_gap(self, robot, reason):
        """Keep failed receiver invalidation in memory until each roster is rechecked."""
        self._checked()
        self.last_error = reason
        self._robot_gaps[key_of(robot)] = {"reason": reason, "recovered": set()}

    def robot_gap(self, robot, scene, scope):
        self._checked()
        gap = self._robot_gaps.get(key_of(robot))
        if gap and (scene, scene_scope(scene, scope)) not in gap["recovered"]:
            return gap["reason"]
        return None

    def revision(self, robot, scene, scope):
        key = self._scope(robot, scene, scope)
        row = self.db.execute("SELECT revision FROM scopes WHERE robot=? AND scene=? AND scope=?", key).fetchone()
        return row[0] if row else 0

    def _bump(self, key):
        self.db.execute("INSERT INTO scopes(robot,scene,scope,revision) VALUES(?,?,?,1) ON CONFLICT(robot,scene,scope) DO UPDATE SET revision=revision+1", key)
        return self.db.execute("SELECT revision FROM scopes WHERE robot=? AND scene=? AND scope=?", key).fetchone()[0]

    def merge(self, robot, scene, scope, user_id, fields, *, source, as_of, received=None, kind=None):
        key = (*self._scope(robot, scene, scope), kind or ("member_openid" if scene == "group" else "channel_user_id" if scene in {"guild", "channel", "dm"} else "user_openid"), text_id(user_id))
        if not isinstance(fields, dict) or not isinstance(source, str) or not source:
            raise V2Error("invalid_profile", "Expected structured profile fields and source.")
        received = self.clock() if received is None else received
        as_of = observation_time(as_of)
        if as_of is not None and as_of > received + 60:
            as_of = None
        clean = {field: value for field, value in fields.items()
                 if field in {"nickname", "avatar_url", "joined_at", "last_known_role", "bot", "union_openid", "user_openid"}
                 and value is not None and type(value) in (str, bool) and (not isinstance(value, str) or len(value) <= 2048)}
        if not clean:
            return False
        try:
            with self.db:
                old = self.db.execute("SELECT fields FROM profiles WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?", key).fetchone()
                saved = json.loads(old[0]) if old else {}
                updated = False
                for field, value in clean.items():
                    prior = saved.get(field)
                    # Untimed history only fills gaps; equal timestamps never rewrite conflicting evidence.
                    if prior is None or as_of is not None and (prior["as_of"] is None or as_of > prior["as_of"]):
                        saved[field] = {"value": value, "source": source, "as_of": as_of, "received": received}
                        updated = True
                if not updated:
                    return False
                if old is None and self.db.execute("SELECT count(*) FROM profiles WHERE robot=?", (key[0],)).fetchone()[0] >= self.max_profiles:
                    self.last_error = "cache_capacity"
                    raise V2Error("cache_capacity", "Profile count limit reached; history was retained.", status=503)
                self.db.execute("INSERT INTO profiles VALUES(?,?,?,?,?,?,?) ON CONFLICT(robot,scene,scope,kind,subject) DO UPDATE SET fields=excluded.fields,updated=excluded.updated",
                                (*key, json.dumps(saved, ensure_ascii=False), received))
            return True
        except V2Error as exc:
            if exc.code == "cache_capacity":
                self.mark_gap(robot, scene, scope, reason=exc.code)
            raise
        except sqlite3.Error as exc:
            if getattr(exc, "sqlite_errorcode", None) in {sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG}:
                self._note_scope_error(key[:3], "cache_capacity")
                try:
                    self.mark_gap(robot, scene, scope, reason="cache_capacity")
                except sqlite3.Error as gap_error:
                    if getattr(gap_error, "sqlite_errorcode", None) not in {sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG}:
                        raise
                raise V2Error("cache_capacity", "Profile byte limit reached; history was retained.", status=503) from None
            try:
                self.mark_gap(robot, scene, scope, reason="profile_storage_unavailable")
            except sqlite3.Error:
                self.last_error = "profile_storage_unavailable"
            raise

    def _member_room(self, key):
        if self.db.execute("SELECT 1 FROM membership WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?", key).fetchone():
            return
        if self.db.execute("SELECT count(*) FROM membership WHERE robot=?", (key[0],)).fetchone()[0] >= self.max_profiles:
            self._note_scope_error(key[:3], "cache_capacity")
            raise V2Error("cache_capacity", "Member-state capacity reached; no status was fabricated.", status=503)


    def member_event(self, robot, scene, scope, user_id, state, at, *, connection=None, sequence=None, received=None):
        if state not in {"present", "left"} or type(at) is not int or at < 0:
            raise V2Error("invalid_member_event", "Member update needs a state and Unix-second timestamp.")
        if sequence is not None and type(sequence) is not int:
            raise V2Error("invalid_member_event", "Sequence must be an integer if present.")
        key = (*self._scope(robot, scene, scope), "member_openid" if scene == "group" else "channel_user_id", text_id(user_id))
        received = self.clock() if received is None else received
        with self.db:
            old = self.db.execute("SELECT * FROM membership WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?", key).fetchone()
            if old is None:
                self._member_room(key)
            if old and old["at"] is not None:
                same_second_query = old["connection"] is None and int(old["at"]) == at
                if (at < old["at"] and not same_second_query) or at <= old["at"] and old["state"] == "unknown":
                    return False
                if at <= old["at"]:
                    if old["state"] == state:
                        return False
                    if (connection is not None and connection == old["connection"] and sequence is not None
                            and old["sequence"] is not None and sequence != old["sequence"]):
                        if sequence < old["sequence"]:
                            return False
                    else:
                        confirmed = old["confirmed"] or old["state"]
                        revision = self._bump(key[:3])
                        self.db.execute("UPDATE membership SET state='unknown',confirmed=?,conflict=?,revision=? WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?",
                                        (confirmed, json.dumps({"at": at, "states": [old["state"], state]}), revision, *key))
                        self._invalidate_roster(key[:3])
                        return True
            revision = self._bump(key[:3])
            self.db.execute("INSERT INTO membership VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(robot,scene,scope,kind,subject) DO UPDATE SET state=excluded.state,confirmed=excluded.confirmed,at=excluded.at,received=excluded.received,connection=excluded.connection,sequence=excluded.sequence,revision=excluded.revision,conflict=NULL",
                            (*key, state, state, float(at), received, connection, sequence, revision, None))
            self._invalidate_roster(key[:3])
            return True

    def query_member(self, robot, scene, scope, user_id, *, started_revision, started_at, fields):
        kind = "member_openid" if scene == "group" else "channel_user_id"
        key = (*self._scope(robot, scene, scope), kind, text_id(user_id))
        if self.revision(robot, scene, scope) != started_revision:
            previous = self.db.execute("SELECT fields FROM profiles WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?", key).fetchone()
            present = json.loads(previous[0]) if previous else {}
            missing = {name: value for name, value in fields.items() if name not in present}
            self.merge(robot, scene, scope, user_id, missing, source="official_query_late", as_of=started_at, kind=kind)
            return False
        self.merge(robot, scene, scope, user_id, fields, source="official_query", as_of=started_at, kind=kind)
        with self.db:
            if self.revision(robot, scene, scope) != started_revision:
                return False
            revision = self._bump(key[:3])
            self._member_room(key)
            self.db.execute("INSERT INTO membership VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(robot,scene,scope,kind,subject) DO UPDATE SET state='present',confirmed='present',at=excluded.at,received=excluded.received,connection=NULL,sequence=NULL,revision=excluded.revision,conflict=NULL",
                            (*key, "present", "present", started_at, self.clock(), None, None, revision, None))
            self._invalidate_roster(key[:3])
            return True

    def _invalidate_roster(self, key):
        self.db.execute("UPDATE rosters SET complete=0 WHERE robot=? AND scene=? AND scope=?", key)

    def mark_gap(self, robot=None, scene=None, scope=None, reason="intake_gap"):
        self._checked()
        if (robot is not None and scene is not None and scope is not None
                and reason in {"cache_capacity", "cache_storage_unavailable", "profile_storage_unavailable"}):
            self._note_scope_error(self._scope(robot, scene, scope), reason)
        filters, params = [], []
        for name, value in (("robot", key_of(robot) if robot else None), ("scene", scene), ("scope", scope)):
            if value is not None:
                filters.append(f"{name}=?")
                params.append(value)
        where = " WHERE " + " AND ".join(filters) if filters else ""
        with self.db:
            self.db.execute("UPDATE scopes SET continuity=0,revision=revision+1,reason=?" + where, (reason, *params))
            self.db.execute("UPDATE rosters SET continuous=0" + where, params)

        if robot is not None and scene is None and scope is None and self._robot_gaps.pop(key_of(robot), None):
            if not self._scope_errors and not self._robot_gaps:
                self.last_error = None
    def set_continuity(self, robot, scene, scope, value, *, reason=None):
        key = self._scope(robot, scene, scope)
        with self.db:
            self.db.execute("INSERT INTO scopes(robot,scene,scope,continuity,reason) VALUES(?,?,?,?,?) ON CONFLICT(robot,scene,scope) DO UPDATE SET continuity=excluded.continuity,reason=excluded.reason", (*key, int(value), reason))

    def roster_status(self, robot, scene, scope):
        key = self._scope(robot, scene, scope)
        row = self.db.execute("SELECT r.*,s.reason FROM rosters r JOIN scopes s USING(robot,scene,scope) WHERE r.robot=? AND r.scene=? AND r.scope=?", key).fetchone()
        if row:
            status = dict(row)
            status["complete"], status["continuous"] = bool(status["complete"]), bool(status["continuous"])
            return status
        return {"revision": self.revision(robot, scene, scope), "complete": False, "continuous": False, "count": 0, "reason": "not_loaded"}

    def record_roster(self, robot, scene, scope, members, *, started_revision, started_at):
        key = self._scope(robot, scene, scope)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO scopes(robot,scene,scope) VALUES(?,?,?)", key)
        if self.revision(robot, scene, scope) != started_revision:
            for member in members:
                user = text_id(member["member_openid"] if scene == "group" else member["user"]["id"])
                self.query_member(robot, scene, scope, user, started_revision=started_revision,
                    started_at=started_at, fields={"nickname": member.get("username"),
                        "last_known_role": member.get("member_role"), "joined_at": member.get("joined_at"),
                        "bot": member.get("bot")})
            return False
        for member in members:
            user = text_id(member["member_openid"] if scene == "group" else member["user"]["id"])
            fields = {"nickname": member.get("username"), "last_known_role": member.get("member_role"),
                      "joined_at": member.get("joined_at"), "bot": member.get("bot")}
            self.merge(robot, scene, scope, user, fields, source="roster_query", as_of=started_at)
        with self.db:
            if self.revision(robot, scene, scope) != started_revision:
                return False
            snapshot_revision = self._bump(key)
            for member in members:
                user_id = member["member_openid"] if scene == "group" else member["user"]["id"]
                self._member_room((*key, "member_openid" if scene == "group" else "channel_user_id", user_id))
                self.db.execute("INSERT INTO membership VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(robot,scene,scope,kind,subject) DO UPDATE SET state='present',confirmed='present',at=excluded.at,received=excluded.received,connection=NULL,sequence=NULL,revision=excluded.revision,conflict=NULL",
                    (*key, "member_openid" if scene == "group" else "channel_user_id", user_id,
                     "present", "present", started_at, self.clock(), None, None, snapshot_revision, None))
            continuity = self.db.execute("SELECT continuity FROM scopes WHERE robot=? AND scene=? AND scope=?", key).fetchone()
            continuous = bool(continuity and continuity[0])
            self.db.execute("INSERT INTO rosters VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(robot,scene,scope) DO UPDATE SET revision=excluded.revision,complete=excluded.complete,continuous=excluded.continuous,started=excluded.started,finished=excluded.finished,count=excluded.count",
                            (*key, snapshot_revision, 1, int(continuous), started_at, self.clock(), len(members)))
        self._scope_errors.pop(key, None)
        gap = self._robot_gaps.get(key[0])
        if gap is not None:
            gap["recovered"].add((scene, scope))
            remaining = self.db.execute("SELECT scene,scope FROM rosters WHERE robot=?", (key[0],))
            if all((row[0], row[1]) in gap["recovered"] for row in remaining):
                self._robot_gaps.pop(key[0], None)
        if not self._scope_errors and not self._robot_gaps:
            self.last_error = None
        return continuous

    def get_member(self, robot, scene, scope, user_id, *, kind=None):
        key = (*self._scope(robot, scene, scope), kind or ("member_openid" if scene == "group" else "channel_user_id" if scene in {"guild", "channel", "dm"} else "user_openid"), text_id(user_id))
        record = self.db.execute("SELECT fields FROM profiles WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?", key).fetchone()
        member = self.db.execute("SELECT * FROM membership WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject=?", key).fetchone()
        if record is None and member is None:
            raise V2Error("identity_not_observed", "No profile has been recorded in this robot and scope.", status=404)
        fields = json.loads(record[0]) if record else {}
        latest = max((entry["as_of"] for entry in fields.values() if entry["as_of"] is not None), default=None)
        return {"robot": key[0], "scene": scene, "scope": scope, "id_kind": key[3], "user_id": user_id,
                "fields": fields, "membership": member["state"] if member else "unknown", "revision": self.revision(robot, scene, scope),
                "confirmed": member["confirmed"] if member else None, "conflict": json.loads(member["conflict"]) if member and member["conflict"] else None,
                "stale": latest is None or self.clock() - latest >= self.stale_seconds, "partial": "nickname" not in fields,
                "missing_fields": [name for name in ("nickname",) if name not in fields],
                "source": fields.get("nickname", {}).get("source"), "as_of": latest}

    def list_known_members(self, robot, scene, scope, *, cursor="", limit=100):
        key = self._scope(robot, scene, scope)
        if not isinstance(cursor, str) or type(limit) is not int or not 1 <= limit <= 100:
            raise V2Error("invalid_pagination", "Use a string cursor and 1..100 entries.")
        users = [row[0] for row in self.db.execute("SELECT subject FROM profiles WHERE robot=? AND scene=? AND scope=? AND kind=? AND subject>? ORDER BY subject LIMIT ?",
            (*key, "member_openid" if scene == "group" else "channel_user_id", cursor, limit + 1))]
        more = len(users) > limit
        users = users[:limit]
        return {"members": [self.get_member(robot, scene, scope, user) for user in users],
                "next_cursor": users[-1] if more else "", "roster": self.roster_status(robot, scene, scope)}

    def find_known_members(self, robot, group, term):
        """Search scoped historical identities without querying QQ or leaking raw profile rows."""
        key = self._scope(robot, "group", group)
        result = []
        needle = term.casefold()
        try:
            rows = self.db.execute("SELECT subject,fields FROM profiles WHERE robot=? AND scene=? AND scope=? AND kind='member_openid' ORDER BY subject", key)
            for row in rows:
                fields = json.loads(row["fields"])
                if not isinstance(fields, dict) or not isinstance(fields.get("nickname", {}), dict):
                    raise ValueError("Invalid profile field structure")
                nickname = fields.get("nickname", {}).get("value")
                if needle not in row["subject"].casefold() and (not isinstance(nickname, str) or needle not in nickname.casefold()):
                    continue
                if len(result) == 20:
                    return {"candidates": result, "truncated": True, "source": "profile_cache"}
                profile = self.get_member(robot, "group", group, row["subject"])
                result.append({"member_openid": row["subject"], "nickname": nickname,
                               "membership": profile["membership"], "as_of": profile["as_of"]})
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            raise V2Error("profile_storage_unavailable", "Scoped profile history cannot be read.", status=503) from None
        return {"candidates": result, "truncated": False, "source": "profile_cache"}


    def migrate_identities(self, messages):
        """Copy only this plugin's proven legacy scoped observations, never delete them."""
        self._checked()
        count = 0
        for row in messages.db.execute("SELECT robot,kind,scope,subject,profile,last FROM identities"):
            try:
                appid, environment = json.loads(row[0])
                scene, scope = row[2].split(":", 1)
                robot = SimpleNamespace(appid=appid, environment=environment)
                old = json.loads(row[4])
                if self.merge(robot, scene, scope, row[3],
                              {"nickname": old.get("nickname"), "avatar_url": old.get("avatar_url")},
                              source="legacy_chat", as_of=row[5], received=row[5], kind=row[1]):
                    count += 1
            except V2Error as exc:
                self.migration_skipped += 1
                if exc.code == "cache_capacity":
                    self.last_error = exc.code
            except (ValueError, TypeError, KeyError):
                self.migration_skipped += 1
                continue
        return count

    def diagnostics(self):
        self._checked()
        return {"profiles": self.db.execute("SELECT count(*) FROM profiles").fetchone()[0],
                "memberships": self.db.execute("SELECT count(*) FROM membership").fetchone()[0],
                "bytes": self.db.execute("PRAGMA page_count").fetchone()[0] * self.db.execute("PRAGMA page_size").fetchone()[0],
                "max_profiles": self.max_profiles, "max_bytes": self.max_bytes,
                "last_error": self.last_error,
                "migration_skipped": self.migration_skipped,
                "rosters_incomplete": self.db.execute("SELECT count(*) FROM rosters WHERE complete=0 OR continuous=0").fetchone()[0]}

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()
