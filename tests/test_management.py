from types import SimpleNamespace

import pytest
from aiohttp import web
from test_messaging_state import NOW
from test_transport_http import MappedSession, upstream

from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.store import MessageStore
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
    async def sleep(seconds):
        clock[0] += seconds
    async with upstream(handle) as base:
        http = HTTPTransport(identity, config["secret"], session_factory=lambda: MappedSession(base))
        service = Management(identity, http, state, store, settings=lambda: policy, sleep=sleep)
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
