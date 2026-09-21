
import pytest
from aiohttp import web
from test_management import management as management
from test_transport_http import MappedSession, upstream

from v2.client import V2Client
from v2.errors import V2Error
from v2.models import InstanceKey


async def test_onebot_three_entries_and_native_use_real_management_contract(management):
    m = management
    client = V2Client(m.http.identity)
    client._state.management, client._state.http = m.service, m.http
    results = [await client.get_group_info(group_id="g"), await client.api.get_group_info(group_id="g"),
               await client.call_action("get_group_info", group_id="g", no_cache=True)]
    assert all(r == results[0] for r in results)
    assert results[0]["group_name"] == "actual-group" and "max_member_count" not in results[0]
    members = await client.api.get_group_member_list(group_id="g")
    assert [v["user_id"] for v in members] == ["u", "v"] and all(v["id_kind"] == "member_openid" for v in members)
    assert (await client.qq.group_info("g"))["group_openid"] == "g"
    assert client.capabilities()["actions"]["set_group_ban"]["permission"] == "unknown"
    before = len(m.calls)
    with pytest.raises(V2Error):
        await client.set_group_whole_ban(group_id="g", enable=True)
    with pytest.raises(V2Error):
        await client.get_msg(message_id="unrecorded")
    with pytest.raises(V2Error):
        await client.delete_msg(message_id="REFIDX_only-a-reference")
    assert len(m.calls) == before
    assert m.store.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 0


async def test_application_expiry_cross_bot_and_auto_approval(management):
    m = management
    row = (await m.service.join_requests("g"))[0]
    original_identity = m.service.identity
    m.service.identity = InstanceKey.from_config({"id": "other", "appid": "other", "environment": "production", "transport": "websocket", "intents": 0, "shard": [0, 1]})
    with pytest.raises(V2Error):
        await m.service.approve(row["flag"], approve=True)
    m.service.identity = original_identity
    m.clock[0] += 301
    before = len(m.calls)
    with pytest.raises(V2Error):
        await m.service.approve(row["flag"], approve=True)
    assert len(m.calls) == before
    renewed = (await m.service.join_requests("g"))[0]
    assert renewed["flag"] != row["flag"]
    m.service.observe_request("g", {**renewed, "auto_approved": {"strategy_id": "actual-auto-policy"}})
    with pytest.raises(V2Error):
        await m.service.approve(renewed["flag"], approve=True)
    invited = {**renewed, "join_request_id": "invited-request", "apply_source": "invited"}
    invited_flag = m.service.observe_request("g", invited, fresh_read=True)
    assert invited_flag
    await m.service.approve(invited_flag, approve=False, reason="fixture declined")
    assert m.calls[-1][3]["join_request_id"] == "invited-request"


async def test_guild_pages_crud_mute_and_precise_query_contract(management):
    m = management
    calls = []
    async def handle(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "guild-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot guild-fixture"
        body = await request.json() if request.can_read_body else None
        calls.append((request.method, request.path, dict(request.query), body))
        if request.path == "/guilds/g/members" and request.method == "GET":
            after = request.query["after"]
            assert request.query["limit"] == "400"
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
    async with upstream(handle) as base:
        m.http._factory = lambda: MappedSession(base)
        assert (await m.service.guild_info("g"))["id"] == "g"
        assert [r["user"]["id"] for r in await m.service.guild_members("g")] == ["first", "second"]
        assert [r[2]["after"] for r in calls if r[1] == "/guilds/g/members"] == ["0", "first", "second"]
        assert (await m.service.guild_member("g", "u"))["user"]["id"] == "u"
        assert (await m.service.channels("g"))[0]["id"] == "c"
        assert (await m.service.channel_info("c"))["id"] == "c"
        await m.service.channel_create("g", {"type": 0, "name": "new", "parent_id": "0"})
        await m.service.channel_update("c", {"name": "updated"})
        await m.service.channel_delete("c")
        await m.service.guild_kick("g", "u", delete_history_days=3)
        assert calls[-1] == ("DELETE", "/guilds/g/members/u", {}, {"add_blacklist": False, "delete_history_msg_days": 3})
        await m.service.guild_mute("g", "u", 60)
        assert calls[-1][3] == {"mute_seconds": "60"}
        with pytest.raises(V2Error) as error:
            await m.service.guild_mute_batch("g", ["u", "v"], 60, operation_id="partial-batch")
        assert error.value.phase == "partial" and error.value.details == {"succeeded": ["u"]}
        assert all(url.startswith("https://api.bot.qq.com/") for _, url, _ in m.http.session.calls)


async def test_management_unknown_mutation_survives_restart_without_replay(management):
    m = management
    calls = []
    async def handle(request):
        if request.path == "/app/getAppAccessToken":
            return web.json_response({"access_token": "unknown-fixture", "expires_in": 7200})
        calls.append((request.method, request.path))
        return web.json_response({"code": 50000}, status=500)
    async with upstream(handle) as base:
        m.http._factory = lambda: MappedSession(base)
        with pytest.raises(V2Error) as error:
            await m.service.channel_delete("c", operation_id="uncertain")
        assert error.value.phase == "result_unknown"
        assert m.state.operation(m.http.identity.robot, "uncertain")["state"] == "unknown"
        from v2.extensions.state import ExtensionStore
        from v2.extensions.management import Management
        from v2.messaging.store import MessageStore
        from v2.transport.http import HTTPTransport
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
            assert calls == [("DELETE", "/channels/c")] and http.session is None
            assert state.operation(identity.robot, "uncertain")["context"] == {"method": "DELETE", "path": "/channels/c", "request": None, "params": None}
        finally:
            await service.close()
            await state.close()
            await http.close()
            recovered.close()
