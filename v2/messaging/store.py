"""Bounded robot-shared observations and durable, non-replayable send reservations."""
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from ..errors import V2Error
from ..models import text_id


def robot_key(robot):
    return json.dumps([robot.appid, robot.environment], separators=(",", ":"))


def route_key(route):
    return robot_key(route.robot), route.scene, route.target


def failure(code, message, status=409):
    raise V2Error(code, message, status=status)


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
            if self.db.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
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
                CREATE TABLE IF NOT EXISTS refs (robot TEXT, scene TEXT, target TEXT, message_id TEXT,
                    ref_idx TEXT, expires REAL NOT NULL, PRIMARY KEY(robot,scene,target,message_id));
                CREATE TABLE IF NOT EXISTS operations (robot TEXT, op_id TEXT, scene TEXT, target TEXT,
                    source TEXT, digest TEXT NOT NULL, seq INTEGER, state TEXT NOT NULL, started REAL NOT NULL,
                    updated REAL NOT NULL, result TEXT, error TEXT, PRIMARY KEY(robot,op_id));
                CREATE INDEX IF NOT EXISTS operation_route ON operations(robot,scene,target,started);
                CREATE INDEX IF NOT EXISTS operation_pending_source ON operations(robot,scene,target,source)
                    WHERE state IN ('reserved','in_flight','unknown');
                CREATE TABLE IF NOT EXISTS charges (robot TEXT, op_id TEXT, bucket TEXT, subject TEXT,
                    until REAL NOT NULL, PRIMARY KEY(robot,op_id,bucket,subject));
                CREATE INDEX IF NOT EXISTS charge_budget ON charges(robot,bucket,subject,until);
                CREATE TABLE IF NOT EXISTS attempts (robot TEXT, op_id TEXT, number INTEGER, scene TEXT, target TEXT, active INTEGER, stamp REAL NOT NULL, PRIMARY KEY(robot,op_id,number));
                CREATE INDEX IF NOT EXISTS attempt_rate ON attempts(robot,stamp);
                PRAGMA user_version=1;
            """)
            self._water = self.db.execute("SELECT value FROM clock_guard WHERE id=1").fetchone()[0]
            with self.transaction():
                now = self.now()
                self.db.execute("UPDATE operations SET state='unknown',updated=? WHERE state='in_flight'", (now,))
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

    def _prune(self, now):
        self.db.execute("DELETE FROM attempts WHERE stamp<=?", (now - 60,))
        self.db.execute("DELETE FROM identities WHERE last<=?", (now - 86400,))
        self.db.execute("DELETE FROM targets WHERE last<=?", (now - 86400,))
        self.db.execute("DELETE FROM charges WHERE until<=?", (now,))
        self.db.execute("DELETE FROM refs WHERE expires<=?", (now,))
        self.db.execute("DELETE FROM sources WHERE expires<=? AND NOT EXISTS (SELECT 1 FROM operations o WHERE o.robot=sources.robot AND o.scene=sources.scene AND o.target=sources.target AND o.source=sources.message_id AND o.state IN ('reserved','in_flight','unknown'))", (now,))
        self.db.execute("DELETE FROM deliveries WHERE accepted<?", (now - 86400,))
        self.db.execute("DELETE FROM operations WHERE updated<? AND state IN ('sent','rejected','not_sent') AND NOT EXISTS (SELECT 1 FROM charges c WHERE c.robot=operations.robot AND c.op_id=operations.op_id)", (now - 86400,))

    def prune(self):
        with self.transaction():
            self._prune(self.now())

    def _capacity(self, table, limit):
        if self.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= limit:
            failure("message_state_full", "Message-state capacity reached; retained quotas and unknown operations were not evicted.", 503)

    def observe(self, chat):
        key, source = route_key(chat.route), chat.source
        with self.transaction():
            now = self.now()
            self._prune(now)
            old = self.db.execute("SELECT * FROM sources WHERE robot=? AND scene=? AND target=? AND message_id=?", (*key, source.message_id)).fetchone()
            seen_at = min(source.received_at, now)
            if not self.delivered(chat):
                self._capacity("deliveries", self.source_capacity * 2)
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
            self._capacity("refs", self.source_capacity * 2)
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
            self._capacity("deliveries", self.source_capacity * 2)
            self.db.execute("INSERT OR IGNORE INTO deliveries VALUES(?,?,?,?,?,?)",
                            (*route_key(chat.route), chat.source.message_id, chat.source.ref_idx or "", self.now()))

    def _source(self, route, source, now):
        if (source.route.robot, source.route.scene, source.route.target) != (route.robot, route.scene, route.target):
            failure("identity_mismatch", "Reply source belongs to another target.")
        row = self.db.execute("SELECT * FROM sources WHERE robot=? AND scene=? AND target=? AND message_id=?", (*route_key(route), source.message_id)).fetchone()
        if row is None or row["expires"] <= now or source.expires <= now:
            failure("reply_expired", "The original incoming message reply window has expired.")
        if row["blocked"]:
            failure("reply_source_rejected", "QQ rejected this reply source; active fallback is forbidden.")
        return row

    def check_source(self, route, source):
        with self.transaction():
            return self._source(route, source, self.now())

    def block_source(self, route, source, code):
        if source:
            with self.transaction():
                self.db.execute("UPDATE sources SET blocked=? WHERE robot=? AND scene=? AND target=? AND message_id=?", (str(code), *route_key(route), source.message_id))

    def operation(self, robot, op_id):
        row = self.db.execute("SELECT * FROM operations WHERE robot=? AND op_id=?", (robot_key(robot), text_id(op_id))).fetchone()
        if row is None:
            failure("operation_not_found", "No retained operation belongs to this robot.", 404)
        result = dict(row)
        result["result"] = json.loads(result["result"]) if result["result"] else None
        result["error"] = json.loads(result["error"]) if result["error"] else None
        return result

    def _budgets(self, route, source, now):
        target = self.target(route)
        scene, recipient = route.scene, route.target
        budgets = [("message_qps", "bot", 1, 100)] if scene in {"c2c", "group"} else [("channel_qps", f"{scene}:{recipient}", 1, 5)]
        if source is not None:
            return budgets
        if scene in {"c2c", "group"}:
            budgets += [("active_bot", "bot", 60, 30), ("active_qps", "bot", 1, 5),
                        ("active_target", f"{scene}:{recipient}", 60, 20), ("active_day", f"{scene}:{recipient}", 86400, 1000)]
        elif scene == "channel":
            if not target["guild"]:
                failure("target_context_missing", "Active channel sending requires an observed guild ID.")
            rows = self.db.execute("SELECT DISTINCT subject FROM charges WHERE robot=? AND bucket=? AND until>?",
                                   (robot_key(route.robot), "guild:" + target["guild"], now)).fetchall()
            if recipient not in {r[0] for r in rows} and len(rows) >= 2:
                failure("active_quota_exhausted", "Conservative two-channel active limit reached.", 429)
            budgets += [("active_day", "channel:" + recipient, 86400, 20), ("guild:" + target["guild"], recipient, 86400, 20)]
        else:
            budgets += [("dm_user_day", target["sender"], 86400, 2), ("dm_bot_day", "bot", 86400, 200)]
        return budgets

    def reserve(self, route, source, digest, op_id):
        text_id(op_id)
        key = route_key(route)
        with self.transaction():
            now = self.now()
            self._prune(now)
            old = self.db.execute("SELECT * FROM operations WHERE robot=? AND op_id=?", (key[0], op_id)).fetchone()
            if old:
                if (old["scene"], old["target"], old["source"], old["digest"]) != (route.scene, route.target, source.message_id if source else None, digest):
                    failure("operation_conflict", "A logical operation cannot change source, target or content.")
                if old["state"] == "sent":
                    return self.operation(route.robot, op_id)
                if old["state"] == "unknown":
                    failure("send_result_unknown", "The previous write is unknown and cannot be replayed.")
                failure("operation_already_attempted", "This operation was already attempted; inspect its recorded result.")
            self._capacity("operations", self.operation_capacity)
            seq = None
            if source:
                row = self._source(route, source, now)
                limit = {"group": 5, "c2c": 4}.get(route.scene)
                if limit is not None and row["used"] >= limit:
                    failure("passive_quota_exhausted", "Passive reply reservation limit reached.", 429)
                seq = row["seq"] + 1
            budgets = self._budgets(route, source, now)
            for bucket, subject, window, limit in budgets:
                count = self.db.execute("SELECT count(*) FROM charges WHERE robot=? AND bucket=? AND subject=? AND until>?", (key[0], bucket, subject, now)).fetchone()[0]
                if count >= limit:
                    failure("local_rate_limited", "Conservative local rate limit reached; no message was sent.", 429)
            if source:
                self.db.execute("UPDATE sources SET seq=?,used=used+1 WHERE robot=? AND scene=? AND target=? AND message_id=?", (seq, *key, source.message_id))
            self.db.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (key[0], op_id, route.scene, route.target, source.message_id if source else None, digest, seq, "reserved", now, now, None, None))
            for bucket, subject, window, _ in budgets:
                self.db.execute("INSERT INTO charges VALUES(?,?,?,?,?)", (key[0], op_id, bucket, subject, now + window))
            return self.operation(route.robot, op_id)

    def prepare_attempt(self, route, source, op_id):
        with self.transaction():
            now, robot = self.now(), robot_key(route.robot)
            if source:
                self._source(route, source, now)
            self.db.execute("DELETE FROM attempts WHERE stamp<=?", (now - 60,))
            self._capacity("attempts", self.operation_capacity * 2)
            clauses, limit = ("", 100) if route.scene in {"group", "c2c"} else (" AND scene=? AND target=?", 5)
            args = (robot, now - 1) + (() if not clauses else (route.scene, route.target))
            count = self.db.execute("SELECT count(*) FROM attempts WHERE robot=? AND stamp>?" + clauses, args).fetchone()[0]
            if count >= limit:
                failure("local_rate_limited", "Wire-attempt rate limit reached; retry later explicitly.", 429)
            if source is None and route.scene in {"group", "c2c"}:
                count = self.db.execute("SELECT count(*) FROM attempts WHERE robot=? AND active=1 AND stamp>?", (robot, now - 1)).fetchone()[0]
                if count >= 5:
                    failure("local_rate_limited", "Conservative active wire-attempt rate reached.", 429)
            for bucket, subject, window, limit in self._budgets(route, source, now):
                count = self.db.execute("SELECT count(*) FROM charges WHERE robot=? AND bucket=? AND subject=? AND until>? AND op_id!=?", (robot, bucket, subject, now, op_id)).fetchone()[0]
                if count >= limit:
                    failure("local_rate_limited", "Quota changed while waiting to send.", 429)
                self.db.execute("INSERT INTO charges VALUES(?,?,?,?,?) ON CONFLICT(robot,op_id,bucket,subject) DO UPDATE SET until=max(until,excluded.until)", (robot, op_id, bucket, subject, now + window))
            number = self.db.execute("SELECT coalesce(max(number),0)+1 FROM attempts WHERE robot=? AND op_id=?", (robot, op_id)).fetchone()[0]
            self.db.execute("INSERT INTO attempts VALUES(?,?,?,?,?,?,?)", (robot, op_id, number, route.scene, route.target, int(source is None), now))
            self.db.execute("UPDATE operations SET state='in_flight',updated=? WHERE robot=? AND op_id=?", (now, robot, op_id))

    def mark_in_flight(self, robot, op_id):
        with self.transaction():
            self.db.execute("UPDATE operations SET state='in_flight',updated=? WHERE robot=? AND op_id=? AND state IN ('reserved','in_flight')", (self.now(), robot_key(robot), op_id))

    def _finish(self, robot, op_id, state, now, result=None, error=None):
        row = self.db.execute("SELECT * FROM operations WHERE robot=? AND op_id=?", (robot, op_id)).fetchone()
        if row is None or row["state"] not in {"reserved", "in_flight"}:
            return
        if state in {"not_sent", "rejected"}:
            if row["source"]:
                self.db.execute("UPDATE sources SET used=max(0,used-1) WHERE robot=? AND scene=? AND target=? AND message_id=?", (robot, row["scene"], row["target"], row["source"]))
            self.db.execute("DELETE FROM charges WHERE robot=? AND op_id=?", (robot, op_id))
        self.db.execute("UPDATE operations SET state=?,updated=?,result=?,error=? WHERE robot=? AND op_id=?",
                        (state, now, json.dumps(result) if result else None, json.dumps(error) if error else None, robot, op_id))
        if state == "sent":
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
