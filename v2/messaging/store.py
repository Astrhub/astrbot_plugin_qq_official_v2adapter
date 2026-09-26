"""Bounded robot-shared observations and durable, non-replayable send reservations."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from astrbot.core.platform.message_type import MessageType

from ..errors import V2Error
from ..models import SessionRoute, text_id
from .reply import ACTIVE_FALLBACK_CODES


def robot_key(robot):
    return json.dumps([robot.appid, robot.environment], separators=(",", ":"))


def route_key(route):
    return robot_key(route.robot), route.scene, route.target


def failure(code, message, status=409):
    raise V2Error(code, message, status=status)


def source_key(source):
    if source.message_id is not None:
        return text_id(source.message_id)
    if source.route.scene not in {"group", "c2c"}:
        failure("unsupported", "This event does not have a verified passive reply route.", 501)
    text_id(source.interaction_id)
    # Chat IDs reject control characters, so this internal namespace cannot collide.
    return "\x1f" + text_id(source.event_id)


def private_file(path):
    if path.is_symlink() or path.exists() and not path.is_file():
        failure("message_state_path_invalid", "Message state must use a regular private file.", 503)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
    path.chmod(0o600)


class MessageStore:
    def __init__(self, path, *, clock=time.time, identity_capacity=4096, operation_capacity=32768, source_capacity=16384):
        self.clock = clock
        self.identity_capacity, self.operation_capacity, self.source_capacity = identity_capacity, operation_capacity, source_capacity
        self._delivery_pins = {}
        self.closed = False
        path = Path(path)
        private_file(path)
        lock_path = path.with_name(path.name + ".lock")
        private_file(lock_path)
        self._lease = sqlite3.connect(lock_path, timeout=0.1)
        try:
            try:
                # The separate SQLite lock stays held across data transactions and needs no POSIX-only API.
                self._lease.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                failure("message_state_in_use", "Another owner holds this message-state directory.", 503)
            self.db = sqlite3.connect(path, timeout=0.1)
            self.db.row_factory = sqlite3.Row
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1, 2):
                failure("message_state_corrupt", "Unsupported message-state schema; data was not reset.", 503)
            self.db.execute("PRAGMA synchronous=FULL")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            self.db.execute(f"PRAGMA max_page_count={128 * 1024 * 1024 // page_size}")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS clock_guard (id INTEGER PRIMARY KEY, value REAL NOT NULL);
                INSERT OR IGNORE INTO clock_guard VALUES(1, 0);
                CREATE TABLE IF NOT EXISTS identities (robot TEXT, kind TEXT, scope TEXT, subject TEXT,
                    profile TEXT NOT NULL, first REAL NOT NULL, last REAL NOT NULL, message_id TEXT NOT NULL,
                    PRIMARY KEY(robot,kind,scope,subject));
                CREATE INDEX IF NOT EXISTS identity_age ON identities(last);
                CREATE TABLE IF NOT EXISTS targets (robot TEXT, scene TEXT, target TEXT, sender TEXT, guild TEXT,
                    last REAL NOT NULL, PRIMARY KEY(robot,scene,target));
                CREATE TABLE IF NOT EXISTS sources (robot TEXT, scene TEXT, target TEXT, message_id TEXT,
                    started REAL NOT NULL, received REAL NOT NULL, expires REAL NOT NULL, ref_idx TEXT,
                    seq INTEGER NOT NULL DEFAULT 0, used INTEGER NOT NULL DEFAULT 0, blocked TEXT,
                    PRIMARY KEY(robot,scene,target,message_id));
                CREATE INDEX IF NOT EXISTS source_expiry ON sources(expires);
                CREATE TABLE IF NOT EXISTS deliveries (robot TEXT, scene TEXT, target TEXT, message_id TEXT,
                    ref_idx TEXT, accepted REAL NOT NULL, PRIMARY KEY(robot,scene,target,message_id,ref_idx));
                CREATE INDEX IF NOT EXISTS delivery_age ON deliveries(accepted);
                CREATE TABLE IF NOT EXISTS refs (robot TEXT, scene TEXT, target TEXT, message_id TEXT,
                    ref_idx TEXT, expires REAL NOT NULL, PRIMARY KEY(robot,scene,target,message_id));
                CREATE INDEX IF NOT EXISTS reference_age ON refs(expires);
                CREATE TABLE IF NOT EXISTS operations (robot TEXT, op_id TEXT, scene TEXT, target TEXT,
                    source TEXT, digest TEXT NOT NULL, seq INTEGER, state TEXT NOT NULL, started REAL NOT NULL,
                    updated REAL NOT NULL, result TEXT, error TEXT, PRIMARY KEY(robot,op_id));
                CREATE INDEX IF NOT EXISTS operation_route ON operations(robot,scene,target,started);
                CREATE INDEX IF NOT EXISTS operation_history_age ON operations(updated)
                    WHERE state IN ('sent','rejected','not_sent');
                CREATE INDEX IF NOT EXISTS operation_expiry ON operations(updated)
                    WHERE state IN ('sent','rejected','not_sent','history_evicted');
                CREATE INDEX IF NOT EXISTS operation_pending_source ON operations(robot,scene,target,source)
                    WHERE state IN ('reserved','in_flight','unknown');
                CREATE TABLE IF NOT EXISTS charges (robot TEXT, op_id TEXT, bucket TEXT, subject TEXT,
                    until REAL NOT NULL, PRIMARY KEY(robot,op_id,bucket,subject));
                CREATE INDEX IF NOT EXISTS charge_budget ON charges(robot,bucket,subject,until);
                CREATE TABLE IF NOT EXISTS attempts (robot TEXT, op_id TEXT, number INTEGER, scene TEXT, target TEXT, active INTEGER, stamp REAL NOT NULL, PRIMARY KEY(robot,op_id,number));
                CREATE INDEX IF NOT EXISTS attempt_rate ON attempts(robot,stamp);
                PRAGMA user_version=2;
            """)
            self._water = self.db.execute("SELECT value FROM clock_guard WHERE id=1").fetchone()[0]
            with self.transaction():
                now = self.now()
                for row in self.db.execute("SELECT robot,op_id FROM operations WHERE state='in_flight'").fetchall():
                    self._finish(*row, "unknown", now)
                pending = self.db.execute("SELECT robot,op_id FROM operations WHERE state='reserved'").fetchall()
                for row in pending:
                    self._finish(*row, "not_sent", now)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self._lease.close()
            raise

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()
            self._lease.close()
            self._delivery_pins.clear()

    def now(self):
        if self.closed:
            failure("service_stopped", "Message state is closed.", 503)
        value = max(self._water, self.clock())
        self._water = value
        return value

    @contextmanager
    def transaction(self):
        # A failed operation must not roll back the clock high-water mark.
        with self.db:
            self.db.execute("UPDATE clock_guard SET value=? WHERE id=1", (self.now(),))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def pin_delivery(self, chat):
        if self.closed:
            failure("service_stopped", "Message state is closed.", 503)
        token = object()
        self._delivery_pins[token] = (*route_key(chat.route), chat.source.message_id, chat.source.ref_idx or "")
        return lambda: self._delivery_pins.pop(token, None)

    def _delivery_guard(self):
        guard = "NOT EXISTS (SELECT 1 FROM sources s WHERE s.robot=d.robot AND s.scene=d.scene AND s.target=d.target AND s.message_id=d.message_id)"
        keys = set(self._delivery_pins.values())
        if keys:
            guard += " AND (d.robot,d.scene,d.target,d.message_id,d.ref_idx) NOT IN (" + ",".join("(?,?,?,?,?)" for _ in keys) + ")"
        return guard, tuple(value for key in keys for value in key)

    def _cache_room(self, table):
        order = {"deliveries": "accepted", "refs": "expires"}[table]
        limit = self.source_capacity * 2
        if limit < 1:
            failure("message_state_full", "History cache capacity is disabled.", 503)
        excess = self.db.execute(f"SELECT max(0,count(*)-?+1) FROM {table}", (limit,)).fetchone()[0]
        if not excess:
            return
        guard, args = self._delivery_guard() if table == "deliveries" else ("1", ())
        deleted = self.db.execute(f"DELETE FROM {table} WHERE rowid IN (SELECT d.rowid FROM {table} d WHERE {guard} ORDER BY {order},d.rowid LIMIT ?)", (*args, excess)).rowcount
        if deleted < excess:
            failure("message_state_full", "History capacity is held by active deliveries or protected reply sources.", 503)

    def _trim_operation_history(self, *, reserve=0):
        limit = max(0, self.operation_capacity - reserve)
        excess = self.db.execute("SELECT max(0,count(*)-?) FROM operations WHERE state IN ('sent','rejected','not_sent')", (limit,)).fetchone()[0]
        if not excess:
            return
        rows = self.db.execute("SELECT rowid,state FROM operations WHERE state IN ('sent','rejected','not_sent') ORDER BY updated,rowid LIMIT ?", (excess,)).fetchall()
        # Success loses result details, not its replay fence or legacy charges.
        self.db.executemany("UPDATE operations SET state='history_evicted',result=NULL,error=NULL WHERE rowid=?", ((r[0],) for r in rows if r[1] == "sent"))
        self.db.executemany("DELETE FROM operations WHERE rowid=?", ((r[0],) for r in rows if r[1] != "sent"))

    def _prune(self, now, *, keep_source=None):
        self.db.execute("DELETE FROM attempts WHERE stamp<=?", (now - 60,))
        self.db.execute("DELETE FROM identities WHERE last<=?", (now - 86400,))
        self.db.execute("DELETE FROM targets WHERE last<=?", (now - 86400,))
        self.db.execute("DELETE FROM charges WHERE until<=?", (now,))
        self.db.execute("DELETE FROM refs WHERE expires<=?", (now,))
        guard = " AND (robot,scene,target,message_id) != (?,?,?,?)" if keep_source is not None else ""
        args = keep_source or ()
        pins = {value[:4] for value in self._delivery_pins.values()}
        if pins:
            # A live event must not lose a definitive invalid/unauthorized-source rejection.
            guard += " AND NOT (blocked IS NOT NULL AND blocked NOT LIKE 'active:%' AND (robot,scene,target,message_id) IN (" + ",".join("(?,?,?,?)" for _ in pins) + "))"
            args += tuple(value for key in pins for value in key)
        self.db.execute("DELETE FROM sources WHERE expires<=? AND NOT (blocked IS NOT NULL AND blocked NOT LIKE 'active:%' AND received>?) AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.robot=sources.robot AND o.scene=sources.scene AND o.target=sources.target AND o.source=sources.message_id AND (o.state IN ('reserved','in_flight','unknown') OR (sources.blocked IS NOT NULL AND sources.blocked NOT LIKE 'active:%')))" + guard,
                        (now, now - 86400, *args))
        guard, args = self._delivery_guard()
        self.db.execute(f"DELETE FROM deliveries AS d WHERE accepted<? AND {guard}", (now - 86400, *args))
        self.db.execute("DELETE FROM operations WHERE updated<? AND state IN ('sent','rejected','not_sent','history_evicted') AND NOT EXISTS (SELECT 1 FROM charges c WHERE c.robot=operations.robot AND c.op_id=operations.op_id)", (now - 86400,))
        self._trim_operation_history()

    def prune(self):
        with self.transaction():
            self._prune(self.now())

    def _capacity(self, table, limit):
        if self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= limit:
            failure("message_state_full", "Message-state capacity reached; retained history and unknown operations were not evicted.", 503)

    def observe(self, chat):
        key, source = route_key(chat.route), chat.source
        with self.transaction():
            now = self.now()
            self._prune(now)
            old = self.db.execute("SELECT * FROM sources WHERE robot=? AND scene=? AND target=? AND message_id=?", (*key, source.message_id)).fetchone()
            seen_at = min(source.received_at, now)
            if old:
                seen_at = min(seen_at, old["received"])
                self.db.execute("UPDATE sources SET expires=min(expires,?),received=min(received,?) WHERE robot=? AND scene=? AND target=? AND message_id=?",
                                (source.expires, source.received_at, *key, source.message_id))
            else:
                self._capacity("sources", self.source_capacity)
                self.db.execute("INSERT INTO sources(robot,scene,target,message_id,started,received,expires,ref_idx) VALUES(?,?,?,?,?,?,?,?)",
                                (*key, source.message_id, source.sent_at, source.received_at, source.expires, source.ref_idx))
            for record in chat.observations:
                profile_key = (key[0], record["id_kind"], record["scope"], record["user_id"])
                row = self.db.execute("SELECT first,last FROM identities WHERE robot=? AND kind=? AND scope=? AND subject=?", profile_key).fetchone()
                if row and row["last"] > seen_at:
                    continue
                self.db.execute("INSERT OR REPLACE INTO identities VALUES(?,?,?,?,?,?,?,?)",
                                (*profile_key, json.dumps(record), row["first"] if row else seen_at, seen_at, source.message_id))
            excess = self.db.execute("SELECT max(0,count(*)-?) FROM identities", (self.identity_capacity,)).fetchone()[0]
            if excess:
                self.db.execute("DELETE FROM identities WHERE rowid IN (SELECT rowid FROM identities ORDER BY last,rowid LIMIT ?)", (excess,))
            self.db.execute("INSERT INTO targets VALUES(?,?,?,?,?,?) ON CONFLICT(robot,scene,target) DO UPDATE SET last=max(last,excluded.last),sender=excluded.sender,guild=excluded.guild",
                            (*key, chat.message.sender.user_id, chat.guild_id, seen_at))
            for reference in [{"message_id": source.message_id, "ref_idx": source.ref_idx}, *chat.references]:
                msg_id = reference.get("message_id") or (reference.get("ref_idx") if chat.route.scene in {"group", "c2c"} else None)
                if msg_id:
                    self._put_ref(key, msg_id, reference.get("ref_idx"), seen_at + 86400)
            if source.ref_idx and chat.route.scene in {"group", "c2c"}:
                self._put_ref(key, source.ref_idx, source.ref_idx, seen_at + 86400)

    def _put_ref(self, key, message_id, ref_idx, expires):
        old = self.db.execute("SELECT ref_idx FROM refs WHERE robot=? AND scene=? AND target=? AND message_id=?", (*key, message_id)).fetchone()
        if old and old[0] != ref_idx:
            self.db.execute("UPDATE refs SET ref_idx=NULL WHERE robot=? AND scene=? AND target=? AND message_id=?", (*key, message_id))
            return
        if not old:
            self._cache_room("refs")
        self.db.execute("INSERT OR IGNORE INTO refs VALUES(?,?,?,?,?,?)", (*key, message_id, ref_idx, expires))

    def lookup(self, robot, kind, scope, subject):
        with self.transaction():
            now = self.now()
            row = self.db.execute("SELECT * FROM identities WHERE robot=? AND kind=? AND scope=? AND subject=? AND last>?",
                                  (robot_key(robot), kind, scope, subject, now - 86400)).fetchone()
            if row is None:
                failure("identity_not_observed", "No available chat observation in this robot and scope.", 404)
            return {**json.loads(row["profile"]), "first_seen": row["first"], "last_seen": row["last"], "source_message_id": row["message_id"]}

    def target(self, route):
        now = max(self._water, self.clock())
        row = self.db.execute("SELECT * FROM targets WHERE robot=? AND scene=? AND target=? AND last>?", (*route_key(route), now - 86400)).fetchone()
        if row is None:
            failure("identity_not_observed", "Active sends require a real, unexpired target observation.", 404)
        return dict(row)

    def resolve_session(self, robot, message_type, session_id):
        if message_type not in {MessageType.GROUP_MESSAGE, MessageType.FRIEND_MESSAGE}:
            failure("invalid_session", "Unsupported host message type.")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
            failure("invalid_session", "Invalid public session identifier.")
        if session_id.startswith("v2."):
            route = SessionRoute.decode(session_id)
            if route.robot != robot:
                failure("identity_mismatch", "Session belongs to another robot or environment.")
            if route.message_type != message_type:
                failure("invalid_session", "Message type does not match the encoded QQ route.")
            return route
        key, cutoff = robot_key(robot), self.now() - 86400
        if message_type == MessageType.GROUP_MESSAGE:
            # Match whole observed IDs, including isolated members; never split or guess an ID kind.
            rows = self.db.execute("""
                SELECT scene,target,NULL AS actor FROM targets
                    WHERE robot=? AND last>? AND scene IN ('group','channel') AND target=?
                UNION SELECT scene,target,sender FROM targets
                    WHERE robot=? AND last>? AND scene IN ('group','channel') AND sender || '_' || target=?
                UNION SELECT t.scene,t.target,i.subject FROM targets t JOIN identities i
                    ON i.robot=t.robot AND i.scope=t.scene || ':' || t.target
                    AND i.kind=CASE t.scene WHEN 'group' THEN 'member_openid' ELSE 'channel_user_id' END
                    WHERE t.robot=? AND t.last>? AND i.last>? AND t.scene IN ('group','channel')
                    AND i.subject || '_' || t.target=?
                """, (key, cutoff, session_id, key, cutoff, session_id, key, cutoff, cutoff, session_id)).fetchall()
        else:
            rows = self.db.execute("""SELECT scene,target,NULL AS actor FROM targets
                WHERE robot=? AND last>? AND ((scene='c2c' AND target=?) OR (scene='dm' AND sender=?))
                """, (key, cutoff, session_id, session_id)).fetchall()
        routes = {SessionRoute(robot, row["scene"], row["target"], row["actor"]) for row in rows}
        if not routes:
            failure("identity_not_observed", "No current chat observation matches this session.", 404)
        if len(routes) != 1:
            failure("ambiguous_session", "Multiple QQ routes match; use a bound event session or an explicit legacy route.")
        return routes.pop()

    def reference(self, route, message_id):
        row = self.db.execute("SELECT ref_idx FROM refs WHERE robot=? AND scene=? AND target=? AND message_id=? AND expires>?",
                              (*route_key(route), text_id(message_id), max(self._water, self.clock()))).fetchone()
        if row is None or (route.scene in {"c2c", "group"} and not row[0]):
            failure("reference_not_observed", "No equivalent reference exists for this robot, scene and target.", 404)
        return row[0] if route.scene in {"c2c", "group"} else message_id

    def delivered(self, chat):
        return self.db.execute("SELECT 1 FROM deliveries WHERE robot=? AND scene=? AND target=? AND message_id=? AND ref_idx=?",
                               (*route_key(chat.route), chat.source.message_id, chat.source.ref_idx or "")).fetchone() is not None

    def mark_delivered(self, chat):
        with self.transaction():
            self._prune(self.now())
            if not self.delivered(chat):
                self._cache_room("deliveries")
            self.db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,?)",
                            (*route_key(chat.route), chat.source.message_id, chat.source.ref_idx or "", self.now()))

    def _source(self, route, source, now):
        if (source.route.robot, source.route.scene, source.route.target) != (route.robot, route.scene, route.target):
            failure("identity_mismatch", "Reply source belongs to another target.")
        row = self.db.execute("SELECT * FROM sources WHERE robot=? AND scene=? AND target=? AND message_id=?", (*route_key(route), source_key(source))).fetchone()
        if row is None or row["expires"] <= now or source.expires <= now:
            failure("reply_expired", "The original incoming message reply window has expired.")
        if row["blocked"]:
            failure("reply_source_rejected", "QQ rejected this passive reply source.")
        return row

    def reply_mode(self, route, source):
        if source is None:
            self.target(route)
            return None
        if route_key(source.route) != route_key(route):
            failure("identity_mismatch", "Reply source belongs to another target.")
        now = self.now()
        key = (*route_key(route), source_key(source))
        row = self.db.execute("SELECT * FROM sources WHERE robot=? AND scene=? AND target=? AND message_id=?", key).fetchone()
        if row is not None and row["blocked"]:
            blocked = row["blocked"]
            if blocked in {str(c) for c in ACTIVE_FALLBACK_CODES}:
                rows = self.db.execute("SELECT error FROM operations WHERE robot=? AND scene=? AND target=? AND source=? AND state='rejected'", key)
                for old in rows:
                    error = json.loads(old[0]) if old[0] else {}
                    if (str(error.get("business_code")) == blocked and error.get("phase") == "rejected"
                            and error.get("code") != "token_refresh_failed" and error.get("http_status") in {200, 400}):
                        blocked = "active:" + blocked
                        break
            if not blocked.startswith("active:") or blocked[7:] not in {str(c) for c in ACTIVE_FALLBACK_CODES}:
                failure("reply_source_rejected", "This rejected source is not eligible for active fallback.")
            self.target(route)
            return {"code": "passive_source_rejected", "business_code": int(blocked[7:])}
        if row is None:
            observed = self.db.execute("SELECT 1 FROM deliveries WHERE robot=? AND scene=? AND target=? AND message_id=? AND ref_idx=?", (*key, source.ref_idx or "")).fetchone()
            observed = observed or self.db.execute("SELECT 1 FROM operations WHERE robot=? AND scene=? AND target=? AND source=?", key).fetchone()
            observed = observed or (*key, source.ref_idx or "") in self._delivery_pins.values()
            if not observed or source.expires > now:
                failure("reply_source_unavailable", "No retained observation proves this reply source.")
        if row is None or row["expires"] <= now:
            self.target(route)
            return {"code": "reply_window_expired"}
        return None


    def check_source(self, route, source):
        with self.transaction():
            return self._source(route, source, self.now())

    def block_source(self, route, source, code, *, allow_active=False):
        if source:
            with self.transaction():
                blocked = f"active:{code}" if allow_active and code in ACTIVE_FALLBACK_CODES else str(code)
                extra = " AND (blocked IS NULL OR blocked LIKE 'active:%')" if blocked.startswith("active:") else ""
                self.db.execute("UPDATE sources SET blocked=? WHERE robot=? AND scene=? AND target=? AND message_id=?" + extra, (blocked, *route_key(route), source_key(source)))

    def register_event_source(self, source):
        if source.message_id is not None:
            failure("invalid_event_source", "Chat sources must enter through genuine chat observation.")
        key = (*route_key(source.route), source_key(source))
        with self.transaction():
            now = self.now()
            self._prune(now)
            if source.expires <= now:
                failure("reply_expired", "The original interaction reply window expired.")
            if self.db.execute("SELECT 1 FROM sources WHERE robot=? AND scene=? AND target=? AND message_id=?", key).fetchone():
                self.db.execute("UPDATE sources SET expires=min(expires,?),received=min(received,?) WHERE robot=? AND scene=? AND target=? AND message_id=?", (source.expires, source.received_at, *key))
            else:
                self._capacity("sources", self.source_capacity)
                self.db.execute("INSERT INTO sources(robot,scene,target,message_id,started,received,expires,ref_idx) VALUES(?,?,?,?,?,?,?,NULL)", (*key, source.sent_at, source.received_at, source.expires))

    def operation(self, robot, op_id):
        row = self.db.execute("SELECT * FROM operations WHERE robot=? AND op_id=?", (robot_key(robot), text_id(op_id))).fetchone()
        if row is None:
            failure("operation_not_found", "No retained operation belongs to this robot.", 404)
        if row["state"] == "history_evicted":
            failure("operation_history_evicted", "Confirmed-send result details were evicted; this operation cannot be replayed.", 410)
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["error"] = json.loads(result["error"]) if result["error"] else None
        return result

    def reserve(self, route, source, digest, op_id, *, allow_active=False):
        text_id(op_id)
        key = route_key(route)
        with self.transaction():
            now = self.now()
            self._prune(now, keep_source=(*key, source_key(source)) if source else None)
            old = self.db.execute("SELECT * FROM operations WHERE robot=? AND op_id=?", (key[0], op_id)).fetchone()
            if old:
                if (old["scene"], old["target"], old["source"], old["digest"]) != (route.scene, route.target, source_key(source) if source else None, digest):
                    failure("operation_conflict", "A logical operation cannot change source, target or content.")
                if old["state"] == "sent":
                    return self.operation(route.robot, op_id)
                if old["state"] == "unknown":
                    failure("send_result_unknown", "The previous write is unknown and cannot be replayed.")
                if old["state"] == "history_evicted":
                    failure("operation_history_evicted", "Confirmed-send result details were evicted; this operation cannot be replayed.")
                failure("operation_already_attempted", "This operation was already attempted; inspect its recorded result.")
            pending = self.db.execute("SELECT count(*) FROM operations WHERE state IN ('reserved','in_flight','unknown')").fetchone()[0]
            if pending >= self.operation_capacity:
                failure("message_state_full", "Unfinished operation capacity reached; unknown and in-flight writes were retained.", 503)
            reason = self.reply_mode(route, source) if allow_active else None
            seq = None
            if source and reason is None:
                row = self._source(route, source, now)
                seq = row["seq"] + 1
                self.db.execute("UPDATE sources SET seq=?,used=used+1 WHERE robot=? AND scene=? AND target=? AND message_id=?", (seq, *key, source_key(source)))
            delivery = None
            if allow_active:
                mode = "passive" if source and reason is None else "active"
                delivery = {"delivery": {"mode": mode, "reason": reason, "attempts": [{"mode": mode, "state": "not_sent", "wire_attempts": 0}]}}
            self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key[0], op_id, route.scene, route.target, source_key(source) if source else None, digest, seq, "reserved", now, now, json.dumps(delivery) if delivery else None, None))
            return self.operation(route.robot, op_id)

    def save_delivery(self, robot, op_id, delivery):
        with self.transaction():
            self.db.execute("UPDATE operations SET result=? WHERE robot=? AND op_id=? AND state IN ('reserved','in_flight')",
                            (json.dumps({"delivery": delivery}), robot_key(robot), op_id))

    def switch_active(self, route, source, op_id, delivery):
        with self.transaction():
            self.target(route)
            row = self.operation(route.robot, op_id)
            if row["state"] not in {"reserved", "in_flight"} or (row["scene"], row["target"], row["source"]) != (route.scene, route.target, source_key(source)):
                failure("operation_conflict", "Only the original unfinished reply can change wire mode.")
            if row["seq"] is None:
                failure("operation_already_attempted", "This operation has already selected active delivery.")
            self.db.execute("UPDATE sources SET used=max(0,used-1) WHERE robot=? AND scene=? AND target=? AND message_id=?", (*route_key(route), source_key(source)))
            self.db.execute("DELETE FROM charges WHERE robot=? AND op_id=?", (robot_key(route.robot), op_id))
            self.db.execute("UPDATE operations SET seq=NULL,state='reserved',updated=?,result=? WHERE robot=? AND op_id=?",
                            (self.now(), json.dumps({"delivery": delivery}), robot_key(route.robot), op_id))


    def prepare_attempt(self, route, source, op_id, *, continuation=False):
        with self.transaction():
            now, robot = self.now(), robot_key(route.robot)
            if source:
                self._source(route, source, now)
            self.db.execute("DELETE FROM attempts WHERE stamp<=?", (now - 60,))
            self._capacity("attempts", self.operation_capacity * 2)
            number = self.db.execute("SELECT coalesce(max(number),0)+1 FROM attempts WHERE robot=? AND op_id=?", (robot, op_id)).fetchone()[0]
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?)", (robot, op_id, number, route.scene, route.target, int(source is None and not continuation), now))
            self.db.execute("UPDATE operations SET state='in_flight',updated=? WHERE robot=? AND op_id=?", (now, robot, op_id))

    def mark_in_flight(self, robot, op_id):
        with self.transaction():
            self.db.execute("UPDATE operations SET state='in_flight',updated=? WHERE robot=? AND op_id=? AND state IN ('reserved','in_flight')", (self.now(), robot_key(robot), op_id))

    def _finish(self, robot, op_id, state, now, result=None, error=None):
        row = self.db.execute("SELECT * FROM operations WHERE robot=? AND op_id=?", (robot, op_id)).fetchone()
        if row is None or row["state"] not in {"reserved", "in_flight"}:
            return
        if state in {"sent", "not_sent", "rejected"}:
            self._trim_operation_history(reserve=1)
        if state in {"not_sent", "rejected"}:
            if row["source"] and row["seq"] is not None:
                self.db.execute("UPDATE sources SET used=max(0,used-1) WHERE robot=? AND scene=? AND target=? AND message_id=?", (robot, row["scene"], row["target"], row["source"]))
            self.db.execute("DELETE FROM charges WHERE robot=? AND op_id=?", (robot, op_id))
        delivery = ((error or {}).get("details") or {}).get("delivery") or (json.loads(row["result"]).get("delivery") if row["result"] else None)
        if delivery:
            delivery["attempts"][-1]["state"] = state
            if result is not None:
                result = {"delivery": delivery, **result}
            else:
                error = {**(error or {}), "details": {**((error or {}).get("details") or {}), "delivery": delivery}}
        self.db.execute("UPDATE operations SET state=?,updated=?,result=?,error=? WHERE robot=? AND op_id=?",
                        (state, now, json.dumps(result) if result else None, json.dumps(error) if error else None, robot, op_id))
        if state == "sent" and result.get("message_id"):
            self._put_ref((robot, row["scene"], row["target"]), result["message_id"], result.get("ref_idx"), now + 86400)
            if result.get("ref_idx") and row["scene"] in {"group", "c2c"}:
                self._put_ref((robot, row["scene"], row["target"]), result["ref_idx"], result["ref_idx"], now + 86400)

    def finish(self, robot, op_id, state, *, result=None, error=None):
        if state not in {"not_sent", "rejected", "unknown", "sent"}:
            raise ValueError("invalid send outcome")
        with self.transaction():
            self._finish(robot_key(robot), op_id, state, self.now(), result, error)


class IdentityView:
    def __init__(self, store, robot):
        self.store, self.robot = store, robot

    def lookup(self, robot, kind, scope, subject):
        if robot != self.robot:
            failure("identity_mismatch", "Identity lookup cannot cross robots.")
        return self.store.lookup(robot, kind, scope, subject)

    @property
    def items(self):
        return {row[0]: True for row in self.store.db.execute("SELECT rowid FROM identities WHERE robot=? AND last>?", (robot_key(self.robot), max(self.store._water, self.store.clock()) - 86400))}
