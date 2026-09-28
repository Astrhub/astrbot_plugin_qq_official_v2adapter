"""Literal QQ M6 request, result, permission and partial-outcome fixtures."""

import asyncio
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from v2.errors import V2Error

pytest_plugins = ("test_sdk_native_writes",)


PANEL = {"items": [], "remark": ""}
SCHEDULE = {"schedule": {"name": "x", "start_timestamp": "1700000000000", "end_timestamp": "1700003600000",
                         "jump_channel_id": "0", "remind_type": "0"}}

# Independent literal requests, not generated from the production catalog.
WRITES = [
    ("A005", "POST", "/v2/generate_url_link", lambda q: q.generate_url_link(""), {"callback_data": ""}, {"data": {"url": "https://qun.qq.com/share?token=private"}}),
    ("A029", "POST", "/v2/groups/g%2F1/restrict_chat_setting", lambda q: q.set_group_restrict_chat_setting("g/1", [{"op": "del", "member_openid": "u", "mute_expire_at": ""}]), {"members": [{"op": "del", "member_openid": "u", "mute_expire_at": ""}]}, {}),
    ("A030", "POST", "/v2/groups/g%2F1/batch_remove_members", lambda q: q.batch_remove_group_members("g/1", ["u"], True), {"member_openids": ["u"], "add_to_member_blacklist": True}, {"remove_members_result": "success", "add_to_member_blacklist_fail_openids": []}),
    ("A032", "POST", "/v2/groups/g%2F1/member_blacklist", lambda q: q.set_group_member_blacklist("g/1", "add", ["u"]), {"op": "add", "member_openids": ["u"]}, {"fail_openids": []}),
    ("A034", "POST", "/v2/groups/g%2F1/approval_join_request/u", lambda q: q.approve_group_join_request("g/1", "u", "decline", "request", "", False), {"op": "decline", "join_request_id": "request", "reject_reason": "", "add_to_member_blacklist": False}, {}),
    ("A036", "POST", "/v2/groups/join_approval_strategy", lambda q: q.create_join_approval_strategy(group_openids=["g/1"], is_enable="off", remark=""), {"group_openids": ["g/1"], "is_enable": "off", "remark": ""}, {"strategy_id": "strat", "extra": 0}),
    ("A037", "PATCH", "/v2/groups/join_approval_strategy/st%2F1", lambda q: q.update_join_approval_strategy("st/1", remark="", group_action={"op": "del", "group_openids": ["g/1"]}), {"remark": "", "group_action": {"op": "del", "group_openids": ["g/1"]}}, {}),
    ("A038", "DELETE", "/v2/groups/join_approval_strategy/st%2F1", lambda q: q.delete_join_approval_strategy("st/1"), None, None),
    ("A039", "POST", "/v2/groups/join_approval_strategy/st%2F1/execute", lambda q: q.execute_join_approval_strategy("st/1"), {}, {}),
    ("A040", "POST", "/v2/groups/join_approval_strategy/st%2F1/whitelist_users", lambda q: q.set_join_approval_whitelist("st/1", "add", ["000123"]), {"op": "add", "whitelist_users": ["000123"]}, {"strategy_id": "st/1", "whitelist_user_count": 1}),
    ("A042", "PUT", "/v2/menu", lambda q: q.put_menu({"items": []}), {"menu": {"items": []}}, {"version": 0, "extra": False}),
    ("A044", "POST", "/v2/panels", lambda q: q.create_panel("group", PANEL), {"scope": "group", "target_type": "all", "panel": PANEL}, {"panel_id": "p-1"}),
    ("A046", "PUT", "/v2/panels/p%2F1", lambda q: q.update_panel("p/1", PANEL), {"panel": PANEL}, {"version": 0}),
    ("A047", "DELETE", "/v2/panels/p%2F1", lambda q: q.delete_panel("p/1"), None, {}),
    ("A048", "PUT", "/v2/panels/p%2F1/target", lambda q: q.set_panel_target("p/1", "del", group_openids=["g/1"]), {"op": "del", "group_openids": ["g/1"]}, {}),
    ("A052", "POST", "/guilds/g%2F1/channels", lambda q: q.create_channel("g/1", "thread", 10007, 0, position=0), {"name": "thread", "type": 10007, "sub_type": 0, "position": 0}, {"id": "c-1", "extra": 0}),
    ("A054", "PATCH", "/channels/c%2F1", lambda q: q.update_channel("c/1", name="", position=0), {"name": "", "position": 0}, {"id": "c/1", "name": "", "position": 0}),
    ("A055", "DELETE", "/channels/c%2F1", lambda q: q.delete_channel("c/1"), None, {"id": "c/1"}),
    ("A058", "DELETE", "/guilds/g%2F1/members/u", lambda q: q.get_delete_member("g/1", "u", False, -1), {"add_blacklist": False, "delete_history_msg_days": -1}, None),
    ("A062-all", "PATCH", "/guilds/g%2F1/mute", lambda q: q.mute_all("g/1", mute_seconds="0"), {"mute_seconds": "0"}, {}),
    ("A062-cancel", "PATCH", "/guilds/g%2F1/mute", lambda q: q.cancel_mute_all("g/1"), {"mute_end_timestamp": "0", "mute_seconds": "0"}, {}),
    ("A062-multi", "PATCH", "/guilds/g%2F1/mute", lambda q: q.mute_multi_member("g/1", ["u"], mute_seconds="10"), {"user_ids": ["u"], "mute_seconds": "10"}, {"user_ids": ["u"]}),
    ("A062-multi-cancel", "PATCH", "/guilds/g%2F1/mute", lambda q: q.cancel_mute_multi_member("g/1", ["u"]), {"user_ids": ["u"], "mute_end_timestamp": "0", "mute_seconds": "0"}, {"user_ids": ["u"]}),
    ("A063", "PATCH", "/guilds/g%2F1/members/u/mute", lambda q: q.mute_member("g/1", "u", mute_end_timestamp="1700000000"), {"mute_end_timestamp": "1700000000"}, None),
    ("A066", "POST", "/guilds/g%2F1/roles", lambda q: q.create_guild_role("g/1", name="Reader", color=0, hoist=0), {"name": "Reader", "color": 0, "hoist": 0}, {"role_id": "r1", "role": {"name": "Reader"}}),
    ("A067", "PATCH", "/guilds/g%2F1/roles/r1", lambda q: q.update_guild_role("g/1", "r1", hoist=0), {"hoist": 0}, {"role_id": "r1"}),
    ("A068", "DELETE", "/guilds/g%2F1/roles/r1", lambda q: q.delete_guild_role("g/1", "r1"), None, None),
    ("A069", "PUT", "/guilds/g%2F1/members/u/roles/r1", lambda q: q.create_guild_role_member("g/1", "r1", "u"), {"channel": None}, None),
    ("A070", "DELETE", "/guilds/g%2F1/members/u/roles/r1", lambda q: q.delete_guild_role_member("g/1", "r1", "u", "c/1"), {"channel": {"id": "c/1"}}, None),
    ("A072", "PUT", "/channels/c%2F1/members/u/permissions", lambda q: q.update_channel_user_permissions("c/1", "u", 0, 4), {"add": "0", "remove": "4"}, None),
    ("A074", "PUT", "/channels/c%2F1/roles/r1/permissions", lambda q: q.update_channel_role_permissions("c/1", "r1", 1), {"add": "1"}, None),
    ("A076", "POST", "/guilds/g%2F1/api_permission/demand", lambda q: q.post_permission_demand("g/1", "c/1", {"path": "/guilds"}, "why"), {"channel_id": "c/1", "api_identify": {"path": "/guilds"}, "desc": "why"}, {"url": "https://qq.test/authorize"}),
    ("A077-message", "POST", "/guilds/g%2F1/announces", lambda q: q.create_announce("g/1", "c/1", "msg"), {"channel_id": "c/1", "message_id": "msg"}, {"id": "ann"}),
    ("A077-recommend", "POST", "/guilds/g%2F1/announces", lambda q: q.create_recommend_announce("g/1", 1, [{"channel_id": "c/1"}]), {"announces_type": 1, "recommend_channels": [{"channel_id": "c/1"}]}, {"id": "ann"}),
    ("A078", "DELETE", "/guilds/g%2F1/announces/all", lambda q: q.delete_announce("g/1"), None, None),
    ("A082", "PUT", "/channels/c%2F1/pins/msg", lambda q: q.put_pin("c/1", "msg"), None, {"message_ids": ["msg"]}),
    ("A083", "DELETE", "/channels/c%2F1/pins/msg", lambda q: q.delete_pin("c/1", "msg"), None, None),
    ("A084", "PUT", "/channels/c%2F1/messages/msg/reactions/1/55", lambda q: q.put_reaction("c/1", "msg", 1, "55"), None, None),
    ("A085", "DELETE", "/channels/c%2F1/messages/msg/reactions/1/55", lambda q: q.delete_reaction("c/1", "msg", 1, "55"), None, None),
    ("A089", "POST", "/channels/c%2F1/schedules", lambda q: q.create_schedule("c/1", "x", "1700000000000", "1700003600000", "0", 0), SCHEDULE, {"id": "schedule-1", "extra": True}),
    ("A090", "PATCH", "/channels/c%2F1/schedules/s1", lambda q: q.update_schedule("c/1", "s1", "x", "1700000000000", "1700003600000", "0", 0), SCHEDULE, {"id": "schedule-1", "extra": True}),
    ("A091", "DELETE", "/channels/c%2F1/schedules/s1", lambda q: q.delete_schedule("c/1", "s1"), None, None),
    ("A092", "POST", "/channels/c%2F1/audio", lambda q: q.update_audio("c/1", {"status": 0, "text": ""}), {"status": 0, "text": ""}, {}),
    ("A093", "PUT", "/channels/c%2F1/mic", lambda q: q.on_microphone("c/1"), {}, {}),
    ("A094", "DELETE", "/channels/c%2F1/mic", lambda q: q.off_microphone("c/1"), {}, {}),
    ("A097", "PUT", "/channels/c%2F1/threads", lambda q: q.post_thread("c/1", "title", "", 1), {"title": "title", "content": "", "format": 1}, {"task_id": "t1", "create_time": "1700000000"}),
    ("A098", "DELETE", "/channels/c%2F1/threads/t1", lambda q: q.delete_thread("c/1", "t1"), None, None),
]


@pytest.fixture
async def admin(native):
    m = native
    seen = []
    next_response = [None]
    async def request(spec, *, before_send=None):
        if before_send:
            before_send()
        seen.append(spec)
        return SimpleNamespace(data=next_response[0], status=200, trace_id="test-trace")
    m.http.request = request
    class Panels:
        async def manual_write(self, client, panel_id, write, *, mutation=None):
            assert client is m.client and (panel_id is None or isinstance(panel_id, str))
            return await write()
    m.client._state.panels = Panels()
    yield SimpleNamespace(client=m.client, store=m.store, calls=seen, response=next_response, policy=m.policy, extension=m.extension)


@pytest.mark.parametrize("identifier,method,path,call,body,response", WRITES, ids=lambda value: value if isinstance(value, str) and value.startswith("A0") else None)
async def test_named_m6_requests_exact_origin_body_and_raw_result(admin, identifier, method, path, call, body, response):
    m = admin
    m.response[0] = response
    actual = await call(m.client.qq)
    assert actual == response, identifier
    assert len(m.calls) == 1, identifier
    spec = m.calls[0]
    url = urlsplit(spec.url)
    assert (url.scheme, url.hostname, url.path, url.query, spec.method, spec.json_body, spec.params) == (
        "https", "api.bot.qq.com", path, "", method, body, None), identifier
    # Only a safe summary survives; raw result and the share URL must not be retained.
    record = m.extension.recent(m.client.identity.robot, 1)[0]
    assert "qun.qq.com" not in str(record)


async def test_m6_disabled_management_and_readonly_arbitrary_request_cannot_write(admin):
    m = admin
    m.policy["management_writes"] = False
    with pytest.raises(V2Error, match="fixture"):
        await m.client.qq.put_pin("c", "m", operation_id="no-write")
    assert not m.calls
    from v2.protocol import RequestSpec
    with pytest.raises(V2Error) as read_only:
        await m.client.qq.request(RequestSpec("production", "POST", "/v2/menu", json_body={}))
    assert read_only.value.code == "unsupported" and not m.calls


async def test_partial_group_removal_and_batch_mute_are_not_retried(admin):
    m = admin
    m.response[0] = {"remove_members_result": "success", "add_to_member_blacklist_fail_openids": ["u"]}
    with pytest.raises(V2Error) as partial:
        await m.client.qq.batch_remove_group_members("g", ["u"], True, operation_id="kick-once")
    assert partial.value.phase == "partial" and partial.value.details["removed"] == ["u"]
    assert m.extension.operation(m.client.identity.robot, "kick-once")["state"] == "partial"
    with pytest.raises(V2Error) as replay:
        await m.client.qq.batch_remove_group_members("g", ["u"], True, operation_id="kick-once")
    assert replay.value.code == "operation_already_attempted" and replay.value.phase == "partial"
    assert len(m.calls) == 1
    m.response[0] = {"user_ids": []}
    with pytest.raises(V2Error) as muted:
        await m.client.qq.mute_multi_member("guild", ["u"], mute_seconds="30", operation_id="mute-once")
    assert muted.value.phase == "partial" and muted.value.details["failed"] == ["u"]
    assert len(m.calls) == 2

@pytest.mark.parametrize("name,invoke,code,phase", [
    ("blacklist-429", lambda q: q.set_group_member_blacklist("g", "add", ["u"], operation_id="blacklist-429"), "qq_rate_limited", "rejected"),
    ("strategy-500", lambda q: q.update_join_approval_strategy("strategy", remark="again", operation_id="strategy-500"), "qq_api_error", "result_unknown"),
    ("guild-cancel", lambda q: q.create_guild_role("guild", name="Role", operation_id="guild-cancel"), None, None),
])
async def test_m6_refusal_unknown_and_cancellation_never_replay(admin, name, invoke, code, phase):
    m = admin
    calls = []
    async def fail(spec, *, before_send=None):
        before_send()
        calls.append((spec.method, spec.url, spec.json_body))
        if code is None:
            raise asyncio.CancelledError
        raise V2Error(code, "fixture", phase=phase, http_status=429 if phase == "rejected" else 500)
    m.client._state.http.request = fail
    with pytest.raises(asyncio.CancelledError if code is None else V2Error) as first:
        await invoke(m.client.qq)
    if code is not None:
        assert first.value.code == code and first.value.phase == phase
    row = m.extension.operation(m.client.identity.robot, name)
    assert row["state"] == ("rejected" if phase == "rejected" else "unknown")
    with pytest.raises(V2Error) as repeated:
        await invoke(m.client.qq)
    assert repeated.value.code in {"operation_already_attempted", "extension_result_unknown"}
    assert len(calls) == 1



async def test_blacklist_limit_is_independent_of_group_member_limit_and_does_not_lookup_members(admin):
    m = admin
    m.response[0] = {"strategy_id": "strat", "whitelist_user_count": 10000}
    numbers = [str(value + 100_000) for value in range(10000)]
    result = await m.client.qq.set_join_approval_whitelist("strat", "add", numbers)
    assert result["whitelist_user_count"] == 10000 and len(m.calls[0].json_body["whitelist_users"]) == 10000
    with pytest.raises(V2Error) as over:
        await m.client.qq.set_join_approval_whitelist("strat", "add", numbers + ["200000"])
    assert over.value.code == "invalid_members" and len(m.calls) == 1
