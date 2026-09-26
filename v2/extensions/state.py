"""Robot-scoped mutation fences without credentials, presigned URLs or stream text."""
import asyncio
import hashlib
import json
import sqlite3

from ..errors import V2Error
from ..messaging.store import robot_key
from ..models import text_id


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


class ExtensionStore:
    def __init__(self, messages, *, capacity=2048):
        self.messages, self.db, self.capacity = messages, messages.db, capacity
        self.tasks = set()
        self.priority_tasks = set()
        self.closed = False
        self.storage_failed = False
        if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='extension_schema'").fetchone():
            if self.db.execute("SELECT version FROM extension_schema").fetchone()[0] not in (1, 2):
                raise V2Error("extension_state_corrupt", "Unsupported extension schema; nothing was reset.", status=503)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS extension_schema (version INTEGER NOT NULL);
            INSERT INTO extension_schema SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM extension_schema);
            CREATE TABLE IF NOT EXISTS extension_ops(robot TEXT, op_id TEXT, kind TEXT, binding TEXT,
                state TEXT NOT NULL, updated REAL NOT NULL, result TEXT, error TEXT, PRIMARY KEY(robot,op_id));
            CREATE INDEX IF NOT EXISTS extension_ops_age ON extension_ops(state,updated);
            CREATE TABLE IF NOT EXISTS extension_rates(robot TEXT, bucket TEXT, stamp REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS extension_ops_expiry ON extension_ops(updated)
                WHERE state NOT IN ('reserved','in_flight','unknown');
            CREATE INDEX IF NOT EXISTS extension_ops_history_age ON extension_ops(updated)
                WHERE state IN ('succeeded','partial','not_sent','rejected');
            CREATE INDEX IF NOT EXISTS extension_rate_count ON extension_rates(robot,bucket,stamp);
            CREATE TABLE IF NOT EXISTS extension_events(robot TEXT,event_key TEXT,name TEXT,received REAL,metadata TEXT,
                ack TEXT,business TEXT,error TEXT,updated REAL,PRIMARY KEY(robot,event_key));
        """)
        if "context" not in {row[1] for row in self.db.execute("PRAGMA table_info(extension_ops)")}:
            self.db.execute("ALTER TABLE extension_ops ADD COLUMN context TEXT")
        self.db.execute("UPDATE extension_schema SET version=2")
        self.db.commit()
        with self.messages.transaction():
            self.db.execute("UPDATE extension_ops SET state='unknown' WHERE state='in_flight'")
            self.db.execute("UPDATE extension_ops SET state='not_sent' WHERE state='reserved'")
            self.db.execute("UPDATE extension_events SET ack='unknown' WHERE ack='pending'")
            self.db.execute("UPDATE extension_events SET business='unknown' WHERE business IN ('pending','queued','admitted')")

    def prune(self):
        now = self.messages.now()
        self.db.execute("DELETE FROM extension_rates WHERE stamp<=?", (now - 60,))
        self.db.execute("DELETE FROM extension_ops WHERE updated<=? AND state NOT IN ('reserved','in_flight','unknown')", (now - 86400,))
        self.db.execute("UPDATE extension_ops SET result=NULL,error=NULL,context=NULL,state='history_evicted' WHERE rowid IN (SELECT rowid FROM extension_ops WHERE state IN ('succeeded','partial','not_sent','rejected') ORDER BY updated DESC,rowid DESC LIMIT -1 OFFSET ?)", (self.capacity,))

    def begin(self, robot, op_id, kind, binding, *, context=None):
        if self.storage_failed:
            raise V2Error("extension_storage_unavailable", "Restore storage and reload before further extension writes.", status=503)
        if self.closed:
            raise V2Error("service_stopped", "Extension operations are stopped.", status=503)
        key = robot_key(robot), text_id(op_id)
        context = json.dumps(context, ensure_ascii=False, allow_nan=False) if context is not None else None
        if context and len(context.encode()) > 16384:
            raise V2Error("extension_context_too_large", "The diagnostic scope exceeds its bounded storage budget.", status=413)
        with self.messages.transaction():
            self.prune()
            row = self.db.execute("SELECT * FROM extension_ops WHERE robot=? AND op_id=?", key).fetchone()
            if row:
                if (row["kind"], row["binding"]) != (kind, binding):
                    raise V2Error("operation_conflict", "This operation is bound to another extension request.", status=409)
                if row["state"] == "succeeded":
                    return False, json.loads(row["result"]) if row["result"] else None
                raise V2Error("extension_result_unknown" if row["state"] == "unknown" else "operation_already_attempted", "The retained extension operation cannot be replayed.", status=409, operation_id=op_id,
                              phase="result_unknown" if row["state"] in {"unknown", "in_flight"} else "not_sent")
            ack = int(kind == "interaction_ack")
            count = self.db.execute("SELECT count(*) FROM extension_ops WHERE state IN ('reserved','in_flight','unknown') AND (kind='interaction_ack')=?", (ack,)).fetchone()[0]
            if count >= (128 if ack else self.capacity):
                raise V2Error("extension_state_full", "Unknown or unfinished operations hold this lane's capacity.", status=503)
            # Compact replay fences retain their TTL; the database byte limit bounds them.
            self.db.execute("INSERT INTO extension_ops VALUES(?,?,?,?,?,?,NULL,NULL,?)", (*key, kind, binding, "reserved", self.messages.now(), context))
        return True, None

    def attempt(self, robot, op_id):
        with self.messages.transaction():
            row = self.db.execute("UPDATE extension_ops SET state='in_flight',updated=? WHERE robot=? AND op_id=? AND state IN ('reserved','in_flight')", (self.messages.now(), robot_key(robot), op_id))
            if row.rowcount != 1:
                raise V2Error("operation_already_attempted", "This extension operation is no longer writable.", status=409)

    def finish(self, robot, op_id, state, *, result=None, error=None):
        if state not in {"succeeded", "partial", "unknown", "not_sent", "rejected"}:
            raise ValueError("extension outcome")
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False) if result is not None else None
        if encoded and len(encoded.encode()) > 65536:
            raise V2Error("extension_result_too_large", "Extension result exceeds its retained limit.", status=502, phase="result_unknown")
        with self.messages.transaction():
            self.db.execute("UPDATE extension_ops SET state=?,updated=?,result=?,error=? WHERE robot=? AND op_id=? AND state IN ('reserved','in_flight')", (state, self.messages.now(), encoded, json.dumps(error) if error else None, robot_key(robot), op_id))
            self.prune()

    def recent(self, robot, limit=10):
        if type(limit) is not int or not 1 <= limit <= 32:
            raise V2Error("invalid_limit", "Read at most 32 operation summaries.")
        return [{**dict(row), "context": json.loads(row["context"]) if row["context"] else None} for row in self.db.execute(
            "SELECT op_id,kind,state,updated,context FROM extension_ops WHERE robot=? ORDER BY (state='unknown') DESC,updated DESC,rowid DESC LIMIT ?", (robot_key(robot), limit))]

    def operation(self, robot, op_id):
        row = self.db.execute("SELECT op_id,kind,state,updated,result,error,context FROM extension_ops WHERE robot=? AND op_id=?", (robot_key(robot), text_id(op_id))).fetchone()
        if not row:
            raise V2Error("operation_not_found", "No extension operation belongs to this robot.", status=404)
        return {**dict(row), "result": json.loads(row["result"]) if row["result"] else None,
                "error": json.loads(row["error"]) if row["error"] else None, "context": json.loads(row["context"]) if row["context"] else None}

    async def execute(self, *args, **kwargs):
        if self.storage_failed:
            raise V2Error("extension_storage_unavailable", "Restore storage and reload before further extension writes.", status=503)
        try:
            return await self._execute(*args, **kwargs)
        except sqlite3.Error:
            self.storage_failed = True
            raise V2Error("extension_storage_unavailable", "Operation state could not be finalized; preserve the database and inspect this operation before retrying.",
                          status=503, phase="result_unknown", operation_id=kwargs.get("op_id")) from None

    async def _execute(self, http, spec, *, op_id, kind, validate=lambda data: data, before_send=lambda: None, ambiguous_codes=(50001,), priority=False, context=None):
        robot = http.identity.robot
        spec.url  # Validate the canonical API path before retaining diagnostic scope.
        context = {**(context or {}), "method": spec.method, "path": spec.path}
        fresh, cached = self.begin(robot, op_id, kind, digest([spec.method, spec.path, spec.params, spec.json_body]), context=context)
        if not fresh:
            return cached
        tasks = self.priority_tasks if priority else self.tasks
        if len(tasks) >= (8 if priority else 32):
            self.finish(robot, op_id, "not_sent")
            raise V2Error("extension_capacity", "Too many pending extension writes.", status=429)
        task = asyncio.current_task()
        tasks.add(task)
        attempted = received = False
        try:
            def prepare():
                nonlocal attempted
                before_send()
                self.attempt(robot, op_id)
                attempted = True
            response = await http.request(spec, before_send=prepare)
            received = True
            result = validate(response.data)
            self.finish(robot, op_id, "succeeded", result=result)
            return result
        except asyncio.CancelledError as exc:
            phase = getattr(exc, "phase", "result_unknown" if attempted else "not_sent")
            self.finish(robot, op_id, "unknown" if phase == "result_unknown" else "not_sent")
            raise
        except V2Error as exc:
            phase = exc.phase
            if attempted and exc.business_code in ambiguous_codes:
                phase = "result_unknown"
            if received and phase != "partial" or attempted and exc.http_status is not None and (exc.http_status == 408 or exc.http_status >= 500) and exc.code != "token_refresh_failed":
                phase = "result_unknown"
            state = {"not_sent": "not_sent", "rejected": "rejected", "partial": "partial"}.get(phase, "unknown")
            error = V2Error(exc.code, str(exc), retcode=exc.retcode, status=exc.status, business_code=exc.business_code,
                            trace_id=exc.trace_id, retry_after=exc.retry_after, http_status=exc.http_status, phase=phase, operation_id=op_id, details=exc.details)
            self.finish(robot, op_id, state, error=error.as_dict())
            raise error from None
        except Exception:
            self.finish(robot, op_id, "unknown" if attempted else "not_sent")
            raise V2Error("extension_state_failure", "Extension state could not be finalized; inspect its operation.", status=503,
                          phase="result_unknown" if attempted else "not_sent", operation_id=op_id) from None
        finally:
            tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = (self.tasks | self.priority_tasks) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
