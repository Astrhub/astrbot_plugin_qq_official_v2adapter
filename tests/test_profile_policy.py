"""Plugin-level profile store policy is persisted in profiles.sqlite3 and applies immediately."""
import sqlite3
from types import SimpleNamespace

import pytest

from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey
from v2.profiles.service import Profiles
from v2.profiles.store import DEFAULT_PROFILE_POLICY, ProfileStore
from v2.web_api import ControlAPI


def test_profile_policy_defaults_and_restart(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    store = ProfileStore(path)
    assert store.policy() == DEFAULT_PROFILE_POLICY
    assert store.cooldown_seconds == 600
    store.close()
    restored = ProfileStore(path, legacy={"profile_max_records": 99, "profile_cooldown_seconds": 11})
    assert restored.policy() == DEFAULT_PROFILE_POLICY
    restored.close()
    assert not (tmp_path / "settings.sqlite3").exists()


def test_profile_policy_migrates_legacy_once(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    first = ProfileStore(path, legacy={
        "profile_max_records": 12,
        "profile_max_bytes": 2097152,
        "profile_stale_seconds": 30,
        "profile_cooldown_seconds": 40,
    })
    assert first.policy() == {
        "max_records": 12,
        "max_bytes": 2097152,
        "stale_seconds": 30,
        "cooldown_seconds": 40,
    }
    first.close()
    second = ProfileStore(path, legacy={
        "profile_max_records": 99,
        "profile_max_bytes": 3145728,
        "profile_stale_seconds": 11,
        "profile_cooldown_seconds": 22,
    })
    assert second.policy()["max_records"] == 12
    assert second.cooldown_seconds == 40
    second.close()


def test_profile_policy_rejects_invalid_legacy_and_stored_row(tmp_path):
    with pytest.raises(V2Error) as illegal:
        ProfileStore(tmp_path / "bad.sqlite3", legacy={"profile_max_records": 0})
    assert illegal.value.code == "invalid_profile_config"
    path = tmp_path / "profiles.sqlite3"
    store = ProfileStore(path)
    store.close()
    db = sqlite3.connect(path)
    db.execute("UPDATE profile_policy SET max_records=0")
    db.commit()
    db.close()
    with pytest.raises(V2Error) as stored:
        ProfileStore(path)
    assert stored.value.code == "invalid_profile_config"


def test_profile_policy_applies_immediately(config, tmp_path):
    store = ProfileStore(tmp_path / "profiles.sqlite3", max_profiles=8)
    views = Profiles(InstanceKey.from_config(config), store, object())
    assert views.cooldown == 600
    saved = store.save_policy({
        "max_records": 1,
        "max_bytes": 2097152,
        "stale_seconds": 10,
        "cooldown_seconds": 7,
    })
    assert saved["policy"]["stale_seconds"] == 10
    assert store.stale_seconds == 10
    assert store.max_profiles == 1
    assert views.cooldown == 7
    page = store.db.execute("PRAGMA page_size").fetchone()[0]
    assert store.db.execute("PRAGMA max_page_count").fetchone()[0] == 2097152 // page
    store.merge(RobotKey("app"), "group", "g1", "u1", {"nickname": "one"}, source="event", as_of=1000.0)
    with pytest.raises(V2Error) as capacity:
        store.merge(RobotKey("app"), "group", "g1", "u2", {"nickname": "two"}, source="event", as_of=1000.0)
    assert capacity.value.code == "cache_capacity"
    store.close()


def test_profile_policy_rejects_max_bytes_below_usage(tmp_path):
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    blob = "x" * 2000
    for index in range(800):
        assert store.merge(RobotKey("app"), "group", "g1", f"u{index}", {"nickname": blob}, source="event", as_of=1000.0) is not False
    used = store.used_bytes()
    assert used > 1048576
    with pytest.raises(V2Error) as below:
        store.save_policy({**store.policy(), "max_bytes": 1048576})
    assert below.value.code == "profile_limit_below_usage"
    assert store.max_bytes == 67108864
    store.close()


def test_profile_policy_rejects_incomplete_save(tmp_path):
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    with pytest.raises(V2Error) as incomplete:
        store.save_policy({"max_records": 12})
    assert incomplete.value.code == "invalid_profile_config"
    store.close()


def test_control_profile_api_plugin_scoped(tmp_path):
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    api = ControlAPI(SimpleNamespace(profiles=store))
    status = api.dispatch("profile_status", "admin", {})
    assert status["plugin_scoped"] is True
    assert status["policy"]["cooldown_seconds"] == 600
    with pytest.raises(V2Error) as confirm:
        api.dispatch("profile_policy", "admin", {"policy": {**status["policy"], "stale_seconds": 12}})
    assert confirm.value.code == "confirmation_required"
    saved = api.dispatch("profile_policy", "admin", {
        "confirm": True,
        "policy": {**status["policy"], "stale_seconds": 12, "cooldown_seconds": 8},
    })
    assert saved["policy"]["stale_seconds"] == 12
    assert store.stale_seconds == 12
    assert store.cooldown_seconds == 8
    store.close()


@pytest.mark.asyncio
async def test_control_profile_api_requires_admin(monkeypatch, tmp_path):
    from test_web_api import parse

    from v2 import web_api as web_api_mod
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    api = ControlAPI(SimpleNamespace(stopping=False, config={"webui_enabled": True}, context=object(), profiles=store))
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="GET", query={}))
    response = await api.handle("profile_status")
    payload = parse(response)
    assert response.status_code == 403 and payload["code"] == "management_auth_required"
    store.close()
