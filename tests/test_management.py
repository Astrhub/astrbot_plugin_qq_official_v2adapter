from types import SimpleNamespace

import pytest
from aiohttp import web
from test_messaging_state import NOW
from test_transport_http import MappedSession, upstream

from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.store import MessageStore, robot_key
from v2.models import InstanceKey
from v2.transport.http import HTTPTransport


@pytest.fixture
async def management(config, tmp_path):
    from v2.extensions.management import Management
    identity = InstanceKey.from_config(config)
    clock, calls, modes = [NOW], [], []
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    state = ExtensionStore(store)
    policy = {"management_writes": True}
    async def handle(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "management-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot management-fixture"
        body = await request.json() if request.can_read_body else None
        calls.append((request.method, request.path, dict(request.query), body))
        if modes and modes[0] == "denied":
            return web.json_response({"code": 11253})
        if request.path.endswith("bot_state"):
            return web.json_response({"member_openid": "real-bot", "member_role": "admin", "allow_proactive_msg": False})
        if request.path.endswith("restrict_chat_setting"):
            return web.json_response({"members": [{"member_openid": "u", "mute_expire_at": "2027-01-15T09:00:00+08:00"}], "global_rule": {"mode": "none"}} if request.method == "GET" else {})
        if request.path.endswith("batch_remove_members"):
            return web.json_response({"remove_members_result": "success", "add_to_member_blacklist_fail_openids": ["u"] if modes and modes[0] == "partial" else []})
        if "/approval_join_request/" in request.path:
            return web.json_response({})
        if request.path.endswith("join_request_list"):
            return web.json_response({"list": [{"member_openid": "u", "join_request_id": "actual-request", "apply_at": "2027-01-15T08:00:00+08:00", "apply_source": "self_apply"}], "next_cursor": ""})
        if request.path.endswith("members") and request.path.startswith("/v2"):
            cursor = request.query.get("cursor", "")
            return web.json_response({"members": [{"member_openid": "u" if not cursor else "v", "username": "same name", "member_role": "member", "bot": False}],
                                      "next_cursor": "repeat" if modes and modes[0] == "cycle" else "opaque &+" if not cursor else ""})
        if "/members/" in request.path:
            return web.json_response({"member_openid": "u", "username": "actual name", "member_role": "member", "bot": False})
        if request.path == "/v2/generate_url_link":
            return web.json_response({"data": {"url": "https://qun.qq.com/qunpro/robot/qunshare?robot_appid=real"}})
        if request.path == "/users/@me":
            return web.json_response({"id": "actual-bot-id", "username": "actual-bot-name", "bot": True})
        if request.path.endswith("/info"):
            return web.json_response({"group_openid": "g", "group_name": "actual-group", "group_member_num": 2})
        raise AssertionError(request.path)
    async with upstream(handle) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        service = Management(identity, http, state, store, settings=lambda: policy)
        try:
            yield SimpleNamespace(service=service, policy=policy, calls=calls, modes=modes, clock=clock, store=store, state=state, http=http)
        finally:
            await service.close()
            await state.close()
            await http.close()
            store.close()


async def test_complete_members_query_cursor_and_no_chat_identity_pollution(management):
    m = management
    rows = await m.service.group_members("g")
    assert [r["member_openid"] for r in rows] == ["u", "v"]
    assert m.calls[1][2] == {"cursor": "opaque &+"}
    assert m.store.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 0
    assert m.http.session.calls[-1][1] == "https://api.bot.qq.com/v2/groups/g/members?cursor=opaque+%26%2B"
    m.modes[:] = ["cycle"]
    with pytest.raises(V2Error) as error:
        await m.service.group_members("g")
    assert error.value.code == "pagination_incomplete"


async def test_legacy_management_rate_rows_do_not_delay_server_requests(management):
    m = management
    robot = robot_key(m.http.identity.robot)
    with m.store.transaction():
        m.store.db.executemany("INSERT INTO extension_rates VALUES(?,?,?)",
            [(robot, "group_info", m.clock[0])] * 30 + [(robot, "group_ban", m.clock[0])] * 60)
    started = m.clock[0]
    await m.service.group_info("g")
    await m.service.group_ban("g", "u", 60, operation_id="unthrottled-ban")
    assert m.clock[0] == started
    assert any(method == "POST" and path.endswith("restrict_chat_setting") for method, path, *_ in m.calls)


async def test_ban_update_unban_and_write_gate(management):
    m = management
    await m.service.group_ban("g", "u", 60, operation_id="ban")
    assert m.calls[-1][3]["members"] == [{"op": "update", "member_openid": "u", "mute_expire_at": "2027-01-15T08:01:00+00:00"}]
    await m.service.group_ban("g", "u", 0, operation_id="unban")
    assert m.calls[-1][3]["members"] == [{"op": "del", "member_openid": "u", "mute_expire_at": ""}]
    before = len(m.calls)
    for duration in (-1, True, 2592001):
        with pytest.raises(V2Error):
            await m.service.group_ban("g", "u", duration)
    m.policy["management_writes"] = False
    with pytest.raises(V2Error):
        await m.service.group_kick("g", ["u"])
    assert len(m.calls) == before


async def test_partial_kick_is_not_success_or_replayed(management):
    m = management
    m.modes[:] = ["partial"]
    with pytest.raises(V2Error) as error:
        await m.service.group_kick("g", ["u"], blacklist=True, operation_id="kick")
    assert error.value.code == "partial_failure" and error.value.phase == "partial"
    assert m.state.operation(m.http.identity.robot, "kick")["state"] == "partial"
    before = sum(method == "POST" for method, *_ in m.calls)
    with pytest.raises(V2Error):
        await m.service.group_kick("g", ["u"], blacklist=True, operation_id="kick")
    assert sum(method == "POST" for method, *_ in m.calls) == before


async def test_real_request_flags_expire_and_cannot_be_reused(management):
    m = management
    request = (await m.service.join_requests("g"))[0]
    flag = request["flag"]
    await m.service.approve(flag, approve=True)
    assert m.calls[-1][1] == "/v2/groups/g/approval_join_request/u"
    assert m.calls[-1][3] == {"op": "approve", "join_request_id": "actual-request"}
    with pytest.raises(V2Error):
        await m.service.approve(flag, approve=False)
    assert not (await m.service.join_requests("g"))[0].get("flag")
    with pytest.raises(V2Error):
        await m.service.approve("fabricated-friend-add", approve=True)


async def test_actual_profile_and_share_results_and_permission_error(management):
    m = management
    assert (await m.service.login_info())["user_id"] == "actual-bot-id"
    assert (await m.service.share("callback"))["url"].startswith("https://qun.qq.com/")
    with pytest.raises(V2Error):
        await m.service.share("x" * 33)
    m.modes[:] = ["denied"]
    with pytest.raises(V2Error) as error:
        await m.service.group_info("g")
    assert error.value.business_code == 11253



async def test_onebot_entries_application_expiry_guild_crud_and_unknown_recovery(management):
    from aiohttp import web

    from v2.client import V2Client
    from v2.extensions.management import Management
    from v2.extensions.state import ExtensionStore
    from v2.messaging.store import MessageStore
    from v2.models import InstanceKey
    from v2.transport.http import HTTPTransport

    m = management
    client = V2Client(m.http.identity)
    client._state.management, client._state.http = m.service, m.http
    results = [await client.get_group_info(group_id="g"), await client.api.get_group_info(group_id="g"),
               await client.call_action("get_group_info", group_id="g", no_cache=True)]
    assert all(r == results[0] for r in results) and results[0]["group_name"] == "actual-group"
    members = await client.api.get_group_member_list(group_id="g")
    assert [v["user_id"] for v in members] == ["u", "v"]
    with pytest.raises(V2Error):
        await client.set_group_whole_ban(group_id="g", enable=True)

    row = (await m.service.join_requests("g"))[0]
    original_identity = m.service.identity
    m.service.identity = InstanceKey.from_config({"id": "other", "appid": "other", "environment": "production", "transport": "websocket", "intents": 0, "shard": [0, 1]})
    with pytest.raises(V2Error):
        await m.service.approve(row["flag"], approve=True)
    m.service.identity = original_identity
    m.clock[0] += 301
    with pytest.raises(V2Error):
        await m.service.approve(row["flag"], approve=True)
    renewed = (await m.service.join_requests("g"))[0]
    invited = {**renewed, "join_request_id": "invited-request", "apply_source": "invited"}
    invited_flag = m.service.observe_request("g", invited, fresh_read=True)
    await m.service.approve(invited_flag, approve=False, reason="fixture declined")
    assert m.calls[-1][3]["join_request_id"] == "invited-request"

    calls = []
    fail_500 = [False]

    async def handle(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "guild-fixture", "expires_in": 7200})
        body = await request.json() if request.can_read_body else None
        calls.append((request.method, request.path, dict(request.query), body))
        if fail_500[0]:
            return web.json_response({"code": 50000}, status=500)
        if request.path == "/guilds/g/members" and request.method == "GET":
            after = request.query["after"]
            return web.json_response([] if after == "second" else [{"user": {"id": "first" if after == "0" else "second"}}])
        if request.path == "/guilds/g/members/u" and request.method == "GET":
            return web.json_response({"user": {"id": "u", "username": "actual"}, "roles": ["1"]})
        if request.path == "/guilds/g/channels" and request.method == "GET":
            return web.json_response([{"id": "c", "guild_id": "g", "name": "actual-channel"}])
        if request.path in {"/channels/c", "/guilds/g/channels"} and request.method in {"GET", "POST", "PATCH"}:
            return web.json_response({"id": "c", "guild_id": "g", "name": (body or {}).get("name", "actual-channel")})
        if request.path == "/guilds/g/mute":
            return web.json_response({"user_ids": ["u"]})
        if request.path == "/guilds/g" and request.method == "GET":
            return web.json_response({"id": "g", "name": "actual-guild"})
        return web.Response(status=204)

    if m.http.session is not None:
        await m.http.session.close()
        m.http.session = None
    m.http._token = None
    async with upstream(handle) as base:
        m.http._factory = lambda: MappedSession(base)
        assert (await m.service.guild_info("g"))["id"] == "g"
        assert [r["user"]["id"] for r in await m.service.guild_members("g")] == ["first", "second"]
        assert (await m.service.guild_member("g", "u"))["user"]["id"] == "u"
        assert (await m.service.channels("g"))[0]["id"] == "c"
        assert (await m.service.channel_info("c"))["id"] == "c"
        await m.service.channel_create("g", {"type": 0, "name": "new", "parent_id": "0"})
        await m.service.channel_update("c", {"name": "updated"})
        await m.service.channel_delete("c")
        await m.service.guild_kick("g", "u", delete_history_days=3)
        await m.service.guild_mute("g", "u", 60)
        with pytest.raises(V2Error) as error:
            await m.service.guild_mute_batch("g", ["u", "v"], 60, operation_id="partial-batch")
        assert error.value.phase == "partial"

        calls.clear()
        fail_500[0] = True
        with pytest.raises(V2Error) as u_err:
            await m.service.channel_delete("c", operation_id="uncertain")
        assert u_err.value.phase == "result_unknown"
        path = m.store.db.execute("PRAGMA database_list").fetchone()[2]
        identity = m.http.identity
        await m.service.close()
        await m.state.close()
        await m.http.close()
        m.store.close()
        recovered = MessageStore(path, clock=lambda: m.clock[0])
        state = ExtensionStore(recovered)
        http = HTTPTransport(identity, "fixture-secret", session_factory=lambda: MappedSession(base))
        service = Management(identity, http, state, recovered, settings=lambda: m.policy)
        try:
            with pytest.raises(V2Error):
                await service.channel_delete("c", operation_id="uncertain")
            assert len(calls) == 1
        finally:
            await service.close()
            await state.close()
            await http.close()
            recovered.close()
