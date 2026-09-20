"""Durable bounded raw acceptance; P3 delivery is a separate acknowledgement."""

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from ..errors import V2Error


class RawInbox:
    def __init__(self, path, *, max_rows=1024, max_bytes=64 * 1024 * 1024, max_tombstones=32768, clock=time.time):
        if type(max_tombstones) is not int or max_tombstones < 1:
            raise ValueError("max_tombstones must be a positive integer")
        self.max_tombstones = max_tombstones
        self.clock, self.max_rows, self.max_bytes = clock, max_rows, max_bytes
        self.queued_bytes = 0
        self.callback_active = 0
        self.closed = False
        self.changed = asyncio.Event()
        path = Path(path)
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if path.is_symlink() or not path.is_file():
                raise V2Error("inbox_path_invalid", "Raw inbox must be a regular private file.", status=503)
            path.chmod(0o600)
        else:
            os.close(descriptor)
        self.db = sqlite3.connect(path, timeout=0.1)
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3):
                raise V2Error("inbox_corrupt", "Unsupported raw inbox schema.", status=503)
            self.db.execute("PRAGMA synchronous=FULL")
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            self.db.execute(f"PRAGMA max_page_count={128 * 1024 * 1024 // page_size}")
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute("""CREATE TABLE IF NOT EXISTS inbox (
                row_id INTEGER PRIMARY KEY, owner TEXT NOT NULL, event_id TEXT,
                body TEXT, received REAL NOT NULL, delivered REAL, size INTEGER NOT NULL DEFAULT 0,
                UNIQUE(owner, event_id))""")
            if version == 1:
                self.db.execute("ALTER TABLE inbox ADD COLUMN size INTEGER NOT NULL DEFAULT 0")
                self.db.execute("UPDATE inbox SET size=coalesce(length(cast(body AS BLOB)),0)")
            if version < 3:
                self.db.execute("ALTER TABLE inbox ADD COLUMN disposition TEXT NOT NULL DEFAULT 'pending'")
                self.db.execute("ALTER TABLE inbox ADD COLUMN reason TEXT")
            self.db.execute("PRAGMA user_version=3")
            self.db.execute("CREATE INDEX IF NOT EXISTS inbox_live_owner ON inbox(owner) WHERE body IS NOT NULL")
            self.db.execute("CREATE INDEX IF NOT EXISTS inbox_delivered_age ON inbox(delivered,row_id) WHERE body IS NULL")
            self._prune_delivered()
            self.db.commit()
        except BaseException:
            self.db.close()
            raise

    def _prune_delivered(self):
        self.db.execute("DELETE FROM inbox WHERE body IS NULL AND delivered<=?", (self.clock() - 300,))
        # Delivered dedup keys have their own bound; unprocessed payloads are never evicted.
        self.db.execute("DELETE FROM inbox WHERE row_id IN (SELECT row_id FROM inbox WHERE body IS NULL ORDER BY delivered DESC,row_id DESC LIMIT -1 OFFSET ?)", (self.max_tombstones,))

    def accept(self, owner, envelope):
        if self.closed:
            raise V2Error("service_stopped", "Raw inbox is stopped.", status=503)
        encoded = json.dumps(envelope.payload, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode()) > 1024 * 1024:
            raise V2Error("event_too_large", "Raw event exceeds 1 MiB.", status=413)
        try:
            with self.db:
                self.db.execute("DELETE FROM inbox WHERE body IS NULL AND delivered<=?", (self.clock() - 300,))
                if envelope.event_id and self.db.execute("SELECT 1 FROM inbox WHERE owner=? AND event_id=?", (owner, envelope.event_id)).fetchone():
                    return False
                count, size = self.db.execute("SELECT count(*), coalesce(sum(size), 0) FROM inbox WHERE body IS NOT NULL").fetchone()
                own_count = self.db.execute("SELECT count(*) FROM inbox WHERE owner=? AND body IS NOT NULL", (owner,)).fetchone()[0]
                if count >= self.max_rows or own_count >= 256 or size + len(encoded.encode()) > self.max_bytes:
                    raise V2Error("inbox_full", "Raw inbox is full; deliver pending events or inspect retained records in the WebUI.", status=503)
                self.db.execute("INSERT INTO inbox(owner,event_id,body,received,size) VALUES (?,?,?,?,?)",
                                (owner, envelope.event_id, encoded, envelope.received_at, len(encoded.encode())))
            self.changed.set()
            return True
        except sqlite3.Error:
            raise V2Error("inbox_unavailable", "Raw inbox commit failed; event was not acknowledged.", status=503) from None

    def pending(self, owner, limit=32):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise V2Error("invalid_limit", "Read at most 256 pending events.")
        return [{"receipt": row, "payload": json.loads(body), "received_at": received}
                for row, body, received in self.db.execute(
                    "SELECT row_id,body,received FROM inbox WHERE owner=? AND body IS NOT NULL AND disposition='pending' ORDER BY row_id LIMIT ?", (owner, limit))]

    def acknowledge(self, owner, receipt):
        with self.db:
            self.db.execute("UPDATE inbox SET body=NULL,size=0,delivered=? WHERE owner=? AND row_id=? AND body IS NOT NULL", (self.clock(), owner, receipt))
            self._prune_delivered()

    def retain(self, owner, receipt, reason, *, invalid=False):
        with self.db:
            self.db.execute("UPDATE inbox SET disposition=?,reason=? WHERE owner=? AND row_id=? AND body IS NOT NULL",
                            ("invalid" if invalid else "extension", reason[:80], owner, receipt))

    def retained(self, owner, limit=32):
        if type(limit) is not int or not 1 <= limit <= 256:
            raise V2Error("invalid_limit", "Read at most 256 retained events.")
        return [{"receipt": row, "payload": json.loads(body), "received_at": received, "reason": reason, "state": state}
                for row, body, received, reason, state in self.db.execute(
                    "SELECT row_id,body,received,reason,disposition FROM inbox WHERE owner=? AND body IS NOT NULL AND disposition!='pending' ORDER BY row_id LIMIT ?", (owner, limit))]

    @staticmethod
    def _retained_entry(owner, row):
        receipt, body, received, reason, state, size = row
        version = hashlib.sha256(json.dumps([owner, *row], ensure_ascii=False).encode()).hexdigest()
        try:
            payload = json.loads(body)
            event = payload.get("t") if isinstance(payload, dict) else None
        except (ValueError, TypeError, RecursionError):
            event = None
        return {"receipt": receipt, "version": version, "received_at": received, "reason": reason,
                "state": state, "size": size, "event_type": event if isinstance(event, str) and len(event) <= 64 else "unknown"}

    def retained_summary(self, owner):
        if self.closed:
            raise V2Error("service_stopped", "Raw inbox is stopped.", status=503)
        rows = self.db.execute("SELECT row_id,body,received,reason,disposition,size FROM inbox WHERE owner=? AND body IS NOT NULL AND disposition IN ('invalid','extension') ORDER BY row_id LIMIT 256", (owner,))
        return [self._retained_entry(owner, row) for row in rows]

    def discard_retained(self, owner, selected):
        if self.closed:
            raise V2Error("service_stopped", "Raw inbox is stopped.", status=503)
        if not isinstance(selected, list) or not 1 <= len(selected) <= 256:
            raise V2Error("invalid_selection", "Select 1 to 256 retained records.")
        if any(not isinstance(item, dict) or type(item.get("receipt")) is not int or item["receipt"] < 1
               or not isinstance(item.get("version"), str) or len(item["version"]) != 64 for item in selected):
            raise V2Error("invalid_selection", "Each retained record needs its exact receipt and snapshot.")
        if len({item["receipt"] for item in selected}) != len(selected):
            raise V2Error("invalid_selection", "Select each retained record once.")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for item in selected:
                row = self.db.execute("SELECT row_id,body,received,reason,disposition,size FROM inbox WHERE owner=? AND row_id=? AND body IS NOT NULL AND disposition IN ('invalid','extension')", (owner, item["receipt"])).fetchone()
                if row is None or self._retained_entry(owner, row)["version"] != item["version"]:
                    raise V2Error("inbox_snapshot_changed", "Retained records changed; inspect them again before discarding.", status=409)
                self.db.execute("UPDATE inbox SET body=NULL,size=0,delivered=?,disposition='discarded',reason='operator_discard' WHERE owner=? AND row_id=?", (self.clock(), owner, item["receipt"]))
            self._prune_delivered()
        self.changed.set()
        return len(selected)

    def diagnostics(self, owner):
        return dict(self.db.execute(
            "SELECT disposition,count(*) FROM inbox WHERE owner=? AND body IS NOT NULL GROUP BY disposition", (owner,)))

    def count(self, owner):
        return self.db.execute("SELECT count(*) FROM inbox WHERE owner=? AND body IS NOT NULL", (owner,)).fetchone()[0]

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()


class Ingress:
    def __init__(self, inbox, owner, *, guard=lambda: None, capacity=32):
        self.inbox, self.owner, self.guard = inbox, owner, guard
        self.queue = asyncio.Queue(maxsize=capacity)
        self.worker = None
        self.stopped = False
        self.last_sequence = None

    def start(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self._consume(), name="qq-v2-ingress")

    async def accept(self, envelope):
        self.guard()
        if self.stopped or self.worker is None or self.worker.done():
            raise V2Error("service_stopped", "Raw consumer is not available.", status=503)
        size = len(json.dumps(envelope.payload, ensure_ascii=False).encode())
        if self.queue.full() or self.inbox.queued_bytes + size > 32 * 1024 * 1024:
            raise V2Error("queue_full", "Raw ingress is full; no acknowledgement was sent.", status=503)
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        self.inbox.queued_bytes += size
        self.queue.put_nowait((envelope, size, future))
        # Caller cancellation must not roll back a durable commit or consume a future error.
        return await asyncio.shield(future)

    async def _consume(self):
        while True:
            envelope, size, future = await self.queue.get()
            try:
                self.guard()
                result = self.inbox.accept(self.owner, envelope)
                seq = envelope.payload.get("s")
                if type(seq) is int and (self.last_sequence is None or seq > self.last_sequence):
                    self.last_sequence = seq
                future.set_result(result)
            except V2Error as exc:
                future.set_exception(exc)
            except Exception:
                future.set_exception(V2Error("intake_failed", "Raw event was not durably accepted.", status=503))
            finally:
                self.inbox.queued_bytes -= size
                self.queue.task_done()

    async def close(self):
        self.stopped = True
        if self.worker is not None:
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        while not self.queue.empty():
            _, size, future = self.queue.get_nowait()
            self.inbox.queued_bytes -= size
            future.set_exception(V2Error("service_stopped", "Raw ingress stopped before commit.", status=503))
            self.queue.task_done()
