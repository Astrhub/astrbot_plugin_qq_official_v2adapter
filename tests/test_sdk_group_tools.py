"""LLM group tools recheck host/session/role, freeze writes and return scoped summaries."""

import json
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from astrbot.core.platform.platform_metadata import PlatformMetadata
from test_messaging_state import chat_payload

from v2 import PLATFORM_TYPE
from v2.errors import V2Error
from v2.event import V2MessageEvent
from v2.extensions.management import Management
from v2.group_tools import TOOL_NAMES, GroupTools
from v2.messaging.convert import convert_chat
from v2.profiles.service import Profiles
from v2.profiles.store import ProfileStore
from v2.protocol import RawEnvelope, RequestSpec
from v2.sdk.api.groups import GroupReads

pytest_plugins = ("test_lifecycle", "test_sdk_native_writes")


@pytest.fixture
async def tool_env(native, tmp_path):
    m = native
    roles = ["member"]
    enabled, session = [True], [True]
    mode = ["ok"]
    requests = []
    async def request(spec, *, before_send=None):
        if before_send:
            before_send()
        parsed = urlsplit(spec.url)
        requests.append((spec.method, parsed.scheme, parsed.hostname, parsed.path, parsed.query, spec.json_body))
        if mode[0] == "unknown" and spec.method == "POST":
            raise V2Error("qq_api_error", "fixture 5xx", phase="result_unknown", http_status=500, status=502)
        if parsed.path == "/v2/groups/g/members/actor":
            data = {"member_openid": "actor", "username": "admin", "member_role": roles[0], "bot": False}
        elif parsed.path == "/v2/groups/g/members/other":
            data = {"member_openid": "other", "username": "other admin", "member_role": "admin", "bot": False}
        elif parsed.path == "/v2/groups/g/members/u":
            data = {"member_openid": "u", "username": "One", "member_role": "member", "bot": False}
        elif parsed.path == "/v2/groups/g/bot_state":
            data = {"member_openid": "bot", "member_role": "admin"}
        elif parsed.path == "/v2/groups/g/info":
            data = {"group_openid": "g", "group_name": "Group", "group_member_num": 2}
        elif parsed.path == "/v2/groups/g/members":
            data = {"members": [{"member_openid": "u", "username": "One", "member_role": "member", "bot": False}], "next_cursor": ""}
        elif parsed.path == "/v2/groups/g/restrict_chat_setting":
            data = {"global_rule": {"mode": "none"}, "members": [{"member_openid": "u", "username": "One", "mute_expire_at": "later", "union_openid": "private"}]} if spec.method == "GET" else {}
        elif parsed.path == "/v2/groups/g/member_blacklist":
            data = {"users": [{"member_openid": "u", "username": "One", "union_openid": "private"}], "next_cursor": ""} if spec.method == "GET" else {"fail_openids": []}
        elif parsed.path == "/v2/groups/g/batch_remove_members":
            data = {"remove_members_result": "success", "add_to_member_blacklist_fail_openids": ["u"] if mode[0] == "partial" else []}
        elif parsed.path == "/v2/groups/g/join_request_list":
            data = {"list": [{"member_openid": "u", "username": "One", "join_request_id": "real-request",
                              "apply_at": "1970-01-01T02:45:00+00:00", "apply_source": "self_apply",
                              "union_openid": "private", "verify_info": {"answer": "secret"}}], "next_cursor": ""}
        elif parsed.path == "/v2/groups/g/approval_join_request/u":
            data = {}
        elif parsed.path == "/v2/groups/join_approval_strategy":
            data = {"list": [{"strategy_id": "strategy", "remark": "mine", "is_enable": "on", "group_openids": ["other-secret-group"]}], "next_cursor": ""}
        else:
            raise AssertionError(parsed.path)
        return SimpleNamespace(data=data, status=200, trace_id="fixture")
    m.http.request = request
    manager = Management(m.client.identity, m.http, m.extension, m.store,
                         settings=lambda: m.policy)
    m.client._state.management = manager
    m.client._state.reads = GroupReads(m.client.identity, m.http, RequestSpec)
    profiles = ProfileStore(tmp_path / "profiles.sqlite3", clock=lambda: m.clock[0])
    service = Profiles(m.client.identity, profiles, m.client._state.reads)
    m.client._state.profiles = service
    owner = SimpleNamespace(instances=set(), stopping=False, messages=m.store,
                            extension_state=m.extension, profiles=profiles)
    instance = SimpleNamespace(identity=m.client.identity, client=m.client, check_generation=m.client.check)
    owner.instances = [instance]
    async def allowed(umo, plugin):
        assert umo and plugin == "astrbot_plugin_qq_official_v2adapter"
        return session[0]
    tools = GroupTools(owner, active=lambda name: enabled[0] and name.startswith("qq_v2_"), session_allowed=allowed)
    def event(sender="actor", target="g", received=None):
        at = m.clock[0] if received is None else received
        payload = chat_payload("GROUP_MESSAGE_CREATE", sender=sender, target=target, timestamp=at)
        chat = convert_chat(m.client.identity, RawEnvelope(payload, at))
        meta = PlatformMetadata(PLATFORM_TYPE, "fixture", m.client.identity.platform_id)
        return V2MessageEvent(chat.message, meta, m.client, chat.route)
    try:
        yield SimpleNamespace(m=m, roles=roles, enabled=enabled, session=session, mode=mode,
                              requests=requests, owner=owner, tools=tools, event=event, profiles=profiles)
    finally:
        await manager.close()
        await service.close()
        profiles.close()


def test_all_thirteen_tools_have_explicit_decorated_handlers_and_no_native_catalog_dump(plugin_module):
    from astrbot.core.provider.register import llm_tools
    names = {f.name for f in llm_tools.func_list if f.name.startswith("qq_v2_")}
    assert names == {"qq_v2_" + name for name in TOOL_NAMES} and len(TOOL_NAMES) == 13
    handlers = {f.name: f for f in llm_tools.func_list if f.name.startswith("qq_v2_")}
    assert all(f.handler and f.parameters is not None for f in handlers.values())
    assert plugin_module.QQOfficialV2.tool_get_group.__name__ == "tool_get_group"


async def test_bound_scope_session_and_member_role_are_rechecked_before_any_management_request(tool_env):
    t = tool_env
    event = t.event()
    assert (await t.tools.call(event, "get_group"))["name"] == "Group"
    t.enabled[0] = False
    with pytest.raises(V2Error) as disabled:
        await t.tools.call(event, "get_group")
    assert disabled.value.code == "tool_disabled"
    t.enabled[0] = True
    t.session[0] = False
    with pytest.raises(V2Error) as off:
        await t.tools.call(event, "get_group")
    assert off.value.code == "tool_disabled"
    t.session[0] = True
    with pytest.raises(V2Error) as no_role:
        await t.tools.call(event, "list_mutes")
    assert no_role.value.code == "group_admin_required"
    assert not any(method == "POST" for method, *_ in t.requests)
    t.roles[0] = "owner"
    mutes = await t.tools.call(event, "list_mutes")
    assert mutes["members"] == [{"member_openid": "u", "username": "One", "mute_expire_at": "later"}]
    assert "private" not in json.dumps(mutes)
    with pytest.raises(V2Error) as robot_admin:
        await t.tools.call(event, "list_join_strategies")
    assert robot_admin.value.code == "host_admin_required"
    event.role = "admin"
    strategies = await t.tools.call(event, "list_join_strategies")
    assert strategies["strategies"] == [{"strategy_id": "strategy", "remark": "mine", "is_enable": "on"}]
    assert "other-secret-group" not in json.dumps(strategies)
    private = t.event()
    private.message_obj.group_id = "different"
    with pytest.raises(V2Error) as wrong:
        await t.tools.call(private, "get_group")
    assert wrong.value.code == "tool_scope_unavailable"


async def test_tool_caches_are_group_scoped_pages_not_complete_or_identity_guessing(tool_env):
    t = tool_env
    event = t.event()
    t.profiles.merge(t.m.client.identity.robot, "group", "g", "u", {"nickname": "Repeated"}, source="chat_history", as_of=9999)
    t.profiles.merge(t.m.client.identity.robot, "group", "g", "v", {"nickname": "Repeated"}, source="chat_history", as_of=9998)
    t.profiles.merge(t.m.client.identity.robot, "group", "other", "x", {"nickname": "Repeated"}, source="chat_history", as_of=9998)
    found = await t.tools.call(event, "find_known_members", query="Repeated")
    assert {item["member_openid"] for item in found["candidates"]} == {"u", "v"} and not t.requests
    cached = await t.tools.call(event, "get_member", member_openid="u")
    assert cached["nickname"] == "Repeated" and cached["_qq"]["source"] == "chat_history"
    assert not any(row[3] == "/v2/groups/g/members/u" for row in t.requests)
    refreshed = await t.tools.call(event, "get_member", member_openid="u", refresh=True)
    assert refreshed["nickname"] == "One" and refreshed["_qq"]["source"] == "official_query"
    assert [row[0:4] for row in t.requests if row[3] == "/v2/groups/g/members/u"] == [
        ("GET", "https", "api.bot.qq.com", "/v2/groups/g/members/u")]
    page = await t.tools.call(event, "list_members")
    assert page["non_atomic"] is True and page["pagination_done"] is True
    assert page["members"][0]["_qq"]["current"] is False
    t.roles[0] = "admin"
    blacklist = await t.tools.call(event, "list_blacklist")
    assert "union_openid" not in json.dumps(blacklist)
    application = await t.tools.call(event, "list_join_requests")
    assert application["list"][0]["flag"] and "secret" not in json.dumps(application)


async def test_write_frozen_relative_expiry_partial_and_unknown_replay_fences(tool_env):
    t = tool_env
    t.roles[0] = "admin"
    event = t.event()
    first = await t.tools.call(event, "mute_members", member_openids=["u"], duration_seconds=60)
    assert first["state"] == "succeeded" and first["operation_id"].startswith("tool-")
    wire = [row for row in t.requests if row[0] == "POST"]
    assert len(wire) == 1 and wire[0][-1]["members"][0]["mute_expire_at"] == "1970-01-01T02:47:40+00:00"
    t.m.clock[0] += 1
    second = await t.tools.call(event, "mute_members", member_openids=["u"], duration_seconds=60)
    assert second == first and len([row for row in t.requests if row[0] == "POST"]) == 1
    status = await t.tools.call(event, "get_operation_status", operation_id=first["operation_id"])
    assert status == first
    stranger = t.event(sender="other")
    with pytest.raises(V2Error) as alien:
        await t.tools.call(stranger, "get_operation_status", operation_id=first["operation_id"])
    assert alien.value.code in {"operator_verification_unavailable", "operation_not_found"}
    t.mode[0] = "partial"
    partial = await t.tools.call(event, "kick_members", member_openids=["u"], blacklist=True)
    assert partial["state"] == "partial" and partial["removed"] == ["u"]
    again = await t.tools.call(event, "kick_members", member_openids=["u"], blacklist=True)
    assert again == partial and len([r for r in t.requests if r[3].endswith("batch_remove_members")]) == 1
    t.mode[0] = "unknown"
    unknown = await t.tools.call(event, "change_blacklist", op="add", member_openids=["u"])
    assert unknown["state"] == "unknown" and unknown["error"]["code"] == "qq_api_error"
    before = len([row for row in t.requests if row[0] == "POST"])
    assert await t.tools.call(event, "change_blacklist", op="add", member_openids=["u"]) == unknown
    assert len([row for row in t.requests if row[0] == "POST"]) == before
    assert await t.tools.call(event, "get_operation_status", operation_id=unknown["operation_id"]) == unknown


async def test_application_flag_scope_and_one_shot_ack(tool_env):
    t = tool_env
    t.roles[0] = "admin"
    event = t.event()
    listed = await t.tools.call(event, "list_join_requests")
    flag = listed["list"][0]["flag"]
    approved = await t.tools.call(event, "approve_join_request", flag=flag, approve=True)
    assert approved["state"] == "succeeded"
    assert await t.tools.call(event, "approve_join_request", flag=flag, approve=True) == approved
    assert len([r for r in t.requests if r[3].endswith("approval_join_request/u")]) == 1
    with pytest.raises(V2Error) as conflict:
        await t.tools.call(event, "approve_join_request", flag=flag, approve=False)
    assert conflict.value.code == "operation_conflict"
