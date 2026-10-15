"""Literal QQ M6 request, result, permission, partial-outcome, and legacy owner-guard fixtures."""

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from aiohttp import web
from test_transport_http import MappedSession, upstream

from v2.errors import V2Error
from v2.extensions.management import NATIVE_ACTIONS, NATIVE_READ_ACTIONS, Management

pytest_plugins = ("test_lifecycle", "test_messaging_delivery", "test_sdk_native_writes")

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


async def test_named_m6_requests_exact_origin_body_and_raw_result(admin):
    m = admin
    for identifier, method, path, call, body, response in WRITES:
        m.calls.clear()
        m.response[0] = response
        actual = await call(m.client.qq)
        assert actual == response, identifier
        assert len(m.calls) == 1, identifier
        spec = m.calls[0]
        url = urlsplit(spec.url)
        assert (url.scheme, url.hostname, url.path, url.query, spec.method, spec.json_body, spec.params) == (
            "https", "api.bot.qq.com", path, "", method, body, None), identifier
        record = m.extension.recent(m.client.identity.robot, 1)[0]
        assert "qun.qq.com" not in str(record)


async def test_m6_disabled_management_partial_outcomes_and_blacklist_limits(admin):
    m = admin
    m.policy["management_writes"] = False
    with pytest.raises(V2Error, match="fixture"):
        await m.client.qq.put_pin("c", "m", operation_id="no-write")
    assert not m.calls
    from v2.protocol import RequestSpec
    with pytest.raises(V2Error) as read_only:
        await m.client.qq.request(RequestSpec("production", "POST", "/v2/menu", json_body={}))
    assert read_only.value.code == "unsupported" and not m.calls

    m.policy["management_writes"] = True
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

    m.calls.clear()
    m.response[0] = {"strategy_id": "strat", "whitelist_user_count": 10000}
    numbers = [str(value + 100_000) for value in range(10000)]
    result = await m.client.qq.set_join_approval_whitelist("strat", "add", numbers)
    assert result["whitelist_user_count"] == 10000 and len(m.calls[0].json_body["whitelist_users"]) == 10000
    with pytest.raises(V2Error) as over:
        await m.client.qq.set_join_approval_whitelist("strat", "add", numbers + ["200000"])
    assert over.value.code == "invalid_members" and len(m.calls) == 1


async def test_m6_refusal_unknown_and_cancellation_never_replay(admin):
    m = admin
    for name, invoke, code, phase in (
        ("blacklist-429", lambda q: q.set_group_member_blacklist("g", "add", ["u"], operation_id="blacklist-429"), "qq_rate_limited", "rejected"),
        ("strategy-500", lambda q: q.update_join_approval_strategy("strategy", remark="again", operation_id="strategy-500"), "qq_api_error", "result_unknown"),
        ("guild-cancel", lambda q: q.create_guild_role("guild", name="Role", operation_id="guild-cancel"), None, None),
    ):
        calls = []

        async def fail(spec, *, before_send=None, _code=code, _phase=phase):
            before_send()
            calls.append((spec.method, spec.url, spec.json_body))
            if _code is None:
                raise asyncio.CancelledError
            raise V2Error(_code, "fixture", phase=_phase, http_status=429 if _phase == "rejected" else 500)

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


@pytest.fixture
async def legacy_wire(receiver):
    owner, instance = receiver
    token_entered, token_release = asyncio.Event(), asyncio.Event()
    write_entered, write_release = asyncio.Event(), asyncio.Event()
    hold_token, hold_write, partial_kick = [False], [False], [False]
    calls = []

    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            if hold_token[0]:
                token_entered.set()
                await token_release.wait()
            return web.json_response({"access_token": "legacy-fixture", "expires_in": 7200})
        assert request.headers["Authorization"] == "QQBot legacy-fixture"
        body = await request.text()
        body = json.loads(body) if body else None
        calls.append((request.method, request.path, body))
        if request.method == "GET":
            if request.path.endswith("/bot_state"):
                return web.json_response({"member_openid": "bot", "member_role": "admin"})
            if request.path.endswith("/members/u"):
                return web.json_response({"member_openid": "u", "username": "Actual", "member_role": "member", "bot": False})
            assert request.path.endswith("/restrict_chat_setting")
            return web.json_response({"members": [{"member_openid": "u"}]})
        if hold_write[0]:
            write_entered.set()
            await write_release.wait()
        if request.path == "/v2/generate_url_link":
            return web.json_response({"data": {"url": "https://qun.qq.com/qunpro/robot/qunshare"}})
        if request.path.endswith("/batch_remove_members"):
            return web.json_response({"remove_members_result": "success", "add_to_member_blacklist_fail_openids": ["u"] if partial_kick[0] else []})
        if request.path == "/guilds/g/channels":
            return web.json_response({"guild_id": "g", "id": "ch"})
        if request.path == "/channels/ch" and request.method == "PATCH":
            return web.json_response({"id": "ch"})
        if request.path == "/guilds/g/mute":
            return web.json_response({"user_ids": ["u"]})
        return web.Response(status=204)

    async with upstream(handler) as base:
        instance.http._factory = lambda: MappedSession(base)
        manager = instance.client._state.management
        settings = manager.settings
        manager.settings = lambda: {**settings(), "management_writes": True}
        try:
            yield SimpleNamespace(owner=owner, instance=instance, manager=manager, calls=calls,
                hold_token=hold_token, token_entered=token_entered, token_release=token_release,
                hold_write=hold_write, write_entered=write_entered, write_release=write_release,
                partial_kick=partial_kick)
        finally:
            token_release.set()
            write_release.set()
            await instance.http.close()


LEGACY_WRITE_CASES = [
    ("group_ban", ("g", "u", 60), "POST", "/v2/groups/g/restrict_chat_setting"),
    ("group_kick", ("g", ["u"]), "POST", "/v2/groups/g/batch_remove_members"),
    ("approve", (), "POST", "/v2/groups/g/approval_join_request/u"),
    ("share", ("callback",), "POST", "/v2/generate_url_link"),
    ("delete_message", ("msg",), "DELETE", "/v2/groups/g/messages/msg"),
    ("channel_create", ("g", {"name": "new", "type": 0}), "POST", "/guilds/g/channels"),
    ("channel_update", ("ch", {"name": "new"}), "PATCH", "/channels/ch"),
    ("channel_delete", ("ch",), "DELETE", "/channels/ch"),
    ("guild_kick", ("g", "u"), "DELETE", "/guilds/g/members/u"),
    ("guild_mute", ("g", "u", 60), "PATCH", "/guilds/g/members/u/mute"),
    ("guild_mute_batch", ("g", ["u"], 60), "PATCH", "/guilds/g/mute"),
]


def prepare_legacy_call(p, name, args, suffix=""):
    if name == "approve":
        now = p.manager.store.now()
        req_id = f"request-1{suffix}"
        flag = p.manager.observe_request("g", {"member_openid": "u", "join_request_id": req_id,
            "apply_source": "self_apply", "apply_at": datetime.fromtimestamp(now, UTC).isoformat()})
        assert flag
        return (flag,), {"approve": True}, "approve-" + hashlib.sha256(flag.encode()).hexdigest()
    op_id = f"legacy-{name}{suffix}"
    if name == "delete_message":
        store = p.manager.store
        route = p.instance.client.route_for("group", "g")
        seed = f"seed-send{suffix}"
        store.reserve(route, None, "fixture-message", seed)
        store.finish(route.robot, seed, "sent", result={"message_id": "msg", "wire_started": store.now()})
    if name == "group_kick":
        return args, {"blacklist": True, "operation_id": op_id}, op_id
    return args, {"operation_id": op_id}, op_id


async def test_legacy_write_catalog_and_live_literal_http(legacy_wire):
    names = {name for name, *_ in LEGACY_WRITE_CASES}
    assert names == NATIVE_ACTIONS - NATIVE_READ_ACTIONS
    for name in names:
        assert inspect.signature(getattr(Management, name)).parameters["guard"].kind is inspect.Parameter.KEYWORD_ONLY

    p = legacy_wire
    for name, args, method, path in LEGACY_WRITE_CASES:
        p.calls.clear()
        call_args, kwargs, op_id = prepare_legacy_call(p, name, args)
        view = p.instance.client.qq.with_options(owner=object())
        result = await getattr(view, name)(*call_args, **kwargs)
        assert isinstance(result, dict), name
        assert [(actual, target) for actual, target, _ in p.calls if actual != "GET"] == [(method, path)], name
        assert p.instance.http.session.calls[-1][:2] == (method, "https://api.bot.qq.com" + path), name
        assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, op_id)["state"] == "succeeded"

    for kind in ("owned", "unowned"):
        p.calls.clear()
        view = p.instance.client.qq.with_options(owner=object()) if kind == "owned" else p.instance.client.qq
        op_id = "live-mute-" + kind
        result = await view.guild_mute("g", "u", 60, operation_id=op_id)
        assert result == {"state": "succeeded"}
        assert p.calls == [("PATCH", "/guilds/g/members/u/mute", {"mute_seconds": "60"})]


@pytest.mark.parametrize("name,args", [
    ("group_ban", ("g", "u", 60)),
    ("approve", ()),
    ("delete_message", ("msg",)),
    ("channel_create", ("g", {"name": "new", "type": 0})),
])
async def test_revoked_legacy_write_waiting_for_token_never_starts_business_wire(legacy_wire, name, args):
    p = legacy_wire
    call_args, kwargs, op_id = prepare_legacy_call(p, name, args, "-tok")
    p.hold_token[0] = True
    plugin = object()
    view = p.instance.client.qq.with_options(owner=plugin)
    task = asyncio.create_task(getattr(view, name)(*call_args, **kwargs))
    try:
        await asyncio.wait_for(p.token_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.token_release.set()
        with pytest.raises(RuntimeError) as stopped:
            await task
        assert stopped.value.code == "stale_owner" and stopped.value.phase == "not_sent"
        assert p.calls == []
        rows = p.instance.client._state.extension_state.db.execute(
            "SELECT state FROM extension_ops WHERE op_id=?", (op_id,)).fetchall()
        if name in {"group_ban", "approve"}:
            assert not rows
        else:
            assert [row[0] for row in rows] == ["not_sent"]
    finally:
        p.token_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_legacy_owner_unloaded_after_wire_or_prequery_and_partial_kick(legacy_wire, monkeypatch):
    p = legacy_wire
    p.hold_write[0] = True
    plugin = object()
    view = p.instance.client.qq.with_options(owner=plugin)
    task = asyncio.create_task(view.guild_mute("g", "u", 60, operation_id="inflight-mute"))
    try:
        await asyncio.wait_for(p.write_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.write_release.set()
        assert await task == {"state": "succeeded"}
        assert p.calls == [("PATCH", "/guilds/g/members/u/mute", {"mute_seconds": "60"})]
        assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, "inflight-mute")["state"] == "succeeded"
    finally:
        p.hold_write[0] = False
        p.write_release.set()

    p.calls.clear()
    entered, release = asyncio.Event(), asyncio.Event()
    original_member = p.manager.group_member

    async def delayed_member(*args, **kwargs):
        member = await original_member(*args, **kwargs)
        entered.set()
        await release.wait()
        return member

    monkeypatch.setattr(p.manager, "group_member", delayed_member)
    plugin2 = object()
    ban_task = asyncio.create_task(p.instance.client.qq.with_options(owner=plugin2).group_ban("g", "u", 60, operation_id="prequery-ban"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin2))
        release.set()
        with pytest.raises(RuntimeError) as stopped:
            await ban_task
        assert stopped.value.code == "stale_owner" and [path for _, path, _ in p.calls] == [
            "/v2/groups/g/bot_state", "/v2/groups/g/members/u"]
        assert not p.instance.client._state.extension_state.db.execute(
            "SELECT 1 FROM extension_ops WHERE op_id='prequery-ban'").fetchone()
    finally:
        release.set()
        monkeypatch.setattr(p.manager, "group_member", original_member)

    p.calls.clear()
    (flag,), kwargs, op_id = prepare_legacy_call(p, "approve", (), "-rev")
    entered2, release2 = asyncio.Event(), asyncio.Event()
    original_admin = p.manager._admin

    async def delayed_admin(*args, **options):
        await original_admin(*args, **options)
        entered2.set()
        await release2.wait()

    monkeypatch.setattr(p.manager, "_admin", delayed_admin)
    plugin3 = object()
    approve_task = asyncio.create_task(p.instance.client.qq.with_options(owner=plugin3).approve(flag, **kwargs))
    try:
        await asyncio.wait_for(entered2.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin3))
        release2.set()
        with pytest.raises(RuntimeError) as stopped:
            await approve_task
        assert stopped.value.code == "stale_owner" and stopped.value.phase == "not_sent"
        assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, op_id)["state"] == "not_sent"
        assert p.manager.store.db.execute("SELECT state FROM join_flags WHERE flag_hash=?", (hashlib.sha256(flag.encode()).hexdigest(),)).fetchone()[0] == "retryable"
    finally:
        release2.set()
        monkeypatch.setattr(p.manager, "_admin", original_admin)

    p.calls.clear()
    p.write_entered.clear()
    p.write_release.clear()
    p.partial_kick[0] = p.hold_write[0] = True
    plugin4 = object()
    kick_task = asyncio.create_task(p.instance.client.qq.with_options(owner=plugin4).group_kick("g", ["u"], blacklist=True, operation_id="partial-owned-kick"))
    try:
        await asyncio.wait_for(p.write_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin4))
        p.write_release.set()
        with pytest.raises(RuntimeError) as partial:
            await kick_task
        assert partial.value.code == "partial_failure" and partial.value.phase == "partial"
        assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, "partial-owned-kick")["state"] == "partial"
        with pytest.raises(RuntimeError) as replay:
            await p.instance.client.qq.group_kick("g", ["u"], blacklist=True, operation_id="partial-owned-kick")
        assert replay.value.phase == "partial"
    finally:
        p.partial_kick[0] = p.hold_write[0] = False
        p.write_release.set()
