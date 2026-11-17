"""Message history full-text search uses FTS5 and falls back to LIKE."""
import sqlite3
from types import SimpleNamespace

import pytest
from test_message_history import history_store, owner_key, recorded
from test_messaging_state import NOW, chat_payload, observed

from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.history import FTS_TABLE, HistoryStore
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, RobotKey
from v2.protocol import RawEnvelope
from v2.web_api import ControlAPI


def ids(rows):
    return [row["message_id"] for row in rows]


def fts_orphans(store):
    return store.db.execute(
        f"SELECT count(*) FROM {FTS_TABLE} WHERE rowid NOT IN (SELECT rowid FROM messages)"
    ).fetchone()[0]


def forwarded(config, store, *, message_id, text, nested_id):
    payload = chat_payload(message_id=message_id, text=text)
    payload["d"]["message_type"] = 102
    payload["d"]["msg_elements"] = [
        {"content": "node-one", "author": {"member_openid": "forward-user", "username": "Fwd"}},
        {"message_type": 102, "msg_idx": nested_id, "msg_elements": [
            {"content": "nested"},
        ]},
    ]
    chat = convert_chat(InstanceKey.from_config(config), RawEnvelope(payload, NOW))
    assert store.record(chat) is True
    return chat

def test_chinese_token_search_hits(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store, text="请帮我查询订单状态")
    assert store.search_mode() == "fts5"
    assert ids(store.query(owner_key(chat), q="订单")) == ["msg-one"]
    assert store.last_query_mode() == "fts5"
    store.close()


def test_english_and_mixed_text_hits(config, tmp_path):
    store, _ = history_store(tmp_path)
    recorded(config, store, message_id="en", text="hello world from bot")
    mix = recorded(config, store, message_id="mix", text="hello 世界")
    owner = owner_key(mix)
    assert set(ids(store.query(owner, q="hello"))) == {"en", "mix"}
    assert ids(store.query(owner, q="世界")) == ["mix"]
    assert ids(store.query(owner, q="world")) == ["en"]
    store.close()


def test_fts_or_hits_where_like_phrase_misses(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store, text="请帮我查询订单状态")
    owner = owner_key(chat)
    assert ids(store.query(owner, q="订单 状态")) == ["msg-one"]
    assert store.last_query_mode() == "fts5"
    store._fts_available = False
    assert store.query(owner, q="订单 状态") == []
    assert store.last_query_mode() == "like"
    store.close()


def test_search_is_isolated_by_instance_group_and_environment(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store, text="请帮我查询订单状态")
    recorded(config, store, message_id="other-group", target="other-group", text="请帮我查询订单状态")
    sibling = dict(config)
    sibling["id"] = "other-v2"
    other = observed(sibling, text="请帮我查询订单状态")
    assert store.record(other) is True
    owner = owner_key(chat)
    rows = store.query(owner, q="订单", target="group-one")
    assert ids(rows) == ["msg-one"]
    assert {row["target"] for row in rows} == {"group-one"}
    assert {row["event"]["_qq"]["platform_id"] for row in store.query(owner, q="订单")} == {chat.identity.platform_id}
    assert ids(store.query(owner_key(other), q="订单")) == ["msg-one"]
    sandbox = InstanceKey(chat.identity.platform_id, RobotKey(chat.identity.robot.appid, "sandbox")).settings_key
    assert store.query(sandbox, q="订单") == []
    store.close()


def test_delete_range_and_single_row_remove_index(config, tmp_path):
    store, _ = history_store(tmp_path)
    first = recorded(config, store, message_id="keep", text="请帮我查询订单状态")
    recorded(config, store, message_id="gone", target="other-group", text="请帮我查询订单状态")
    owner = owner_key(first)
    assert set(ids(store.query(owner, q="订单"))) == {"keep", "gone"}
    store.delete_range(owner, target="other-group")
    assert ids(store.query(owner, q="订单")) == ["keep"]
    assert fts_orphans(store) == 0
    store.delete_range(owner, since=NOW, until=NOW)
    assert store.query(owner, q="订单") == []
    assert fts_orphans(store) == 0
    store.close()


def test_purge_and_policy_eviction_remove_index(config, tmp_path):
    store, _ = history_store(tmp_path)
    first = recorded(config, store, message_id="old", text="请帮我查询订单状态", timestamp=NOW)
    recorded(config, store, message_id="new", text="请帮我查询订单状态", timestamp=NOW + 10)
    owner = owner_key(first)
    store.save_policy(owner, {"enabled": True, "mode": "max_count", "max_count": 1})
    assert ids(store.query(owner, q="订单")) == ["new"]
    assert fts_orphans(store) == 0
    sibling = dict(config)
    sibling["id"] = "other-v2"
    other = observed(sibling, text="请帮我查询订单状态", message_id="other")
    assert store.record(other) is True
    store.purge(owner)
    assert store.query(owner, q="订单") == []
    assert ids(store.query(owner_key(other), q="订单")) == ["other"]
    assert fts_orphans(store) == 0
    store.close()


def test_fts_unavailable_falls_back_to_like(config, tmp_path, monkeypatch):
    def fail(self, if_not_exists=True):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(HistoryStore, "_create_fts_table", fail)
    store, _ = history_store(tmp_path)
    chat = recorded(config, store, text="请帮我查询订单状态")
    assert store.search_mode() == "like"
    assert ids(store.query(owner_key(chat), q="订单")) == ["msg-one"]
    assert store.last_query_mode() == "like"
    assert store.status(owner_key(chat))["search"]["mode"] == "like"
    store.close()


def test_existing_db_backfill_indexes_old_messages(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store, text="请帮我查询订单状态")
    path = tmp_path / "history.sqlite3"
    store.close()
    raw = sqlite3.connect(path)
    raw.execute(f"DROP TABLE {FTS_TABLE}")
    raw.commit()
    raw.close()
    restored = HistoryStore(path, clock=lambda: NOW)
    assert restored.search_mode() == "fts5"
    assert ids(restored.query(owner_key(chat), q="订单")) == ["msg-one"]
    assert restored.search_pending(owner_key(chat)) == 0
    restored.close()


def test_index_write_failure_does_not_block_record(config, tmp_path):
    store, _ = history_store(tmp_path)
    original = store.db

    class Boom:
        def execute(self, sql, params=()):
            if isinstance(sql, str) and "INSERT INTO" in sql and f"{FTS_TABLE}(rowid" in sql:
                raise sqlite3.OperationalError("disk I/O error")
            return original.execute(sql, params)

        def __enter__(self):
            return original.__enter__()

        def __exit__(self, *args):
            return original.__exit__(*args)

        def __getattr__(self, name):
            return getattr(original, name)

    store.db = Boom()
    chat = observed(config, text="请帮我查询订单状态")
    assert store.record(chat) is True
    assert store.get_msg(owner_key(chat), "msg-one")["message_id"] == "msg-one"
    assert store._search_error == "history_search_index_failed"
    store.db = original
    store.close()


def test_search_does_not_write_send_ledger(config, tmp_path):
    messages = MessageStore(tmp_path / "messaging.sqlite3", clock=lambda: NOW)
    history, _ = history_store(tmp_path)
    chat = recorded(config, history, text="请帮我查询订单状态")
    messages.observe(chat)
    messages.mark_delivered(chat)
    messages.reserve(chat.route, None, "digest", "search-op")
    messages.mark_in_flight(chat.route.robot, "search-op")
    messages.finish(chat.route.robot, "search-op", "unknown")
    ops = messages.db.execute("SELECT count(*) FROM operations").fetchone()[0]
    deliveries = messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0]
    tombs = history.db.execute("SELECT count(*) FROM tombs").fetchone()[0]
    assert ids(history.query(owner_key(chat), q="订单")) == ["msg-one"]
    history.status(owner_key(chat))
    assert messages.db.execute("SELECT count(*) FROM operations").fetchone()[0] == ops
    assert messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == deliveries
    assert history.db.execute("SELECT count(*) FROM tombs").fetchone()[0] == tombs
    history.close()
    messages.close()


def test_control_query_reports_search_mode(config, tmp_path, monkeypatch):
    from v2 import web_api as web_api_mod

    store, _ = history_store(tmp_path)
    chat = recorded(config, store, text="请帮我查询订单状态")
    identity = InstanceKey.from_config(config)
    assert identity.settings_key == owner_key(chat)
    owner = SimpleNamespace(
        stopping=False,
        config={"webui_enabled": True, "remote_menu_sync": False},
        context=SimpleNamespace(get_config=lambda: {"platform": [config]}),
        history=store,
        messages=SimpleNamespace(db=SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: (1,)))),
    )
    api = ControlAPI(owner)
    monkeypatch.setattr(web_api_mod, "require_admin", lambda ctx: "admin")
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="GET", query={"platform_id": config["id"]}))
    status = api.dispatch_history("history_status", "admin", {}, identity, "fp")
    assert status["search"]["mode"] == "fts5"
    assert status["search"]["pending"] == 0
    monkeypatch.setattr(
        web_api_mod, "request",
        SimpleNamespace(method="GET", query={"platform_id": config["id"], "q": "订单"}),
    )
    rows = api.dispatch_history("history_query", "admin", {}, identity, "fp")
    assert rows["search_mode"] == "fts5"
    assert ids(rows["entries"]) == [chat.source.message_id]
    assert rows["entries"][0]["event"]["raw_message"] == chat.message.message_str
    store.close()


def test_export_applies_q_message_id_and_instance_isolation(config, tmp_path):
    store, _ = history_store(tmp_path)
    hit = recorded(config, store, message_id="hit", text="请帮我查询订单状态")
    recorded(config, store, message_id="miss", text="today weather is fine")
    owner = owner_key(hit)
    sibling = dict(config)
    sibling["id"] = "other-v2"
    other = observed(sibling, message_id="other", text="请帮我查询订单状态")
    assert store.record(other) is True
    by_q = store.export_rows(owner, q="订单")
    assert ids(by_q["messages"]) == ["hit"]
    assert by_q["forwards"] == []
    by_id = store.export_rows(owner, message_id="miss")
    assert ids(by_id["messages"]) == ["miss"]
    empty = store.export_rows(owner, q="zzzznomatch")
    assert empty["messages"] == [] and empty["forwards"] == []
    other_export = store.export_rows(owner_key(other), q="订单")
    assert ids(other_export["messages"]) == ["other"]
    store.close()


def test_export_search_hits_match_fts_and_like(config, tmp_path):
    store, _ = history_store(tmp_path)
    recorded(config, store, message_id="hit", text="请帮我查询订单状态")
    miss = recorded(config, store, message_id="miss", text="today weather is fine")
    owner = owner_key(miss)
    assert store.search_mode() == "fts5"
    fts = store.export_rows(owner, q="订单")
    assert ids(fts["messages"]) == ["hit"]
    store._fts_available = False
    like = store.export_rows(owner, q="订单")
    assert ids(like["messages"]) == ["hit"]
    assert store.last_query_mode() == "like"
    store.close()


def test_export_forwards_only_for_filtered_messages(config, tmp_path):
    store, _ = history_store(tmp_path)
    hit = forwarded(config, store, message_id="hit", text="请帮我查询订单状态", nested_id="hit-fwd")
    forwarded(config, store, message_id="miss", text="today weather is fine", nested_id="miss-fwd")
    exported = store.export_rows(owner_key(hit), q="订单")
    assert ids(exported["messages"]) == ["hit"]
    assert {row["source_message_id"] for row in exported["forwards"]} == {"hit"}
    assert "hit-fwd" in {row["forward_id"] for row in exported["forwards"]}
    assert "miss-fwd" not in {row["forward_id"] for row in exported["forwards"]}
    by_id = store.export_rows(owner_key(hit), message_id="miss")
    assert ids(by_id["messages"]) == ["miss"]
    assert {row["source_message_id"] for row in by_id["forwards"]} == {"miss"}
    store.close()


def test_control_export_passes_q_and_message_id(config, tmp_path, monkeypatch):
    from v2 import web_api as web_api_mod

    store, _ = history_store(tmp_path)
    hit = recorded(config, store, message_id="hit", text="请帮我查询订单状态")
    recorded(config, store, message_id="miss", text="today weather is fine")
    identity = InstanceKey.from_config(config)
    owner = SimpleNamespace(
        stopping=False,
        config={"webui_enabled": True, "remote_menu_sync": False},
        context=SimpleNamespace(get_config=lambda: {"platform": [config]}),
        history=store,
        messages=SimpleNamespace(db=SimpleNamespace(execute=lambda *a, **k: SimpleNamespace(fetchone=lambda: (1,)))),
    )
    api = ControlAPI(owner)
    monkeypatch.setattr(web_api_mod, "require_admin", lambda ctx: "admin")
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="POST", query={}))
    by_q = api.dispatch_history(
        "history_export", "admin",
        {"fingerprint": "fp", "confirm": True, "q": "订单", "limit": 1000},
        identity, "fp",
    )
    assert ids(by_q["messages"]) == [hit.source.message_id]
    by_keyword = api.dispatch_history(
        "history_export", "admin",
        {"fingerprint": "fp", "confirm": True, "keyword": "订单", "limit": 1000},
        identity, "fp",
    )
    assert ids(by_keyword["messages"]) == [hit.source.message_id]
    by_id = api.dispatch_history(
        "history_export", "admin",
        {"fingerprint": "fp", "confirm": True, "message_id": "miss", "limit": 1000},
        identity, "fp",
    )
    assert ids(by_id["messages"]) == ["miss"]
    store.close()


def test_invalid_query_text_rejected(config, tmp_path):
    store, _ = history_store(tmp_path)
    chat = recorded(config, store)
    with pytest.raises(V2Error) as exc:
        store.query(owner_key(chat), q=1)
    assert exc.value.code == "invalid_request"
    with pytest.raises(V2Error) as too_long:
        store.query(owner_key(chat), q="x" * 513)
    assert too_long.value.code == "invalid_request"
    with pytest.raises(V2Error) as export_q:
        store.export_rows(owner_key(chat), q=1)
    assert export_q.value.code == "invalid_request"
    store.close()
