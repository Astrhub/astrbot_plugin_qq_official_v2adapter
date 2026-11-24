"""Message-state retention and entry caps live in messaging.sqlite3 and apply on save."""
from types import SimpleNamespace

import pytest

from v2.errors import V2Error
from v2.messaging.store import MESSAGE_POLICY_DEFAULTS, MessageStore, robot_key
from v2.models import RobotKey
from v2.web_api import ControlAPI


def test_message_policy_defaults_to_seven_days_and_survives_restart(tmp_path):
    path = tmp_path / "messaging.sqlite3"
    store = MessageStore(path)
    assert store.policy() == MESSAGE_POLICY_DEFAULTS
    assert store.retention_seconds == 7 * 86400
    store.close()
    restored = MessageStore(path, identity_capacity=8)
    assert restored.policy() == MESSAGE_POLICY_DEFAULTS
    restored.close()


def test_message_policy_constructor_values_seed_the_first_open(tmp_path):
    store = MessageStore(tmp_path / "messaging.sqlite3", identity_capacity=8, retention_seconds=7200)
    assert (store.identity_capacity, store.retention_seconds) == (8, 7200)
    store.close()


def test_message_policy_save_trims_and_survives_reopen(tmp_path):
    path = tmp_path / "messaging.sqlite3"
    store = MessageStore(path)
    with store.transaction():
        for index in range(300):
            store.db.execute("INSERT INTO identities VALUES(?,?,?,?,?,?,?,?)",
                             (robot_key(RobotKey("app")), "member_openid", "group:g", f"u{index}",
                              "{}", store.now(), store.now() - index, ""))
    saved = store.save_policy({"retention_seconds": 3600, "identity_capacity": 256,
                               "source_capacity": 1024, "operation_capacity": 1024})
    assert saved["policy"]["retention_seconds"] == 3600
    assert store.retention_seconds == 3600
    assert store.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 256
    store.close()
    restored = MessageStore(path)
    assert restored.policy()["identity_capacity"] == 256
    restored.close()


def test_message_policy_rejects_incomplete_and_out_of_bounds_saves(tmp_path):
    store = MessageStore(tmp_path / "messaging.sqlite3")
    with pytest.raises(V2Error) as incomplete:
        store.save_policy({"retention_seconds": 7200})
    assert incomplete.value.code == "invalid_message_policy"
    for override in ({"retention_seconds": 60}, {"identity_capacity": 1}, {"operation_capacity": "32768"}):
        with pytest.raises(V2Error) as invalid:
            store.save_policy({**MESSAGE_POLICY_DEFAULTS, **override})
        assert invalid.value.code == "invalid_message_policy"
    store.close()


def test_control_message_state_api_is_plugin_scoped(tmp_path):
    store = MessageStore(tmp_path / "messaging.sqlite3")
    api = ControlAPI(SimpleNamespace(messages=store))
    status = api.dispatch("state_status", "admin", {})
    assert status["plugin_scoped"] is True
    assert status["policy"]["retention_seconds"] == 7 * 86400
    assert status["bounds"]["retention_seconds"] == {"min": 3600, "max": 30 * 86400}
    with pytest.raises(V2Error) as confirm:
        api.dispatch("state_policy", "admin", {"policy": {**status["policy"], "retention_seconds": 7200}})
    assert confirm.value.code == "confirmation_required"
    saved = api.dispatch("state_policy", "admin", {
        "confirm": True,
        "policy": {**status["policy"], "retention_seconds": 7200},
    })
    assert saved["policy"]["retention_seconds"] == 7200
    assert store.retention_seconds == 7200
    store.close()
