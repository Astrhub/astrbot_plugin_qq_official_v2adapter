"""Native read routes, pagination contracts, and owned-view wire boundaries."""

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_transport_http import MappedSession, upstream

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey
from v2.protocol import RequestSpec
from v2.transport.http import HTTPTransport

pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


@pytest.fixture
def client():
    identity = InstanceKey("instance", RobotKey("app"))
    return V2Client(identity, state=ClientState(identity))


READ_CASES = [
    ("get_gateway", (), {}, "https://api.bot.qq.com/gateway", None),
    ("get_gateway_bot", (), {}, "https://api.bot.qq.com/gateway/bot", None),
    ("get_ws_url", (), {}, "https://api.bot.qq.com/gateway/bot", None),
    ("me", (), {}, "https://api.bot.qq.com/users/@me", None),
    ("me_guilds", (), {}, "https://api.bot.qq.com/users/@me/guilds?limit=100", None),
    ("me_guilds", ("g&1", 0), {}, None, None),
    ("me_guilds", ("g/1", 2, True), {}, "https://api.bot.qq.com/users/@me/guilds?limit=2&before=g%2F1", None),
    ("get_message", ("c/1", "m 2"), {}, "https://api.bot.qq.com/channels/c%2F1/messages/m%202", None),
    ("get_group_bot_state", ("g/1",), {}, "https://api.bot.qq.com/v2/groups/g%2F1/bot_state", None),
    ("get_group_restrict_chat_setting", ("g",), {}, "https://api.bot.qq.com/v2/groups/g/restrict_chat_setting", None),
    ("get_group_member_blacklist", ("g",), {}, "https://api.bot.qq.com/v2/groups/g/member_blacklist", None),
    ("get_group_member_blacklist", ("g", "", 0), {}, None, None),
    ("get_group_member_blacklist", ("g", "", 20), {}, "https://api.bot.qq.com/v2/groups/g/member_blacklist?cursor=&limit=20", None),
    ("get_group_join_requests", ("g", "c&+", 4), {}, "https://api.bot.qq.com/v2/groups/g/join_request_list?cursor=c%26%2B&limit=4", None),
    ("get_join_approval_strategies", (), {}, "https://api.bot.qq.com/v2/groups/join_approval_strategy",
     {"strategies": [{"strategy_id": "strategy-1", "is_enable": "on", "group_openids": ["other-group"]}],
      "next_cursor": "", "official_extra": {"zero": 0}}),
    ("get_menu", (), {}, "https://api.bot.qq.com/v2/menu", None),
    ("get_panels", ("group",), {}, "https://api.bot.qq.com/v2/panels?scope=group", None),
    ("get_panels", ("dm", "", 2), {}, "https://api.bot.qq.com/v2/panels?scope=dm&cursor=&limit=2", None),
    ("get_panel", ("p/1",), {}, "https://api.bot.qq.com/v2/panels/p%2F1", None),
    ("get_guild", ("g/1",), {}, "https://api.bot.qq.com/guilds/g%2F1", None),
    ("get_channels", ("g",), {}, "https://api.bot.qq.com/guilds/g/channels", []),
    ("get_channel", ("c",), {}, "https://api.bot.qq.com/channels/c", None),
    ("get_guild_members", ("g",), {}, "https://api.bot.qq.com/guilds/g/members?after=0&limit=1", []),
    ("get_guild_member", ("g", "u/1"), {}, "https://api.bot.qq.com/guilds/g/members/u%2F1", None),
    ("get_guild_role_members", ("g", "role"), {}, "https://api.bot.qq.com/guilds/g/roles/role/members?start_index=0&limit=1", None),
    ("get_voice_members", ("c",), {}, "https://api.bot.qq.com/channels/c/voice/members", []),
    ("get_channel_online_nums", ("c",), {}, "https://api.bot.qq.com/channels/c/online_nums", None),
    ("get_guild_message_setting", ("g",), {}, "https://api.bot.qq.com/guilds/g/message/setting", None),
    ("get_guild_roles", ("g",), {}, "https://api.bot.qq.com/guilds/g/roles", None),
    ("get_channel_user_permissions", ("c", "u"), {}, "https://api.bot.qq.com/channels/c/members/u/permissions", None),
    ("get_channel_role_permissions", ("c", "r"), {}, "https://api.bot.qq.com/channels/c/roles/r/permissions", None),
    ("get_permissions", ("g",), {}, "https://api.bot.qq.com/guilds/g/api_permission", []),
    ("get_pins", ("c",), {}, "https://api.bot.qq.com/channels/c/pins", None),
    ("get_reaction_users", ("c", "m", 1, "203"), {}, "https://api.bot.qq.com/channels/c/messages/m/reactions/1/203?limit=20", None),
    ("get_reaction_users", ("c", "m", 2, "1", "", 1), {}, "https://api.bot.qq.com/channels/c/messages/m/reactions/2/1?limit=1&cookie=", None),
    ("get_schedules", ("c",), {}, "https://api.bot.qq.com/channels/c/schedules", []),
    ("get_schedules", ("c", "0"), {}, "https://api.bot.qq.com/channels/c/schedules", []),
    ("get_schedule", ("c", "s"), {}, "https://api.bot.qq.com/channels/c/schedules/s", None),
    ("get_threads", ("c",), {}, "https://api.bot.qq.com/channels/c/threads", None),
    ("get_thread_detail", ("c", "t"), {}, "https://api.bot.qq.com/channels/c/threads/t", None),
]


async def test_native_read_routes_reach_literal_https_destination_and_preserve_raw_response(client):
    current_payload = [None]

    class Response:
        status = 200
        headers = {}

        @property
        def content(self):
            class Stream:
                async def iter_chunked(self, size):
                    yield json.dumps(current_payload[0]).encode()
            return Stream()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def __init__(self):
            self.calls = []

        def request(self, method, destination, **options):
            self.calls.append((method, destination, options))
            return Response()

        async def close(self):
            pass

    session = Session()

    async def token_provider(*, rejected=None):
        return "fixture-token"

    transport = HTTPTransport(client.identity, "", session_factory=lambda: session, token_provider=token_provider)
    client._state.http = transport
    try:
        for name, args, kwargs, url, shape in READ_CASES:
            session.calls.clear()
            current_payload[0] = shape if shape is not None else {"official_extra": {"opaque": "001", "route": name}}
            if url is None:
                with pytest.raises(V2Error) as exc:
                    await getattr(client.qq, name)(*args, **kwargs)
                assert exc.value.code == "invalid_limit" and not session.calls
                continue
            assert await getattr(client.qq, name)(*args, **kwargs) == current_payload[0]
            assert len(session.calls) == 1
            assert session.calls[0][0:2] == ("GET", url)
            assert session.calls[0][2]["headers"]["Authorization"] == "QQBot fixture-token"
            if name == "get_schedules" and args == ("c", "0"):
                assert session.calls[0][2]["json"] == {"since": 0}
            else:
                assert "json" not in session.calls[0][2]
            if name not in {"get_group_info", "get_group_member_info", "get_group_member_list"}:
                assert getattr(client.api, name) is not None
    finally:
        await transport.close()


async def test_native_reads_stay_outside_network_onebot_action_list_and_propagate_errors(client):
    async def no_request(spec):
        raise AssertionError("network-only action must not run")

    client._state.http = SimpleNamespace(request=no_request)
    with pytest.raises(V2Error) as exc:
        await client.call_action("get_guild", guild_id="g")
    assert exc.value.code == "unsupported"
    assert "get_guild" not in client.capabilities()["actions"]

    for status, code, phase in (
        (400, "qq_api_error", "rejected"),
        (429, "qq_rate_limited", "rejected"),
        (502, "qq_api_error", "result_unknown"),
        (403, "qq_api_error", "rejected"),
    ):
        calls = []

        class HTTP:
            async def request(self, spec, *, before_send=None):
                if before_send is not None:
                    before_send()
                calls.append((spec.method, spec.url))
                raise V2Error(code, "fixture", status=status, business_code=11253 if status == 400 else None,
                              phase=phase, http_status=status)

        client._state.http = HTTP()
        with pytest.raises(V2Error) as err:
            await client.qq.get_guild("g")
        assert (err.value.code, err.value.phase) == (code, phase)
        assert calls == [("GET", "https://api.bot.qq.com/guilds/g")]

    async def onebot(action, **params):
        assert action == "get_group_info" and params == {"group_id": "g"}
        return {"group_id": "g", "group_name": "OneBot"}

    client.call_action = onebot
    assert (await client.api.get_group_info(group_id="g"))["group_name"] == "OneBot"
    assert inspect.signature(client.qq.me_guilds).parameters["limit"].default == 100
    assert inspect.signature(client.qq.get_guild_members).parameters["after"].default == "0"

    class CancelHTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            raise asyncio.CancelledError

    client._state.http = CancelHTTP()
    with pytest.raises(asyncio.CancelledError):
        await client.qq.get_guild_member("g", "u")


@pytest.mark.parametrize("last", [[], [{"strategy_id": "strategy-2", "is_enable": "off"}]])
async def test_official_strategy_page_and_two_page_iterator_preserve_cursor_and_raw_fields(client, last):
    first = {"strategies": [{"strategy_id": "strategy-1", "group_openids": ["other-group"]}],
             "next_cursor": "next&1", "official_extra": {"zero": 0}}
    second = {"strategies": last, "next_cursor": "", "official_extra": False}
    calls = []

    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            calls.append((spec.method, spec.url, spec.json_body))
            return SimpleNamespace(data=second if spec.params and spec.params.get("cursor") == "next&1" else first)

    client._state.http = HTTP()
    assert await client.qq.get_join_approval_strategies() is first
    assert [row async for row in client.qq.iter_join_approval_strategies()] == first["strategies"] + last
    base = "https://api.bot.qq.com/v2/groups/join_approval_strategy"
    assert calls == [("GET", base, None), ("GET", base + "?cursor=", None),
                     ("GET", base + "?cursor=next%261", None)]


async def test_strategy_page_missing_official_fields_and_cursor_repetition_fail_explicitly(client):
    for malformed in (
        {"list": [{"strategy_id": "wrong-field"}], "next_cursor": ""},
        {"next_cursor": ""},
        {"strategies": None, "next_cursor": ""},
        {"strategies": []},
    ):
        async def request(spec, *, before_send=None, _malformed=malformed):
            if before_send is not None:
                before_send()
            assert spec.method == "GET" and spec.url.startswith("https://api.bot.qq.com/v2/groups/join_approval_strategy")
            return SimpleNamespace(data=_malformed)

        client._state.http = SimpleNamespace(request=request)
        with pytest.raises(V2Error) as direct:
            await client.qq.get_join_approval_strategies()
        assert direct.value.code == "pagination_incomplete"
        with pytest.raises(V2Error) as iterated:
            [row async for row in client.qq.iter_join_approval_strategies()]
        assert iterated.value.code == "pagination_incomplete"

    pages = [{"users": [{"member_openid": "u1"}], "next_cursor": "repeat"},
             {"users": [{"member_openid": "u2"}], "next_cursor": "repeat"}]

    class RepeatHTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            assert spec.method == "GET" and spec.url.startswith("https://api.bot.qq.com/v2/groups/g/member_blacklist")
            return SimpleNamespace(data=pages.pop(0))

    client._state.http = RepeatHTTP()
    seen = []
    with pytest.raises(V2Error) as exc:
        async for row in client.qq.iter_group_member_blacklist("g"):
            seen.append(row["member_openid"])
    assert seen == ["u1", "u2"] and exc.value.code == "pagination_incomplete"


async def test_role_guild_member_iterators_and_threads_cursor_contracts(client):
    urls = []

    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            urls.append(spec.url)
            if "/roles/" in spec.url:
                data = {"data": [{"user": {"id": "001"}}], "next": "opaque"} if len(urls) == 1 else {"data": [], "next": "0"}
            else:
                data = [{"user": {"id": "001"}}] if len(urls) == 3 else []
            return SimpleNamespace(data=data)

    client._state.http = HTTP()
    assert [row async for row in client.qq.iter_guild_role_members("g", "r")] == [{"user": {"id": "001"}}]
    assert [row async for row in client.qq.iter_guild_members("g")] == [{"user": {"id": "001"}}]
    assert urls == ["https://api.bot.qq.com/guilds/g/roles/r/members?start_index=0&limit=400",
                    "https://api.bot.qq.com/guilds/g/roles/r/members?start_index=opaque&limit=400",
                    "https://api.bot.qq.com/guilds/g/members?after=0&limit=400",
                    "https://api.bot.qq.com/guilds/g/members?after=001&limit=400"]

    async def thread_request(spec, *, before_send=None):
        if before_send is not None:
            before_send()
        return SimpleNamespace(data={"threads": [], "is_finish": 0})

    client._state.http = SimpleNamespace(request=thread_request)
    assert (await client.qq.get_threads("c"))["is_finish"] == 0
    with pytest.raises(V2Error, match="continuation"):
        await client.qq.complete_threads("c")


@pytest.fixture
async def read_wire(receiver):
    owner, instance = receiver
    calls = []
    token_entered, token_release = asyncio.Event(), asyncio.Event()
    business_entered, business_release = asyncio.Event(), asyncio.Event()
    token_blocked, business_blocked, retry = [False], [False], [False]

    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            if token_blocked[0]:
                token_entered.set()
                await token_release.wait()
            return web.json_response({"access_token": "read-fixture", "expires_in": 7200})
        calls.append((request.method, request.path, dict(request.query)))
        assert request.headers["Authorization"] == "QQBot read-fixture"
        if business_blocked[0]:
            business_entered.set()
            await business_release.wait()
        if retry[0] and len(calls) == 1:
            return web.Response(status=429, headers={"Retry-After": "1"})
        if request.method == "HEAD":
            return web.Response(status=204)
        if request.path == "/v2/groups/g/members/u":
            return web.json_response({"member_openid": "u", "username": "Official", "member_role": "member", "bot": False})
        if request.path == "/v2/groups/g/members":
            if request.query.get("cursor", "") == "":
                return web.json_response({"members": [{"member_openid": "u", "username": "Official",
                                                       "member_role": "member", "bot": False}], "next_cursor": "opaque&1"})
            assert request.query.get("cursor") == "opaque&1"
            return web.json_response({"members": [{"member_openid": "v", "username": "Second",
                                                   "member_role": "member", "bot": False}], "next_cursor": ""})
        if request.path == "/v2/groups/g/info":
            return web.json_response({"group_openid": "g", "group_name": "Actual", "group_member_num": 2})
        if request.path == "/guilds/g":
            return web.json_response({"id": "g", "name": "Guild"})
        assert request.path == "/users/@me"
        return web.json_response({"id": "bot", "username": "Actual"})

    async with upstream(handler) as base:
        instance.http._factory = lambda: MappedSession(base)
        try:
            yield SimpleNamespace(owner=owner, instance=instance, calls=calls, token_blocked=token_blocked,
                token_entered=token_entered, token_release=token_release, business_blocked=business_blocked,
                business_entered=business_entered, business_release=business_release, retry=retry)
        finally:
            token_release.set()
            business_release.set()
            await instance.http.close()


def named_read(view, entry, environment):
    if entry == "mixin":
        return view.me()
    if entry == "member":
        return view.get_group_member_info("g", "u")
    if entry == "page":
        return view.get_group_member_list("g")
    if entry == "iterate":
        async def collect():
            return [row async for row in view.iter_group_members("g")]
        return collect()
    if entry == "group":
        return view.get_group_info("g")
    if entry == "management":
        return view.group_info("g")
    if entry == "management_member":
        return view.group_member("g", "u")
    if entry == "management_members":
        return view.group_members("g")
    if entry == "guild":
        return view.guild_info("g")
    if entry == "request_get":
        return view.request(RequestSpec(environment, "GET", "/users/@me"))
    assert entry == "request_head"
    return view.request(RequestSpec(environment, "HEAD", "/users/@me"))


@pytest.mark.parametrize("entry", ["mixin", "member", "iterate", "management", "request_get", "request_head"])
async def test_revoked_native_read_waiting_for_token_never_starts_business_wire(read_wire, entry):
    p = read_wire
    p.token_blocked[0] = True
    owner = object()
    view = p.instance.client.qq.with_options(owner=owner)
    pending = asyncio.create_task(named_read(view, entry, p.instance.identity.robot.environment))
    try:
        await asyncio.wait_for(p.token_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner))
        p.token_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await pending
        assert invalid.value.code == "stale_owner"
        assert p.calls == []
    finally:
        p.token_release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_native_read_owned_and_unowned_wire_and_pagination(read_wire):
    p = read_wire
    env = p.instance.identity.robot.environment
    for entry, path, method in (
        ("mixin", "/users/@me", "GET"),
        ("member", "/v2/groups/g/members/u", "GET"),
        ("page", "/v2/groups/g/members", "GET"),
        ("group", "/v2/groups/g/info", "GET"),
        ("management", "/v2/groups/g/info", "GET"),
        ("management_member", "/v2/groups/g/members/u", "GET"),
        ("guild", "/guilds/g", "GET"),
        ("request_get", "/users/@me", "GET"),
        ("request_head", "/users/@me", "HEAD"),
    ):
        for kind in ("owned", "unowned"):
            p.calls.clear()
            view = p.instance.client.qq.with_options(owner=object()) if kind == "owned" else p.instance.client.qq
            result = await named_read(view, entry, env)
            assert p.calls == [(method, path, {"cursor": ""} if entry == "page" else {})]
            if entry in {"request_get", "request_head"}:
                assert result.status == (204 if method == "HEAD" else 200)
            elif entry == "page":
                assert result["next_cursor"] == "opaque&1"
            else:
                assert result is not None

    for entry in ("iterate", "management_members"):
        p.calls.clear()
        view = p.instance.client.qq.with_options(owner=object())
        result = await named_read(view, entry, env)
        assert [row["member_openid"] for row in result] == ["u", "v"]
        assert p.calls == [("GET", "/v2/groups/g/members", {"cursor": ""}),
                           ("GET", "/v2/groups/g/members", {"cursor": "opaque&1"})]


async def test_revoked_owner_after_first_page_or_during_retry_and_raw_read_guards(read_wire):
    p = read_wire
    env = p.instance.identity.robot.environment

    view = p.instance.client.qq.with_options(owner=object())
    with pytest.raises(RuntimeError) as denied:
        await view.request(RequestSpec(env, "POST", "/v2/groups/g/messages", json_body={"content": "x"}))
    assert denied.value.code == "unsupported" and not p.calls

    p.business_blocked[0] = True
    owner = object()
    task = asyncio.create_task(named_read(p.instance.client.qq.with_options(owner=owner), "iterate", env))
    try:
        await asyncio.wait_for(p.business_entered.wait(), 2)
        assert p.calls == [("GET", "/v2/groups/g/members", {"cursor": ""})]
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner))
        p.business_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await task
        assert invalid.value.code == "stale_owner"
        assert p.calls == [("GET", "/v2/groups/g/members", {"cursor": ""})]
    finally:
        p.business_blocked[0] = False
        p.business_release.set()

    p.calls.clear()
    p.business_entered.clear()
    p.business_release.clear()
    p.business_blocked[0] = True
    owner2 = object()
    raw_task = asyncio.create_task(p.instance.client.qq.with_options(owner=owner2).request(RequestSpec(env, "GET", "/users/@me")))
    try:
        await asyncio.wait_for(p.business_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner2))
        p.business_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await raw_task
        assert invalid.value.code == "stale_owner"
        assert p.calls == [("GET", "/users/@me", {})]
    finally:
        p.business_blocked[0] = False
        p.business_release.set()

    p.calls.clear()
    p.retry[0] = True
    waiting, release = asyncio.Event(), asyncio.Event()

    async def pause(_delay):
        waiting.set()
        await release.wait()

    p.instance.http.sleep = pause
    owner3 = object()
    retry_task = asyncio.create_task(p.instance.client.qq.with_options(owner=owner3).me())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert p.calls == [("GET", "/users/@me", {})]
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner3))
        release.set()
        with pytest.raises(RuntimeError) as invalid:
            await retry_task
        assert invalid.value.code == "stale_owner" and p.calls == [("GET", "/users/@me", {})]
    finally:
        release.set()
