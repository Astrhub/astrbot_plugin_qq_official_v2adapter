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


@pytest.fixture(autouse=True)
def forbid_real_qq(monkeypatch):
    from urllib.parse import urlsplit

    import aiohttp
    original = aiohttp.ClientSession._request

    async def guarded(self, method, url, **kwargs):
        host = urlsplit(str(url)).hostname or ""
        if host == "qq.com" or host.endswith(".qq.com"):
            raise AssertionError("Tests must map QQ URLs to explicit local upstream fixtures.")
        return await original(self, method, url, **kwargs)

    monkeypatch.setattr(aiohttp.ClientSession, "_request", guarded)


@pytest.fixture
async def qq_reject_server():
    from aiohttp import web
    from test_transport_http import upstream
    calls = []

    async def handler(request):
        calls.append(request.path)
        assert request.path == "/app/getAppAccessToken"
        return web.json_response({"code": 100016, "message": "invalid fixture credential"})

    async with upstream(handler) as base:
        yield base, calls


@pytest.fixture
async def qq_portal_server():
    from test_onboarding import Portal
    from test_transport_http import upstream
    portal = Portal()
    async with upstream(portal.handle) as base:
        yield base, portal
