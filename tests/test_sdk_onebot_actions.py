"""New NapCat-style actions use existing management boundaries and scoped QQ reads."""

from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from v2.client import ClientState, V2Client
from v2.errors import V2Error

pytest_plugins = ("test_management", "test_sdk_native_writes")

async def test_onebot_mutes_and_bulk_kick_use_actual_qq_and_existing_partial_ledger(management):
    m = management
    state = ClientState(m.http.identity)
    state.management, state.http, state.extension_state = m.service, m.http, m.state
    client = V2Client(m.http.identity, state=state)
    mutes = await client.call_action("get_group_shut_list", group_id="g")
    assert mutes == [{"group_id": "g", "user_id": "u", "nickname": None,
                      "mute_expire_at": "2027-01-15T09:00:00+08:00", "_qq": {"source": "official_query"}}]
    assert m.calls[-1][:2] == ("GET", "/v2/groups/g/restrict_chat_setting")
    result = await client.call_action("set_group_kick_members", group_id="g", user_ids=["u"],
                                      reject_add_request=False, _qq_operation_id="ob-bulk")
    assert result["state"] == "succeeded" and result["removed"] == ["u"]
    assert m.calls[-1] == ("POST", "/v2/groups/g/batch_remove_members", {},
                           {"member_openids": ["u"], "add_to_member_blacklist": False})
    repeated = await client.call_action("set_group_kick_members", group_id="g", user_ids=["u"],
                                        reject_add_request=False, _qq_operation_id="ob-bulk")
    assert repeated == result
    assert sum(path.endswith("batch_remove_members") for _, path, *_ in m.calls) == 1
    m.policy["management_writes"] = False
    with pytest.raises(V2Error) as disabled:
        await client.call_action("set_group_kick_members", group_id="g", user_ids=["u"], _qq_operation_id="ob-disabled")
    assert disabled.value.code == "management_disabled"


async def test_onebot_blacklist_page_strategy_and_write_keep_native_parameters(native):
    m = native
    calls = []
    async def request(spec, *, before_send=None):
        if before_send:
            before_send()
        calls.append((spec.method, spec.url, spec.params, spec.json_body))
        if spec.method == "GET" and urlsplit(spec.url).path.endswith("/member_blacklist"):
            data = {"users": [{"member_openid": "001", "username": "Nick"}], "next_cursor": "next"}
        elif spec.method == "GET":
            data = {"list": [], "next_cursor": ""}
        else:
            data = {"fail_openids": []}
        return SimpleNamespace(data=data, status=200, trace_id="fixture")
    m.http.request = request
    m.client._state.management.check(write=True)
    page = await m.client.call_action("_qq_get_group_blacklist", group_id="g", cursor="0", limit=20)
    assert page["users"][0]["member_openid"] == "001"
    assert calls[-1] == ("GET", "https://api.bot.qq.com/v2/groups/g/member_blacklist?cursor=0&limit=20", {"cursor": "0", "limit": 20}, None)
    strategies = await m.client.call_action("_qq_get_join_approval_strategies", limit=10)
    assert strategies == {"list": [], "next_cursor": ""}
    assert calls[-1] == ("GET", "https://api.bot.qq.com/v2/groups/join_approval_strategy?limit=10", {"limit": 10}, None)
    result = await m.client.call_action("_qq_set_group_blacklist", group_id="g", op="add", user_ids=["001"],
                                        _qq_operation_id="ob-blacklist")
    assert result == {"fail_openids": []}
    assert calls[-1] == ("POST", "https://api.bot.qq.com/v2/groups/g/member_blacklist", None,
                         {"op": "add", "member_openids": ["001"]})
    assert m.extension.operation(m.client.identity.robot, "ob-blacklist")["state"] == "succeeded"
    m.policy["management_writes"] = False
    with pytest.raises(V2Error, match="fixture"):
        await m.client.call_action("_qq_set_group_blacklist", group_id="g", op="add", user_ids=["001"],
                                   _qq_operation_id="ob-blacklist-disabled")
    assert len(calls) == 3
