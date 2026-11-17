"""ControlAPI logs management errors without secrets, tokens, or request bodies."""
import json
import time
from types import SimpleNamespace

from v2 import web_api as web_api_mod
from v2.web_api import ControlAPI

SENSITIVE = ("fixture-app-secret", "fixture-bearer-token", "secret-body-title")


def parse(response):
    return json.loads(bytes(response.body))


def secret_body(csrf):
    return json.dumps({
        "csrf": csrf,
        "secret": "fixture-app-secret",
        "token": "fixture-bearer-token",
        "patch": {"title": "secret-body-title"},
    }).encode()


class ProbeError(Exception):
    pass


def make_api(monkeypatch):
    holder = {"body": b"{}"}
    owner = SimpleNamespace(
        stopping=False,
        config={"webui_enabled": True, "remote_menu_sync": False},
        context=object(),
    )
    api = ControlAPI(owner)

    async def fake_body(req, limit):
        return holder["body"]

    monkeypatch.setattr(web_api_mod, "require_admin", lambda ctx: "admin")
    monkeypatch.setattr(web_api_mod, "bounded_body", fake_body)
    monkeypatch.setattr(web_api_mod, "request", SimpleNamespace(method="POST", _get_current=lambda: None))
    return api, holder


def assert_log_safe(text, *tokens):
    assert all(item not in text for item in SENSITIVE)
    assert all(token not in text for token in tokens)


async def test_csrf_expired_is_logged_without_token_or_body(monkeypatch, caplog):
    api, holder = make_api(monkeypatch)
    expired = api.csrf("admin", int(time.time()) - 1)
    holder["body"] = secret_body(expired)
    with caplog.at_level("WARNING"):
        response = await api.handle("mutate")
    payload = parse(response)
    assert response.status_code == 403 and payload["code"] == "csrf_expired"
    dumped = json.dumps(payload)
    assert expired not in dumped
    assert all(item not in dumped for item in SENSITIVE)
    text = caplog.text
    assert "qq-v2 control error operation=mutate code=csrf_expired status=403" in text
    assert_log_safe(text, expired)


async def test_unexpected_control_error_is_logged_without_secrets(monkeypatch, caplog):
    api, holder = make_api(monkeypatch)
    csrf = api.csrf("admin")
    holder["body"] = secret_body(csrf)

    def boom(operation, username, body):
        raise ProbeError("boom fixture-app-secret fixture-bearer-token secret-body-title " + body["csrf"])

    monkeypatch.setattr(api, "dispatch", boom)
    with caplog.at_level("ERROR"):
        response = await api.handle("mutate")
    payload = parse(response)
    assert response.status_code == 500 and payload["code"] == "control_internal_error"
    assert all(item not in payload["message"] for item in SENSITIVE)
    assert csrf not in payload["message"]
    text = caplog.text
    assert "qq-v2 control unexpected operation=mutate code=control_internal_error status=500 type=ProbeError" in text
    assert_log_safe(text, csrf)
