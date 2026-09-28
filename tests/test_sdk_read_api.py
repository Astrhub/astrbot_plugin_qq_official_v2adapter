"""Native read routes have independent literal origins, parameters and response fixtures."""

import asyncio
import inspect
from types import SimpleNamespace

import pytest

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey
from v2.transport.http import HTTPTransport


@pytest.fixture
def client():
    identity = InstanceKey("instance", RobotKey("app"))
    client = V2Client(identity, state=ClientState(identity))
    return client


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


@pytest.mark.parametrize("name,args,kwargs,url,shape", READ_CASES)
async def test_native_read_has_literal_origin_and_raw_response(client, name, args, kwargs, url, shape):
    calls = []
    payload = shape if shape is not None else {"official_extra": {"opaque": "001"}}
    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            calls.append(spec)
            return SimpleNamespace(data=payload)
    client._state.http = HTTP()
    if url is None:
        with pytest.raises(V2Error) as exc:
            await getattr(client.qq, name)(*args, **kwargs)
        assert exc.value.code == "invalid_limit" and not calls
        return
    result = await getattr(client.qq, name)(*args, **kwargs)
    assert result is payload
    assert len(calls) == 1 and calls[0].method == "GET" and calls[0].url == url
    assert calls[0].json_body == ({"since": 0} if name == "get_schedules" and len(args) == 2 else None)
    if name not in {"get_group_info", "get_group_member_info", "get_group_member_list"}:
        assert getattr(client.api, name) is not None

@pytest.mark.parametrize("name,args,kwargs,url,shape", [case for case in READ_CASES if case[3] is not None])
async def test_native_read_reaches_literal_https_destination(client, name, args, kwargs, url, shape):
    import json
    payload = shape if shape is not None else {"response_marker": name}
    class Response:
        status = 200
        headers = {}
        @property
        def content(self):
            class Stream:
                async def iter_chunked(self, size):
                    yield json.dumps(payload).encode()
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
        assert await getattr(client.qq, name)(*args, **kwargs) == payload
        assert len(session.calls) == 1
        assert session.calls[0][0:2] == ("GET", url)
        assert session.calls[0][2]["headers"]["Authorization"] == "QQBot fixture-token"
        if name == "get_schedules":
            if args == ("c", "0"):
                assert session.calls[0][2]["json"] == {"since": 0}
            else:
                assert "json" not in session.calls[0][2]
    finally:
        await transport.close()



async def test_native_reads_stay_outside_network_onebot_action_list(client):
    async def no_request(spec):
        raise AssertionError("network-only action must not run")
    client._state.http = SimpleNamespace(request=no_request)
    with pytest.raises(V2Error) as exc:
        await client.call_action("get_guild", guild_id="g")
    assert exc.value.code == "unsupported"
    assert "get_guild" not in client.capabilities()["actions"]


async def test_native_read_propagates_qq_errors_and_preserves_onebot_collisions(client):
    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            raise V2Error("qq_api_error", "fixture", business_code=11253, phase="rejected")
    client._state.http = HTTP()
    with pytest.raises(V2Error) as exc:
        await client.qq.get_guild("g")
    assert exc.value.business_code == 11253
    async def onebot(action, **params):
        assert action == "get_group_info" and params == {"group_id": "g"}
        return {"group_id": "g", "group_name": "OneBot"}
    client.call_action = onebot
    assert (await client.api.get_group_info(group_id="g"))["group_name"] == "OneBot"
    assert inspect.signature(client.qq.me_guilds).parameters["limit"].default == 100
    assert inspect.signature(client.qq.get_guild_members).parameters["after"].default == "0"

@pytest.mark.parametrize("status,code,phase", [
    (429, "qq_rate_limited", "rejected"),
    (502, "qq_api_error", "result_unknown"),
    (403, "qq_api_error", "rejected"),
])
async def test_native_reads_propagate_denial_rate_and_server_failure(client, status, code, phase):
    calls = []
    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            calls.append((spec.method, spec.url))
            raise V2Error(code, "fixture", status=status, phase=phase, http_status=status)
    client._state.http = HTTP()
    with pytest.raises(V2Error) as exc:
        await client.qq.get_guild("g")
    assert (exc.value.code, exc.value.phase) == (code, phase)
    assert calls == [("GET", "https://api.bot.qq.com/guilds/g")]


async def test_native_read_cancellation_does_not_return_cached_state(client):
    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            raise asyncio.CancelledError
    client._state.http = HTTP()
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


@pytest.mark.parametrize("malformed", [
    {"list": [{"strategy_id": "wrong-field"}], "next_cursor": ""},
    {"next_cursor": ""},
    {"strategies": None, "next_cursor": ""},
    {"strategies": []},
])
async def test_strategy_page_missing_official_fields_fails_explicitly(client, malformed):
    async def request(spec, *, before_send=None):
        if before_send is not None:
            before_send()
        assert spec.method == "GET" and spec.url.startswith("https://api.bot.qq.com/v2/groups/join_approval_strategy")
        return SimpleNamespace(data=malformed)
    client._state.http = SimpleNamespace(request=request)
    with pytest.raises(V2Error) as direct:
        await client.qq.get_join_approval_strategies()
    assert direct.value.code == "pagination_incomplete"
    with pytest.raises(V2Error) as iterated:
        [row async for row in client.qq.iter_join_approval_strategies()]
    assert iterated.value.code == "pagination_incomplete"


async def test_cursor_pagination_detects_repetition_without_partial_list(client):
    pages = [{"users": [{"member_openid": "u1"}], "next_cursor": "repeat"},
             {"users": [{"member_openid": "u2"}], "next_cursor": "repeat"}]
    class HTTP:
        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            assert spec.method == "GET" and spec.url.startswith("https://api.bot.qq.com/v2/groups/g/member_blacklist")
            return SimpleNamespace(data=pages.pop(0))
    client._state.http = HTTP()
    seen = []
    with pytest.raises(V2Error) as exc:
        async for row in client.qq.iter_group_member_blacklist("g"):
            seen.append(row["member_openid"])
    assert seen == ["u1", "u2"] and exc.value.code == "pagination_incomplete"


async def test_role_and_guild_member_iterators_keep_string_cursors(client):
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


async def test_threads_do_not_claim_complete_when_qq_does_not_supply_cursor(client):
    async def request(spec, *, before_send=None):
        if before_send is not None:
            before_send()
        return SimpleNamespace(data={"threads": [], "is_finish": 0})
    client._state.http = SimpleNamespace(request=request)
    assert (await client.qq.get_threads("c"))["is_finish"] == 0
    with pytest.raises(V2Error, match="continuation"):
        await client.qq.complete_threads("c")
