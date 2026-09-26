"""Fixed OpenAPI requests and server-discovered WebSocket destinations."""
import asyncio
import copy
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import web
from test_lifecycle import context
from test_lifecycle import plugin_module as plugin_module
from test_onboarding import owner as owner
from test_transport_http import MappedSession, upstream
from test_transport_receive import HELLO, READY, FakeGatewayHTTP, FakeWS, gateway_document

from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey
from v2.onboarding import PORTAL, Onboarding
from v2.protocol import RequestSpec, avatar_url
from v2.transport.http import HTTPTransport
from v2.transport.inbox import Ingress, RawInbox
from v2.transport.websocket import Gateway


@pytest.mark.parametrize("gateway_url", [
    "wss://api.bot.qq.com/websocket/",
    "wss://api.sgroup.qq.com/websocket",
    "wss://edge.gateway.example:8443/events?route=one%2Ftwo",
])
async def test_documented_token_api_gateway_and_identify_destinations(config, tmp_path, gateway_url):
    seen = []
    async def handler(request):
        seen.append(request.path)
        if request.path == "/app/getAppAccessToken":
            assert not request.query and "Authorization" not in request.headers
            assert await request.json() == {"appId": config["appid"], "clientSecret": config["secret"]}
            return web.json_response({"access_token": "endpoint-fixture-token", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot endpoint-fixture-token"
        if request.path == "/gateway/bot":
            assert not request.query
            return web.json_response(gateway_document(gateway_url))
        assert request.path == "/v2/items" and request.query.getall("ids") == ["a", "b"]
        assert request.query["cursor"] == "a+b"
        return web.json_response([])
    socket = FakeWS([HELLO, READY, 4014])
    class Session(MappedSession):
        async def ws_connect(self, url, **kwargs):
            assert url == gateway_url
            assert "Authorization" not in kwargs["headers"]
            return socket
    inbox = RawInbox(tmp_path / "inbox")
    ingress = Ingress(inbox, "endpoint-owner")
    ingress.start()
    async with upstream(handler) as base:
        session = Session(base)
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: session)
        gateway = Gateway(http, ingress)
        try:
            result = await http.request(RequestSpec("production", "GET", "/v2/items", {"cursor": "a+b", "ids": ["a", "b"]}))
            assert result.data == []
            with pytest.raises(V2Error) as exc:
                await gateway.run()
            assert exc.value.business_code == 4014
            assert [(m, u) for m, u, _ in session.calls] == [
                ("POST", "https://api.bot.qq.com/app/getAppAccessToken"),
                ("GET", "https://api.bot.qq.com/v2/items?cursor=a%2Bb&ids=a&ids=b"),
                ("GET", "https://api.bot.qq.com/gateway/bot"),
            ]
            for _, url, _ in session.calls:
                assert urlsplit(url).scheme == "https" and urlsplit(url).hostname == "api.bot.qq.com"
                assert config["secret"] not in url and "endpoint-fixture-token" not in url
            assert parse_qs(urlsplit(session.calls[1][1]).query) == {"cursor": ["a+b"], "ids": ["a", "b"]}
            assert socket.sent[0] == {"op": 2, "d": {"token": "QQBot endpoint-fixture-token", "intents": config["intents"], "shard": config["shard"], "properties": {}}}
            assert seen == ["/app/getAppAccessToken", "/v2/items", "/gateway/bot"]
        finally:
            await gateway.close()
            await http.close()
            await ingress.close()
            inbox.close()
        assert session.closed and socket.closed


BAD_HTTPS = [
    "https://api.sgroup.qq.com/items", "https://sandbox.api.sgroup.qq.com/items",
    "https://sandbox.api.bot.qq.com/items", "http://api.bot.qq.com/items",
    "https://api.bot.qq.com.evil.invalid/items", "https://api.bot.qq.com@evil.invalid/items",
    "https://@api.bot.qq.com/items", "https://api.bot.qq.com:444/items",
    "https://api.bot.qq.com/items#fragment", "https://q.qq.com/items", "https://q.qlogo.cn/items",
]


@pytest.mark.parametrize("url", BAD_HTTPS)
async def test_https_rejects_legacy_guessed_and_wrong_origins_before_credentials(config, url):
    def forbidden_session():
        pytest.fail("Unapproved destination created a network session")
    http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=forbidden_session)
    forged = SimpleNamespace(environment="production", method="GET", url=url, path="/items", json_body=None, multipart=None)
    try:
        with pytest.raises(V2Error) as exc:
            await http.request(forged)
        assert exc.value.code == "invalid_request" and exc.value.phase == "not_sent"
        # The lower-level token/request path also checks destinations before serializing credentials.
        with pytest.raises(V2Error) as exc:
            await http._exchange("POST", url, json_body={"clientSecret": config["secret"]})
        assert exc.value.code == "invalid_request" and config["secret"] not in str(exc.value)
        assert http.session is None and http._refresh is None
    finally:
        await http.close()


@pytest.mark.parametrize("data", [None, [], {}, {"url": None}, {"url": ""}, {"url": 123}, {"url": {}}])
async def test_gateway_response_requires_a_nonempty_url(config, data):
    class HTTP(FakeGatewayHTTP):
        async def request(self, spec):
            assert spec.url == "https://api.bot.qq.com/gateway/bot"
            return SimpleNamespace(data=data)
    http = HTTP(InstanceKey.from_config(config), [])
    gateway = Gateway(http, SimpleNamespace(last_sequence=None))
    with pytest.raises(V2Error) as exc:
        await gateway.run()
    assert exc.value.code == "invalid_gateway" and http.connects == 0 and gateway.attempts == 1


@pytest.mark.parametrize("entrypoint", ["start", "token", "request", "exchange"])
async def test_sandbox_fails_before_session_or_token_creation(config, entrypoint):
    def forbidden_session():
        pytest.fail("Unconfirmed sandbox created a network session")
    identity = InstanceKey.from_config({**config, "environment": "sandbox"})
    http = HTTPTransport(identity, config["secret"], session_factory=forbidden_session)
    try:
        with pytest.raises(V2Error) as exc:
            if entrypoint == "start":
                http.start()
            elif entrypoint == "token":
                await http.token()
            elif entrypoint == "request":
                await http.request(RequestSpec("sandbox", "GET", "/gateway/bot"))
            else:
                await http._exchange("POST", "https://api.bot.qq.com/app/getAppAccessToken")
        assert exc.value.code == "unsupported_environment" and exc.value.phase == "not_sent"
        assert http.session is None and http._refresh is None and not http._active
        assert identity.robot != InstanceKey.from_config(config).robot
    finally:
        await http.close()


async def test_sandbox_gateway_has_no_request_or_fallback(config):
    class HTTP(FakeGatewayHTTP):
        async def request(self, spec):
            pytest.fail("Sandbox requested a production gateway")
    http = HTTP(InstanceKey.from_config({**config, "environment": "sandbox"}), [])
    gateway = Gateway(http, SimpleNamespace(last_sequence=None))
    with pytest.raises(V2Error) as exc:
        await gateway.run()
    assert exc.value.code == "unsupported_environment" and http.connects == 0 and gateway.attempts == 1


async def test_sandbox_configuration_is_retained_but_binding_does_not_start(owner, config):
    conn, cfg = owner.connections, owner.context.get_config()
    saved = await conn.save(config["id"], conn.view(config["id"])["fingerprint"],
                            {"environment": "sandbox", "enable": False}, confirm=True, confirm_identity=True)
    original = copy.deepcopy(dict(cfg))
    onboarding = Onboarding(owner, session_factory=lambda: pytest.fail("Sandbox contacted the binding portal"))
    try:
        with pytest.raises(V2Error) as exc:
            await onboarding.start("fixture-admin", config["id"], saved["fingerprint"], confirm=True)
        assert exc.value.code == "unsupported_environment"
        assert dict(cfg) == original and cfg["platform"][0]["is_sandbox"] is True
        assert cfg["platform"][0]["secret"] == config["secret"]
        assert not onboarding.bindings and onboarding.session is None
        assert not owner.context.platform_manager.calls
    finally:
        await onboarding.close()


@pytest.mark.parametrize("mode", ["websocket", "webhook"])
async def test_sandbox_adapter_never_registers_as_ready_or_sends_a_request(plugin_module, config, mode):
    ctx = context()
    cfg = {**config, "environment": "sandbox", "transport": mode, "unified_webhook_mode": mode == "webhook",
           "webhook_uuid": "4c4eb5e4c98340118deec38fed41bd75"}
    ctx.get_config()["platform"].append(cfg)
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    cfg = ctx.get_config()["platform"][0]
    try:
        original = copy.deepcopy(cfg)
        instance = owner.adapter_class(cfg, {}, asyncio.Queue())
        instance.http._factory = lambda: pytest.fail("Sandbox adapter attempted a network session")
        with pytest.raises(RuntimeError) as exc:
            await asyncio.create_task(instance.run())
        assert exc.value.code == "unsupported_environment"
        assert not instance.ready.is_set() and not instance.runtime_status()["online"]
        assert instance.http.session is None and cfg == original
        if mode == "webhook":
            assert (await instance.webhook_callback(None))[1] == 503
    finally:
        await owner.terminate()


@pytest.mark.parametrize("duration", [7200, "7200", 7201, 86400])
async def test_token_lifetime_matches_current_official_limit(config, duration):
    calls = []
    async def handler(request):
        calls.append(request.path)
        return web.json_response({"access_token": "expiry-fixture", "expires_in": duration})
    async with upstream(handler) as base:
        http = HTTPTransport(InstanceKey.from_config(config), config["secret"], session_factory=lambda: MappedSession(base), clock=lambda: 0)
        try:
            if duration in (7200, "7200"):
                assert await http.token() == "expiry-fixture" and http._expires == 7140
            else:
                with pytest.raises(V2Error) as exc:
                    await http.token()
                assert exc.value.code == "invalid_token_response" and not http._token
            assert calls == ["/app/getAppAccessToken"]
        finally:
            await http.close()


def test_portal_and_avatar_keep_their_distinct_official_origins():
    assert PORTAL == "https://q.qq.com"
    assert avatar_url(RobotKey("fixture-app", "production"), "observed-openid") == "https://q.qlogo.cn/qqapp/fixture-app/observed-openid/100"
