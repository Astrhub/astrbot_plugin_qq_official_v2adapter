"""Instance-isolated chat history, separate from delivery and operation ledgers."""
import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from astrbot.api import logger
from astrbot.core.message.components import (
    At,
    File,
    Image,
    Plain,
    Record,
    Reply,
    Unknown,
    Video,
)

from ..errors import V2Error
from ..models import text_id
from .convert import attachment_component, author_nickname

DEFAULT_POLICY = {
    "enabled": True,
    "mode": "unlimited",
    "max_age_seconds": None,
    "max_count": None,
    "max_bytes": None,
}
POLICY_MODES = {"unlimited", "max_age", "max_count", "max_bytes"}
QUERY_LIMIT = 200
EXPORT_LIMIT = 1000
FORWARD_TYPES = {101, 102}
FTS_TABLE = "messages_fts"
BACKFILL_LIMIT = 512
SEARCH_QUERY_MAX = 512
_TOKENIZER = None
_TOKENIZER_READY = False
SECRET_KEY_MARKERS = ("token", "secret", "authorization", "cookie", "password", "csrf")


def failure(code, message, status=409):
    raise V2Error(code, message, status=status)



# Instance settings_key: platform_id, appid, environment.
def history_owner(value):
    if not isinstance(value, str) or not value:
        failure("invalid_history_owner", "Message history requires an instance owner key.", 503)
    return value

def history_file(path):
    if path.is_symlink() or path.exists() and not path.is_file():
        failure("history_state_path_invalid", "Message history must use a regular private file.", 503)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(descriptor)
    path.chmod(0o600)


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def load(value, default=None):
    if not value:
        return default
    parsed = json.loads(value)
    return default if parsed is None else parsed



def search_corpus(event):
    if not isinstance(event, dict):
        return ""
    chunks = []
    raw = event.get("raw_message")
    if isinstance(raw, str) and raw:
        chunks.append(raw)
    sender = event.get("sender")
    if isinstance(sender, dict):
        name = sender.get("nickname")
        if isinstance(name, str) and name:
            chunks.append(name)
    for part in event.get("message") or []:
        if not isinstance(part, dict):
            continue
        data = part.get("data") if isinstance(part.get("data"), dict) else {}
        for key in ("text", "name"):
            value = data.get(key)
            if isinstance(value, str) and value:
                chunks.append(value)
    return "\n".join(chunks)


def like_needle(value):
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def search_tokenizer():
    global _TOKENIZER, _TOKENIZER_READY
    if _TOKENIZER_READY:
        return _TOKENIZER
    _TOKENIZER_READY = True
    try:
        from astrbot.core.knowledge_base.retrieval import tokenizer as tokmod
        stopwords = tokmod.load_stopwords(Path(tokmod.__file__).with_name("hit_stopwords.txt"))
        _TOKENIZER = (tokmod.to_fts5_search_text, tokmod.build_fts5_or_query, tokmod.tokenize_text, stopwords)
    except Exception as exc:
        logger.warning("qq-v2 history search tokenizer unavailable type=%s", type(exc).__name__)
        _TOKENIZER = None
    return _TOKENIZER


def query_text(value):
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        failure("invalid_request", "q must be a string.")
    value = value.strip()
    if not value:
        return None
    if len(value) > SEARCH_QUERY_MAX:
        failure("invalid_request", "q is too long.")
    return value

def secret_key(name):
    lowered = name.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def secret_assignment(value):
    if not isinstance(value, str) or "=" not in value or "://" in value:
        return False
    name = value.split("=", 1)[0]
    return name.isidentifier() and secret_key(name)


def onebot_segment(part):
    if isinstance(part, Plain):
        return {"type": "text", "data": {"text": part.text or ""}}
    if isinstance(part, At):
        return {"type": "at", "data": {"qq": str(part.qq)}}
    if isinstance(part, Reply):
        return {"type": "reply", "data": {"id": str(part.id)}}
    if isinstance(part, Image):
        url = part.url or part.file
        return {"type": "image", "data": {"file": url, "url": url}}
    if isinstance(part, Record):
        url = part.url or part.file
        data = {"file": url, "url": url}
        if part.text:
            data["text"] = part.text
        return {"type": "record", "data": data}
    if isinstance(part, Video):
        url = part.url or part.file
        return {"type": "video", "data": {"file": url, "url": url}}
    if isinstance(part, File):
        return {"type": "file", "data": {"name": part.name or "file", "file": part.url or "", "url": part.url or ""}}
    if isinstance(part, Unknown):
        return {"type": "text", "data": {"text": part.text or ""}}
    return {"type": "text", "data": {"text": "[unmapped]"}}


def media_metadata(attachments):
    rows = []
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("content_type")
        kind = kind.lower() if isinstance(kind, str) else ""
        if kind == "voice" or kind.startswith("audio/"):
            media_kind = "record"
        elif kind.startswith("image/"):
            media_kind = "image"
        elif kind.startswith("video/"):
            media_kind = "video"
        else:
            media_kind = "file"
        row = {"kind": media_kind}
        for key in ("url", "content_type", "filename", "size", "voice_wav_url", "asr_refer_text"):
            if key in item:
                row[key] = item[key]
        rows.append(row)
    return rows


def sanitize_official(payload):
    if not isinstance(payload, dict):
        return {}

    def clean(value):
        if isinstance(value, dict):
            cleaned = {}
            for key, item in value.items():
                if not isinstance(key, str) or secret_key(key) or secret_assignment(item):
                    continue
                cleaned[key] = clean(item)
            return cleaned
        if isinstance(value, list):
            return [clean(item) for item in value[:256] if not secret_assignment(item)]
        if isinstance(value, str) and len(value.encode()) > 64 * 1024:
            return value.encode()[:64 * 1024].decode("utf-8", "ignore")
        return value

    data = payload.get("d") if isinstance(payload.get("d"), dict) else payload
    snapshot = {"t": payload.get("t"), "id": payload.get("id"), "d": clean(data)}
    return snapshot


def _node_time(node):
    stamp = node.get("timestamp")
    if isinstance(stamp, (int, float)) and stamp > 0:
        return int(stamp)
    if isinstance(stamp, str):
        try:
            from datetime import datetime
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            return int(parsed.timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0
    return 0


def _has_real_content(node):
    if not isinstance(node, dict):
        return False
    text = node.get("content")
    if isinstance(text, str) and text:
        return True
    attachments = node.get("attachments")
    if isinstance(attachments, list):
        for item in attachments:
            if isinstance(item, dict) and (item.get("url") or item.get("voice_wav_url")):
                return True
    children = node.get("msg_elements")
    if isinstance(children, list):
        return any(_has_real_content(child) for child in children)
    return False


def _project_forward_node(node, scene, field):
    parts = []
    text = node.get("content")
    if isinstance(text, str) and text:
        parts.append({"type": "text", "data": {"text": text}})
    attachments = node.get("attachments") if isinstance(node.get("attachments"), list) else []
    for item in attachments:
        if not isinstance(item, dict):
            continue
        part = attachment_component(item)
        if part is not None:
            parts.append(onebot_segment(part))
    author = node.get("author") if isinstance(node.get("author"), dict) else {}
    user_id = author.get(field)
    if user_id is not None:
        try:
            user_id = text_id(user_id)
        except V2Error:
            user_id = ""
    else:
        user_id = ""
    nickname = author_nickname(author, scene) or ""
    message_type = "group" if scene in {"group", "channel"} else "private"
    return {
        "time": _node_time(node),
        "message_type": message_type,
        "sender": {"user_id": user_id, "nickname": nickname},
        "content": parts,
        "message": parts,
    }


def _forward_resource_id(message_id, path, msg_idx):
    if msg_idx is not None:
        try:
            return text_id(msg_idx)
        except V2Error:
            pass
    if not path:
        value = f"fwd:{message_id}"
    else:
        value = f"fwd:{message_id}:{'.'.join(str(item) for item in path)}"
    if len(value) > 512:
        value = "fwd:" + hashlib.sha256(value.encode()).hexdigest()
    return value


def extract_forwards(payload, message_id, scene, field):
    data = payload.get("d") if isinstance(payload, dict) and isinstance(payload.get("d"), dict) else payload
    if not isinstance(data, dict):
        return []
    resources = []

    def walk(node, path):
        if not isinstance(node, dict):
            return
        children = node.get("msg_elements") if isinstance(node.get("msg_elements"), list) else []
        if node.get("message_type") in FORWARD_TYPES:
            nodes = []
            for index, child in enumerate(children):
                if not isinstance(child, dict):
                    continue
                if child.get("message_type") in FORWARD_TYPES:
                    walk(child, path + (index,))
                    continue
                if _has_real_content(child):
                    nodes.append(_project_forward_node(child, scene, field))
            if nodes:
                resources.append((_forward_resource_id(message_id, path, node.get("msg_idx")), nodes, media_metadata(
                    [item for child in children if isinstance(child, dict) for item in (child.get("attachments") or [])
                     if isinstance(item, dict)])))
            return
        for index, child in enumerate(children):
            walk(child, path + (index,))

    walk(data, ())
    return resources


def canonical_event(chat, forward_ids):
    message = chat.message
    scene = chat.route.scene
    message_type = "group" if scene in {"group", "channel"} else "private"
    segments = [onebot_segment(part) for part in message.message]
    existing = {(seg.get("type"), (seg.get("data") or {}).get("id")) for seg in segments}
    for forward_id in forward_ids:
        marker = ("forward", forward_id)
        if marker not in existing:
            segments.append({"type": "forward", "data": {"id": forward_id}})
            existing.add(marker)
    event = {
        "time": int(chat.source.sent_at),
        "message_type": message_type,
        "sub_type": "normal" if message_type == "group" else "friend",
        "message_id": chat.source.message_id,
        "real_id": chat.source.message_id,
        "user_id": message.sender.user_id,
        "self_id": message.self_id,
        "sender": {
            "user_id": message.sender.user_id,
            "nickname": message.sender.nickname or "",
            "card": "",
        },
        "message": segments,
        "raw_message": message.message_str or "",
        "font": 0,
        "group_id": chat.route.target if scene in {"group", "channel"} else "",
        "_qq": {
            "scene": scene,
            "target": chat.route.target,
            "generation": chat.identity.generation,
            "platform_id": chat.identity.platform_id,
            "ref_idx": chat.source.ref_idx,
            "event_id": chat.source.event_id,
            "media": media_metadata(chat.attachments),
            "references": list(chat.references),
            "official": sanitize_official(message.raw_message),
            "forwards": list(forward_ids),
        },
    }
    return event


def validate_policy(policy):
    if not isinstance(policy, dict) or policy.keys() - DEFAULT_POLICY.keys():
        failure("invalid_history_policy", "Unknown history policy fields.")
    enabled = policy.get("enabled", True)
    mode = policy.get("mode", "unlimited")
    if type(enabled) is not bool or mode not in POLICY_MODES:
        failure("invalid_history_policy", "History policy enabled must be boolean and mode must be known.")
    result = {**DEFAULT_POLICY, "enabled": enabled, "mode": mode}
    if mode == "max_age":
        value = policy.get("max_age_seconds")
        if type(value) is not int or value < 1:
            failure("invalid_history_policy", "max_age_seconds must be a positive integer.")
        result["max_age_seconds"] = value
    elif mode == "max_count":
        value = policy.get("max_count")
        if type(value) is not int or value < 1:
            failure("invalid_history_policy", "max_count must be a positive integer.")
        result["max_count"] = value
    elif mode == "max_bytes":
        value = policy.get("max_bytes")
        if type(value) is not int or value < 1:
            failure("invalid_history_policy", "max_bytes must be a positive integer.")
        result["max_bytes"] = value
    return result


def record_chat(history, chat):
    if history is None or history.closed:
        return False
    try:
        return history.record(chat)
    except Exception as exc:
        history.last_error = "history_storage_unavailable"
        logger.warning("qq-v2 history error operation=record code=%s type=%s", history.last_error, type(exc).__name__)
        return False


def resolve_reply_time(history, chat):
    if history is None or getattr(history, "closed", True):
        return None
    try:
        parts = getattr(getattr(chat, "message", None), "message", None) or []
        reply = next((part for part in parts if isinstance(part, Reply)), None)
        if reply is None or reply.id in (None, ""):
            return None
        return history.quoted_sent_at(
            chat.identity.settings_key, chat.route.scene, chat.route.target, reply.id,
            current_message_id=chat.source.message_id)
    except Exception as exc:
        history.last_error = "history_storage_unavailable"
        logger.warning(
            "qq-v2 history error operation=resolve_reply_time code=%s type=%s",
            history.last_error, type(exc).__name__)
        return None


def apply_reply_time(history, chat):
    stamp = resolve_reply_time(history, chat)
    if stamp is None:
        return
    parts = getattr(getattr(chat, "message", None), "message", None)
    if not isinstance(parts, list):
        return
    for part in parts:
        if isinstance(part, Reply):
            part.time = stamp
            return


class HistoryStore:
    def __init__(self, path, *, clock=time.time):
        self.clock = clock
        self.closed = False
        self.last_error = None
        self._fts_available = False
        self._fts_contentless_delete = False
        self._search_error = None
        self._query_mode = "like"
        path = Path(path)
        history_file(path)
        lock_path = path.with_name(path.name + ".lock")
        history_file(lock_path)
        self.path = path
        self._lease = sqlite3.connect(lock_path, timeout=0.1)
        try:
            try:
                self._lease.execute("BEGIN EXCLUSIVE")
            except sqlite3.OperationalError:
                failure("history_state_in_use", "Another owner holds this history directory.", 503)
            self.db = sqlite3.connect(path, timeout=0.1)
            self.db.row_factory = sqlite3.Row
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                failure("history_state_corrupt", "Unsupported history schema; data was not reset.", 503)
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS clock_guard (id INTEGER PRIMARY KEY, value REAL NOT NULL);
                INSERT OR IGNORE INTO clock_guard VALUES(1, 0);
                CREATE TABLE IF NOT EXISTS policy (
                    owner TEXT PRIMARY KEY,
                    enabled INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    max_age_seconds INTEGER,
                    max_count INTEGER,
                    max_bytes INTEGER,
                    updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    owner TEXT NOT NULL,
                    platform_id TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    target TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    sender TEXT,
                    sent_at REAL NOT NULL,
                    received_at REAL NOT NULL,
                    event_json TEXT NOT NULL,
                    official_json TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    ref_idx TEXT,
                    event_id TEXT,
                    bytes INTEGER NOT NULL,
                    PRIMARY KEY(owner, scene, target, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_time ON messages(owner, scene, target, sent_at);
                CREATE INDEX IF NOT EXISTS messages_id ON messages(owner, message_id, sent_at);
                CREATE TABLE IF NOT EXISTS forwards (
                    owner TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    target TEXT NOT NULL,
                    forward_id TEXT NOT NULL,
                    source_message_id TEXT NOT NULL,
                    generation TEXT NOT NULL,
                    nodes_json TEXT NOT NULL,
                    media_json TEXT NOT NULL,
                    stored_at REAL NOT NULL,
                    PRIMARY KEY(owner, scene, target, forward_id)
                );
                CREATE INDEX IF NOT EXISTS forwards_source ON forwards(owner, source_message_id);
                CREATE TABLE IF NOT EXISTS tombs (
                    owner TEXT NOT NULL,
                    scene TEXT NOT NULL,
                    target TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    at REAL NOT NULL,
                    PRIMARY KEY(owner, scene, target, message_id)
                );
                PRAGMA user_version=1;
            """)
            self.db.commit()
            self._water = self.db.execute("SELECT value FROM clock_guard WHERE id=1").fetchone()[0]
            self._init_search()
            self._secure_sidecars()
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
            failure("service_stopped", "Message history is closed.", 503)
        value = max(self._water, self.clock())
        self._water = value
        return value

    @contextmanager
    def transaction(self):
        # A failed history write must not roll back the clock high-water mark.
        with self.db:
            self.db.execute("UPDATE clock_guard SET value=? WHERE id=1", (self.now(),))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        self._secure_sidecars()



    def search_mode(self):
        return "fts5" if self._fts_available else "like"

    def last_query_mode(self):
        return self._query_mode

    def _init_search(self):
        if search_tokenizer() is None:
            self._query_mode = "like"
            return
        try:
            self._create_fts_table()
            valid, contentless = self._inspect_fts_table()
            if not valid:
                self.db.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
                self._create_fts_table(if_not_exists=False)
                valid, contentless = self._inspect_fts_table()
            if not valid:
                raise sqlite3.OperationalError("fts5 table invalid")
            self._fts_available = True
            self._fts_contentless_delete = contentless
            self._query_mode = "fts5"
            self.db.commit()
            self.backfill_search()
        except sqlite3.Error as exc:
            self._fts_available = False
            self._fts_contentless_delete = False
            self._query_mode = "like"
            self._search_error = "history_search_unavailable"
            logger.warning("qq-v2 history search FTS5 unavailable type=%s", type(exc).__name__)
            try:
                self.db.rollback()
            except sqlite3.Error:
                pass

    def _create_fts_table(self, if_not_exists=True):
        clause = "CREATE VIRTUAL TABLE IF NOT EXISTS" if if_not_exists else "CREATE VIRTUAL TABLE"
        try:
            self.db.execute(
                f"{clause} {FTS_TABLE} USING fts5(search_text, content='', contentless_delete=1, tokenize='unicode61')"
            )
        except sqlite3.Error:
            self.db.rollback()
            self.db.execute(
                f"{clause} {FTS_TABLE} USING fts5(search_text, content='', tokenize='unicode61')"
            )

    def _inspect_fts_table(self):
        row = self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (FTS_TABLE,)
        ).fetchone()
        if row is None or not row[0]:
            return False, False
        sql = row[0].lower()
        if "virtual table" not in sql or "using fts5" not in sql:
            return False, False
        columns = {item[1] for item in self.db.execute(f"PRAGMA table_info({FTS_TABLE})")}
        if "search_text" not in columns:
            return False, False
        compact = "".join(sql.split())
        return True, "contentless_delete=1" in compact

    def _filter_sql(self, owner, *, message_id=None, scene=None, target=None, since=None, until=None, alias=""):
        prefix = f"{alias}." if alias else ""
        sql = f"{prefix}owner=?"
        params = [owner]
        if message_id:
            sql += f" AND {prefix}message_id=?"
            params.append(text_id(message_id))
        if scene:
            sql += f" AND {prefix}scene=?"
            params.append(scene)
        if target:
            sql += f" AND {prefix}target=?"
            params.append(text_id(target))
        if since is not None:
            if type(since) not in (int, float):
                failure("invalid_request", "since must be a unix timestamp.")
            sql += f" AND {prefix}sent_at>=?"
            params.append(float(since))
        if until is not None:
            if type(until) not in (int, float):
                failure("invalid_request", "until must be a unix timestamp.")
            sql += f" AND {prefix}sent_at<=?"
            params.append(float(until))
        return sql, params

    def _fts_text(self, event):
        tools = search_tokenizer()
        if tools is None:
            return ""
        to_text, _build, _tokenize, stopwords = tools
        return to_text(search_corpus(event), stopwords)

    def _fts_row_exists(self, rowid):
        return self.db.execute(f"SELECT 1 FROM {FTS_TABLE} WHERE rowid=? LIMIT 1", (rowid,)).fetchone() is not None

    def _delete_fts_row(self, rowid, event=None):
        if not self._fts_available:
            return
        if self._fts_contentless_delete:
            self.db.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid=?", (rowid,))
            return
        if not self._fts_row_exists(rowid):
            return
        search_text = self._fts_text(event) if event is not None else ""
        self.db.execute(
            f"INSERT INTO {FTS_TABLE}({FTS_TABLE}, rowid, search_text) VALUES('delete', ?, ?)",
            (rowid, search_text),
        )

    def _insert_fts_row(self, rowid, event):
        if not self._fts_available:
            return
        self.db.execute(
            f"INSERT INTO {FTS_TABLE}(rowid, search_text) VALUES(?, ?)",
            (rowid, self._fts_text(event)),
        )

    def _upsert_fts(self, rowid, event):
        if not self._fts_available:
            return
        self.db.execute("SAVEPOINT history_fts")
        try:
            self._delete_fts_row(rowid, event)
            self._insert_fts_row(rowid, event)
            self.db.execute("RELEASE history_fts")
        except sqlite3.Error as exc:
            self.db.execute("ROLLBACK TO history_fts")
            self.db.execute("RELEASE history_fts")
            self._search_error = "history_search_index_failed"
            logger.warning(
                "qq-v2 history error operation=search_index code=%s type=%s",
                self._search_error, type(exc).__name__,
            )

    def backfill_search(self, *, owner=None, limit=BACKFILL_LIMIT):
        if not self._fts_available or search_tokenizer() is None:
            return {"indexed": 0, "pending": 0, "done": True}
        if type(limit) is not int or limit < 1:
            limit = BACKFILL_LIMIT
        sql = f"SELECT rowid, event_json FROM messages WHERE NOT EXISTS (SELECT 1 FROM {FTS_TABLE} f WHERE f.rowid = messages.rowid)"
        params = []
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        sql += " ORDER BY rowid LIMIT ?"
        params.append(limit)
        indexed = 0
        with self.transaction():
            rows = self.db.execute(sql, params).fetchall()
            for row in rows:
                self.db.execute("SAVEPOINT history_fts")
                try:
                    self._insert_fts_row(row["rowid"], load(row["event_json"]))
                    self.db.execute("RELEASE history_fts")
                    indexed += 1
                except sqlite3.Error as exc:
                    self.db.execute("ROLLBACK TO history_fts")
                    self.db.execute("RELEASE history_fts")
                    self._search_error = "history_search_index_failed"
                    logger.warning(
                        "qq-v2 history error operation=search_backfill code=%s type=%s",
                        self._search_error, type(exc).__name__,
                    )
                    break
        pending = self.search_pending(owner)
        return {"indexed": indexed, "pending": pending, "done": pending == 0}

    def search_pending(self, owner=None):
        if not self._fts_available:
            return 0
        sql = f"SELECT count(*) FROM messages WHERE NOT EXISTS (SELECT 1 FROM {FTS_TABLE} f WHERE f.rowid = messages.rowid)"
        params = []
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        return self.db.execute(sql, params).fetchone()[0]

    def search_indexed(self, owner=None):
        if not self._fts_available:
            return 0
        sql = f"SELECT count(*) FROM messages WHERE EXISTS (SELECT 1 FROM {FTS_TABLE} f WHERE f.rowid = messages.rowid)"
        params = []
        if owner is not None:
            sql += " AND owner=?"
            params.append(owner)
        return self.db.execute(sql, params).fetchone()[0]

    def search_status(self, owner=None):
        return {
            "mode": self.search_mode(),
            "pending": self.search_pending(owner),
            "indexed": self.search_indexed(owner),
            "contentless_delete": self._fts_contentless_delete,
            "last_error": self._search_error,
        }


    def _policy_row(self, owner):
        row = self.db.execute("SELECT * FROM policy WHERE owner=?", (owner,)).fetchone()
        if row is None:
            return dict(DEFAULT_POLICY)
        return {
            "enabled": bool(row["enabled"]),
            "mode": row["mode"],
            "max_age_seconds": row["max_age_seconds"],
            "max_count": row["max_count"],
            "max_bytes": row["max_bytes"],
        }

    def policy(self, owner):
        with self.transaction():
            return self._policy_row(history_owner(owner))

    def save_policy(self, owner, policy):
        policy = validate_policy(policy)
        key = history_owner(owner)
        with self.transaction():
            now = self.now()
            self.db.execute(
                """INSERT INTO policy(owner, enabled, mode, max_age_seconds, max_count, max_bytes, updated)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(owner) DO UPDATE SET
                     enabled=excluded.enabled, mode=excluded.mode,
                     max_age_seconds=excluded.max_age_seconds, max_count=excluded.max_count,
                     max_bytes=excluded.max_bytes, updated=excluded.updated""",
                (key, int(policy["enabled"]), policy["mode"], policy["max_age_seconds"],
                 policy["max_count"], policy["max_bytes"], now),
            )
            removed = self._apply_policy(key, policy, now)
        self.last_error = None
        return {"policy": policy, "evicted": removed}

    def _tomb(self, owner, scene, target, message_id, reason, now):
        self.db.execute(
            """INSERT INTO tombs(owner, scene, target, message_id, reason, at) VALUES(?,?,?,?,?,?)
               ON CONFLICT(owner, scene, target, message_id) DO UPDATE SET reason=excluded.reason, at=excluded.at""",
            (owner, scene, target, message_id, reason, now),
        )

    def _delete_message(self, owner, scene, target, message_id, reason, now):
        row = self.db.execute(
            "SELECT rowid, event_json FROM messages WHERE owner=? AND scene=? AND target=? AND message_id=?",
            (owner, scene, target, message_id),
        ).fetchone()
        if row is not None:
            self._delete_fts_row(row["rowid"], load(row["event_json"]))
            self.db.execute(
                "DELETE FROM messages WHERE owner=? AND scene=? AND target=? AND message_id=?",
                (owner, scene, target, message_id),
            )
            self.db.execute(
                "DELETE FROM forwards WHERE owner=? AND scene=? AND target=? AND source_message_id=?",
                (owner, scene, target, message_id),
            )
        self._tomb(owner, scene, target, message_id, reason, now)

    def _apply_policy(self, owner, policy, now):
        if policy["mode"] == "unlimited":
            return 0
        removed = 0
        if policy["mode"] == "max_age":
            cutoff = now - policy["max_age_seconds"]
            rows = self.db.execute(
                "SELECT scene, target, message_id FROM messages WHERE owner=? AND sent_at<?",
                (owner, cutoff),
            ).fetchall()
            for row in rows:
                self._delete_message(owner, row["scene"], row["target"], row["message_id"], "expired", now)
                removed += 1
            return removed
        if policy["mode"] == "max_count":
            extra = self.db.execute("SELECT count(*) FROM messages WHERE owner=?", (owner,)).fetchone()[0] - policy["max_count"]
            if extra <= 0:
                return 0
            rows = self.db.execute(
                "SELECT scene, target, message_id FROM messages WHERE owner=? ORDER BY sent_at ASC, message_id ASC LIMIT ?",
                (owner, extra),
            ).fetchall()
            for row in rows:
                self._delete_message(owner, row["scene"], row["target"], row["message_id"], "expired", now)
                removed += 1
            return removed
        while True:
            total = self.db.execute("SELECT coalesce(sum(bytes),0) FROM messages WHERE owner=?", (owner,)).fetchone()[0]
            if total <= policy["max_bytes"]:
                return removed
            row = self.db.execute(
                "SELECT scene, target, message_id FROM messages WHERE owner=? ORDER BY sent_at ASC, message_id ASC LIMIT 1",
                (owner,),
            ).fetchone()
            if row is None:
                return removed
            self._delete_message(owner, row["scene"], row["target"], row["message_id"], "expired", now)
            removed += 1

    def record(self, chat):
        try:
            owner = history_owner(chat.identity.settings_key)
            scene = chat.route.scene
            target = chat.route.target
            message_id = text_id(chat.source.message_id)
            payload = chat.message.raw_message if isinstance(chat.message.raw_message, dict) else {}
            field = {"group": "member_openid", "c2c": "user_openid", "channel": "id", "dm": "id"}.get(scene, "id")
            forwards = extract_forwards(payload, message_id, scene, field)
            forward_ids = [item[0] for item in forwards]
            event = canonical_event(chat, forward_ids)
            official = event["_qq"]["official"]
            media = event["_qq"]["media"]
            event_json, official_json, media_json = dump(event), dump(official), dump(media)
            size = len(event_json.encode()) + len(official_json.encode()) + len(media_json.encode())
            with self.transaction():
                policy = self._policy_row(owner)
                if not policy["enabled"]:
                    return False
                now = self.now()
                self.db.execute("DELETE FROM tombs WHERE owner=? AND scene=? AND target=? AND message_id=?",
                                (owner, scene, target, message_id))
                self.db.execute(
                    """INSERT INTO messages(owner, platform_id, generation, scene, target, message_id, sender,
                           sent_at, received_at, event_json, official_json, media_json, ref_idx, event_id, bytes)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(owner, scene, target, message_id) DO UPDATE SET
                         platform_id=excluded.platform_id, generation=excluded.generation, sender=excluded.sender,
                         sent_at=excluded.sent_at, received_at=excluded.received_at, event_json=excluded.event_json,
                         official_json=excluded.official_json, media_json=excluded.media_json, ref_idx=excluded.ref_idx,
                         event_id=excluded.event_id, bytes=excluded.bytes""",
                    (owner, chat.identity.platform_id, chat.identity.generation, scene, target, message_id,
                     chat.message.sender.user_id, chat.source.sent_at, chat.source.received_at,
                     event_json, official_json, media_json, chat.source.ref_idx, chat.source.event_id, size),
                )
                self.db.execute(
                    "DELETE FROM forwards WHERE owner=? AND scene=? AND target=? AND source_message_id=?",
                    (owner, scene, target, message_id),
                )
                for forward_id, nodes, forward_media in forwards:
                    self.db.execute(
                        """INSERT INTO forwards(owner, scene, target, forward_id, source_message_id, generation,
                               nodes_json, media_json, stored_at)
                           VALUES(?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(owner, scene, target, forward_id) DO UPDATE SET
                             source_message_id=excluded.source_message_id, generation=excluded.generation,
                             nodes_json=excluded.nodes_json, media_json=excluded.media_json, stored_at=excluded.stored_at""",
                        (owner, scene, target, forward_id, message_id, chat.identity.generation,
                         dump(nodes), dump(forward_media), now),
                    )
                rowid = self.db.execute(
                    "SELECT rowid FROM messages WHERE owner=? AND scene=? AND target=? AND message_id=?",
                    (owner, scene, target, message_id),
                ).fetchone()[0]
                self._upsert_fts(rowid, event)
                self._apply_policy(owner, policy, now)
            self.last_error = None
            return True
        except sqlite3.Error as exc:
            code = "history_integrity_failed" if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_CORRUPT else "history_storage_unavailable"
            self.last_error = code
            logger.warning("qq-v2 history error operation=record code=%s", code)
            return False
        except V2Error as exc:
            self.last_error = exc.code
            logger.warning("qq-v2 history error operation=record code=%s", exc.code)
            return False

    def _tomb_reason(self, owner, message_id, scene=None, target=None):
        sql = "SELECT reason FROM tombs WHERE owner=? AND message_id=?"
        params = [owner, message_id]
        if scene is not None:
            sql += " AND scene=?"
            params.append(scene)
        if target is not None:
            sql += " AND target=?"
            params.append(target)
        row = self.db.execute(sql + " ORDER BY at DESC LIMIT 1", params).fetchone()
        return row["reason"] if row else None

    def _raise_missing(self, owner, message_id, scene=None, target=None):
        reason = self._tomb_reason(owner, message_id, scene, target)
        if reason == "expired":
            failure("message_expired", "This message was removed by the applied retention policy.", 404)
        if reason in {"deleted", "purged"}:
            failure("message_deleted", "This message was deleted from local history.", 404)
        failure("message_not_found", "This message is not in local history.", 404)

    def get_msg(self, owner, message_id, *, scene=None, target=None):
        message_id = text_id(message_id)
        key = history_owner(owner)
        with self.transaction():
            sql = "SELECT event_json, scene, target FROM messages WHERE owner=? AND message_id=?"
            params = [key, message_id]
            if scene is not None:
                sql += " AND scene=?"
                params.append(scene)
            if target is not None:
                sql += " AND target=?"
                params.append(target)
            rows = self.db.execute(sql, params).fetchall()
            if not rows:
                if scene is not None and target is not None:
                    others = self.db.execute(
                        "SELECT 1 FROM messages WHERE owner=? AND message_id=? LIMIT 1",
                        (key, message_id),
                    ).fetchone()
                    if others:
                        failure("message_route_unavailable", "This message does not belong to the bound chat route.")
                    self._raise_missing(key, message_id, scene, target)
                self._raise_missing(key, message_id)
            if len({(row["scene"], row["target"]) for row in rows}) > 1:
                failure("message_route_unavailable", "Bind a chat route to disambiguate this message ID.")
            return load(rows[0]["event_json"])


    def quoted_sent_at(self, owner, scene, target, reply_id, *, current_message_id=None):
        if self.closed:
            return None
        try:
            key = history_owner(owner)
            target = text_id(target)
            reply_id = text_id(reply_id)
            if current_message_id not in (None, ""):
                current_message_id = text_id(current_message_id)
            else:
                current_message_id = None
        except V2Error:
            return None
        if scene in {"group", "c2c"}:
            column = "ref_idx"
        elif scene in {"channel", "dm"}:
            column = "message_id"
        else:
            return None
        sql = f"SELECT sent_at FROM messages WHERE owner=? AND scene=? AND target=? AND {column}=?"
        params = [key, scene, target, reply_id]
        if current_message_id is not None:
            sql += " AND message_id!=?"
            params.append(current_message_id)
        with self.transaction():
            if not self._policy_row(key)["enabled"]:
                return None
            rows = self.db.execute(sql, params).fetchall()
            if len(rows) != 1:
                return None
            return int(rows[0]["sent_at"])

    def get_forward(self, owner, forward_id, *, scene=None, target=None):
        forward_id = text_id(forward_id)
        key = history_owner(owner)
        with self.transaction():
            sql = "SELECT nodes_json, scene, target, source_message_id FROM forwards WHERE owner=? AND forward_id=?"
            params = [key, forward_id]
            if scene is not None:
                sql += " AND scene=?"
                params.append(scene)
            if target is not None:
                sql += " AND target=?"
                params.append(target)
            rows = self.db.execute(sql, params).fetchall()
            if rows:
                if len({(row["scene"], row["target"]) for row in rows}) > 1:
                    failure("message_route_unavailable", "Bind a chat route to disambiguate this forward ID.")
                return {"messages": load(rows[0]["nodes_json"])}
            if scene is not None and target is not None:
                message = self.db.execute(
                    "SELECT event_json FROM messages WHERE owner=? AND scene=? AND target=? AND message_id=?",
                    (key, scene, target, forward_id),
                ).fetchone()
            else:
                message = self.db.execute(
                    "SELECT event_json FROM messages WHERE owner=? AND message_id=?",
                    (key, forward_id),
                ).fetchone()
            if message:
                failure("forward_nodes_unavailable", "This ID is not a stored forward resource with real nodes.", 404)
            failure("forward_not_found", "This forward resource is not in local history.", 404)

    def query(self, owner, *, message_id=None, scene=None, target=None, since=None, until=None, q=None, limit=50, max_limit=QUERY_LIMIT):
        if type(limit) is not int or not 1 <= limit <= max_limit:
            failure("invalid_request", f"Query limit must be 1 to {max_limit}.")
        key = history_owner(owner)
        q = query_text(q)
        if q and self._fts_available:
            self.backfill_search(owner=key)
            rows = self._fts_query(
                key, q, message_id=message_id, scene=scene, target=target, since=since, until=until, limit=limit,
            )
            if rows is not None:
                return [self._public_row(row) for row in rows]
        filters, params = self._filter_sql(
            key, message_id=message_id, scene=scene, target=target, since=since, until=until,
        )
        with self.transaction():
            rows = self._like_query(filters, params, q, limit)
        self._query_mode = "like" if q else self.search_mode()
        return [self._public_row(row) for row in rows]

    def _fts_query(self, owner, q, *, message_id, scene, target, since, until, limit):
        tools = search_tokenizer()
        if tools is None:
            return None
        _to_text, build_query, tokenize, stopwords = tools
        match = build_query(tokenize(q, stopwords))
        if not match:
            self._query_mode = "fts5"
            return []
        filters, params = self._filter_sql(
            owner, message_id=message_id, scene=scene, target=target, since=since, until=until, alias="m",
        )
        sql = f"""SELECT m.platform_id, m.generation, m.scene, m.target, m.message_id, m.sender, m.sent_at, m.received_at,
                        m.event_json, m.official_json, m.media_json, m.ref_idx, m.event_id, m.bytes
                 FROM {FTS_TABLE}
                 JOIN messages m ON m.rowid = {FTS_TABLE}.rowid
                 WHERE {FTS_TABLE} MATCH ? AND {filters}
                 ORDER BY bm25({FTS_TABLE}) ASC, m.sent_at DESC, m.message_id DESC
                 LIMIT ?"""
        try:
            with self.transaction():
                rows = self.db.execute(sql, [match, *params, limit]).fetchall()
            self._query_mode = "fts5"
            return rows
        except sqlite3.Error as exc:
            logger.warning("qq-v2 history search FTS5 query failed type=%s", type(exc).__name__)
            self._search_error = "history_search_unavailable"
            message = str(exc).lower()
            if "no such module" in message or "no such table" in message:
                self._fts_available = False
            return None

    def _like_query(self, filters, params, q, limit):
        sql = f"""SELECT platform_id, generation, scene, target, message_id, sender, sent_at, received_at,
                        event_json, official_json, media_json, ref_idx, event_id, bytes
                 FROM messages WHERE {filters}"""
        like_params = list(params)
        if q:
            sql += " AND event_json LIKE ? ESCAPE '\\'"
            like_params.append(like_needle(q))
        sql += " ORDER BY sent_at DESC, message_id DESC LIMIT ?"
        like_params.append(limit)
        return self.db.execute(sql, like_params).fetchall()

    def _public_row(self, row):
        return {
            "platform_id": row["platform_id"],
            "generation": row["generation"],
            "scene": row["scene"],
            "target": row["target"],
            "message_id": row["message_id"],
            "sender": row["sender"],
            "sent_at": row["sent_at"],
            "received_at": row["received_at"],
            "ref_idx": row["ref_idx"],
            "event_id": row["event_id"],
            "bytes": row["bytes"],
            "event": load(row["event_json"]),
            "official": load(row["official_json"]),
            "media": load(row["media_json"]),
        }

    def _secure_sidecars(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.path if suffix == "" else self.path.with_name(self.path.name + suffix)
            if path.exists() and path.is_file() and not path.is_symlink():
                path.chmod(0o600)

    def _file_size(self):
        size = self.path.stat().st_size if self.path.exists() else 0
        for suffix in ("-wal", "-shm"):
            extra = self.path.with_name(self.path.name + suffix)
            if extra.exists():
                size += extra.stat().st_size
        return size

    def status(self, owner):
        key = history_owner(owner)
        if self._fts_available:
            self.backfill_search(owner=key)
        with self.transaction():
            policy = self._policy_row(key)
            count = self.db.execute("SELECT count(*) FROM messages WHERE owner=?", (key,)).fetchone()[0]
            forwards = self.db.execute("SELECT count(*) FROM forwards WHERE owner=?", (key,)).fetchone()[0]
            scenes = {row["scene"]: row["n"] for row in self.db.execute(
                "SELECT scene, count(*) AS n FROM messages WHERE owner=? GROUP BY scene", (key,))}
            bounds = self.db.execute(
                "SELECT min(sent_at) AS earliest, max(sent_at) AS latest FROM messages WHERE owner=?",
                (key,),
            ).fetchone()
            wal = self.db.execute("PRAGMA journal_mode").fetchone()[0]
            schema = self.db.execute("PRAGMA user_version").fetchone()[0]
            search = self.search_status(key)
        return {
            "path": str(self.path),
            "size_bytes": self._file_size(),
            "message_count": count,
            "forward_count": forwards,
            "scene_counts": scenes,
            "earliest": bounds["earliest"],
            "latest": bounds["latest"],
            "wal": {"mode": wal},
            "integrity": "not_checked",
            "schema_version": schema,
            "policy": policy,
            "search": search,
            "last_error": self.last_error,
        }

    def integrity(self):
        with self.transaction():
            result = self.db.execute("PRAGMA integrity_check").fetchone()[0]
            schema = self.db.execute("PRAGMA user_version").fetchone()[0]
            messages = self.db.execute("SELECT count(*) FROM messages").fetchone()[0]
            forwards = self.db.execute("SELECT count(*) FROM forwards").fetchone()[0]
        ok = result == "ok"
        if not ok:
            self.last_error = "history_integrity_failed"
        return {
            "ok": ok,
            "result": result,
            "schema_version": schema,
            "message_count": messages,
            "forward_count": forwards,
            "last_error": self.last_error,
        }

    def checkpoint(self):
        if self.closed:
            failure("service_stopped", "Message history is closed.", 503)
        busy, log, checkpointed = self.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        self._secure_sidecars()
        return {"busy": busy, "log": log, "checkpointed": checkpointed, "size_bytes": self._file_size()}

    def export_rows(self, owner, *, scene=None, target=None, since=None, until=None, q=None, message_id=None, limit=EXPORT_LIMIT):
        if type(limit) is not int or not 1 <= limit <= EXPORT_LIMIT:
            limit = EXPORT_LIMIT
        rows = self.query(
            owner, message_id=message_id, scene=scene, target=target, since=since, until=until, q=q,
            limit=limit, max_limit=EXPORT_LIMIT,
        )
        key = history_owner(owner)
        exported = {(row["scene"], row["target"], row["message_id"]) for row in rows}
        forwards = []
        if exported:
            with self.transaction():
                sql = "SELECT forward_id, scene, target, source_message_id, nodes_json, media_json FROM forwards WHERE owner=?"
                params = [key]
                if scene:
                    sql += " AND scene=?"
                    params.append(scene)
                if target:
                    sql += " AND target=?"
                    params.append(text_id(target))
                forwards = [
                    {
                        "forward_id": row["forward_id"],
                        "scene": row["scene"],
                        "target": row["target"],
                        "source_message_id": row["source_message_id"],
                        "messages": load(row["nodes_json"]),
                        "media": load(row["media_json"]),
                    }
                    for row in self.db.execute(sql, params).fetchall()
                    if (row["scene"], row["target"], row["source_message_id"]) in exported
                ]
        truncated = len(rows) >= min(limit, EXPORT_LIMIT)
        return {"messages": rows, "forwards": forwards, "truncated": truncated}

    def delete_range(self, owner, *, scene=None, target=None, since=None, until=None):
        key = history_owner(owner)
        sql = "SELECT scene, target, message_id FROM messages WHERE owner=?"
        params = [key]
        bounded = False
        if scene not in (None, ""):
            sql += " AND scene=?"
            params.append(scene)
            bounded = True
        if target not in (None, ""):
            sql += " AND target=?"
            params.append(text_id(target))
            bounded = True
        if since is not None:
            if type(since) not in (int, float):
                failure("invalid_request", "since must be a unix timestamp.")
            sql += " AND sent_at>=?"
            params.append(float(since))
            bounded = True
        if until is not None:
            if type(until) not in (int, float):
                failure("invalid_request", "until must be a unix timestamp.")
            sql += " AND sent_at<=?"
            params.append(float(until))
            bounded = True
        if not bounded:
            failure("invalid_history_range", "Range delete requires scene, target, since, or until.")
        with self.transaction():
            now = self.now()
            rows = self.db.execute(sql, params).fetchall()
            for row in rows:
                self._delete_message(key, row["scene"], row["target"], row["message_id"], "deleted", now)
        return {"deleted": len(rows)}

    def purge(self, owner):
        key = history_owner(owner)
        with self.transaction():
            now = self.now()
            rows = self.db.execute(
                "SELECT rowid, scene, target, message_id, event_json FROM messages WHERE owner=?",
                (key,),
            ).fetchall()
            if self._fts_available and self._fts_contentless_delete:
                self.db.execute(
                    f"DELETE FROM {FTS_TABLE} WHERE rowid IN (SELECT rowid FROM messages WHERE owner=?)",
                    (key,),
                )
            elif self._fts_available:
                for row in rows:
                    self._delete_fts_row(row["rowid"], load(row["event_json"]))
            self.db.execute("DELETE FROM messages WHERE owner=?", (key,))
            self.db.execute("DELETE FROM forwards WHERE owner=?", (key,))
            for row in rows:
                self._tomb(key, row["scene"], row["target"], row["message_id"], "purged", now)
        return {"deleted": len(rows)}
