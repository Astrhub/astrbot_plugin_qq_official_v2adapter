"""Tests must not import AstrBot against an operator's runtime directory."""

import os

import pytest

if os.environ.get("V2_TEST_SANDBOX") != "1" or os.environ.get("ASTRBOT_ROOT") != "/work":
    raise RuntimeError("Use bash scripts/test-isolated.sh (isolated root and network namespace required).")


@pytest.fixture
def config():
    return {"id": "test-v2", "type": "qq_official_v2", "enable": True,
            "appid": "test-app", "secret": "fixture-not-a-real-secret",
            "environment": "production", "shard": [0, 1], "intents": 33554432}


@pytest.fixture
def store(tmp_path):
    from v2.settings import SettingsStore
    instance = SettingsStore(tmp_path / "settings.sqlite3")
    yield instance
    instance.close()
