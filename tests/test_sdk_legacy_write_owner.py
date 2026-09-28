"""Old named management writes keep the owned view at each pre-wire boundary."""

import asyncio
import hashlib
import inspect
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_transport_http import MappedSession, upstream

from v2.extensions.management import Management, NATIVE_ACTIONS, NATIVE_READ_ACTIONS

pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


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


WRITE_CASES = [
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


def prepare_call(p, name, args):
    if name == "approve":
        now = p.manager.store.now()
        flag = p.manager.observe_request("g", {"member_openid": "u", "join_request_id": "request-1",
            "apply_source": "self_apply", "apply_at": datetime.fromtimestamp(now, UTC).isoformat()})
        assert flag
        return (flag,), {"approve": True}, "approve-" + hashlib.sha256(flag.encode()).hexdigest()
    op_id = "legacy-" + name
    if name == "delete_message":
        store = p.manager.store
        route = p.instance.client.route_for("group", "g")
        store.reserve(route, None, "fixture-message", "seed-send")
        store.finish(route.robot, "seed-send", "sent", result={"message_id": "msg", "wire_started": store.now()})
    if name == "group_kick":
        return args, {"blacklist": True, "operation_id": op_id}, op_id
    return args, {"operation_id": op_id}, op_id


def test_legacy_write_catalog_is_explicit_and_every_method_accepts_owner_guard():
    names = {name for name, *_ in WRITE_CASES}
    assert names == NATIVE_ACTIONS - NATIVE_READ_ACTIONS
    for name in names:
        assert inspect.signature(getattr(Management, name)).parameters["guard"].kind is inspect.Parameter.KEYWORD_ONLY


@pytest.mark.parametrize("name,args,method,path", WRITE_CASES)
async def test_revoked_legacy_write_waiting_for_token_never_starts_business_wire(legacy_wire, name, args, method, path):
    p = legacy_wire
    args, kwargs, op_id = prepare_call(p, name, args)
    p.hold_token[0] = True
    plugin = object()
    view = p.instance.client.qq.with_options(owner=plugin)
    task = asyncio.create_task(getattr(view, name)(*args, **kwargs))
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
        if name in {"group_ban", "group_kick", "approve"}:
            assert not rows  # An authorized bot-state GET did not finish.
        else:
            assert [row[0] for row in rows] == ["not_sent"]
        assert [(method, url) for method, url, _ in p.instance.http.session.calls] == [
            ("POST", "https://api.bot.qq.com/app/getAppAccessToken")]
    finally:
        p.token_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("name,args,method,path", WRITE_CASES)
async def test_live_legacy_write_uses_literal_http_and_retained_operation(legacy_wire, name, args, method, path):
    p = legacy_wire
    args, kwargs, op_id = prepare_call(p, name, args)
    view = p.instance.client.qq.with_options(owner=object())
    result = await getattr(view, name)(*args, **kwargs)
    assert isinstance(result, dict)
    assert [(actual, target) for actual, target, _ in p.calls if actual != "GET"] == [(method, path)]
    assert p.instance.http.session.calls[-1][:2] == (method, "https://api.bot.qq.com" + path)
    assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, op_id)["state"] == "succeeded"


@pytest.mark.parametrize("kind", ["owned", "unowned"])
async def test_live_legacy_owner_and_default_view_keep_actual_write_result(legacy_wire, kind):
    p = legacy_wire
    view = p.instance.client.qq.with_options(owner=object()) if kind == "owned" else p.instance.client.qq
    op_id = "live-mute-" + kind
    result = await view.guild_mute("g", "u", 60, operation_id=op_id)
    assert result == {"state": "succeeded"}
    assert p.calls == [("PATCH", "/guilds/g/members/u/mute", {"mute_seconds": "60"})]
    assert p.instance.http.session.calls[-1][:2] == ("PATCH", "https://api.bot.qq.com/guilds/g/members/u/mute")
    assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, op_id)["state"] == "succeeded"


async def test_owner_unloaded_after_wire_keeps_confirmed_write_and_ledger(legacy_wire):
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
        p.write_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_revocation_after_group_prequery_never_starts_followup_write(legacy_wire, monkeypatch):
    p = legacy_wire
    entered, release = asyncio.Event(), asyncio.Event()
    original = p.manager.group_member
    async def delayed(*args, **kwargs):
        member = await original(*args, **kwargs)
        entered.set()
        await release.wait()
        return member
    monkeypatch.setattr(p.manager, "group_member", delayed)
    plugin = object()
    task = asyncio.create_task(p.instance.client.qq.with_options(owner=plugin).group_ban("g", "u", 60, operation_id="prequery-ban"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert [path for _, path, _ in p.calls] == ["/v2/groups/g/bot_state", "/v2/groups/g/members/u"]
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        release.set()
        with pytest.raises(RuntimeError) as stopped:
            await task
        assert stopped.value.code == "stale_owner" and [path for _, path, _ in p.calls] == [
            "/v2/groups/g/bot_state", "/v2/groups/g/members/u"]
        assert not p.instance.client._state.extension_state.db.execute(
            "SELECT 1 FROM extension_ops WHERE op_id='prequery-ban'").fetchone()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_revoked_approval_cannot_claim_flag_after_authority_check(legacy_wire, monkeypatch):
    p = legacy_wire
    (flag,), kwargs, op_id = prepare_call(p, "approve", ())
    entered, release = asyncio.Event(), asyncio.Event()
    original = p.manager._admin
    async def delayed(*args, **options):
        await original(*args, **options)
        entered.set()
        await release.wait()
    monkeypatch.setattr(p.manager, "_admin", delayed)
    plugin = object()
    task = asyncio.create_task(p.instance.client.qq.with_options(owner=plugin).approve(flag, **kwargs))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        release.set()
        with pytest.raises(RuntimeError) as stopped:
            await task
        assert stopped.value.code == "stale_owner" and stopped.value.phase == "not_sent"
        assert p.calls == [("GET", "/v2/groups/g/bot_state", None)]
        assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, op_id)["state"] == "not_sent"
        assert p.manager.store.db.execute("SELECT state FROM join_flags WHERE flag_hash=?", (hashlib.sha256(flag.encode()).hexdigest(),)).fetchone()[0] == "retryable"
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_partial_group_removal_stays_partial_if_owner_unloads_after_wire(legacy_wire):
    p = legacy_wire
    p.partial_kick[0] = p.hold_write[0] = True
    plugin = object()
    view = p.instance.client.qq.with_options(owner=plugin)
    task = asyncio.create_task(view.group_kick("g", ["u"], blacklist=True, operation_id="partial-owned-kick"))
    try:
        await asyncio.wait_for(p.write_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.write_release.set()
        with pytest.raises(RuntimeError) as partial:
            await task
        assert partial.value.code == "partial_failure" and partial.value.phase == "partial"
        assert p.instance.client._state.extension_state.operation(p.instance.identity.robot, "partial-owned-kick")["state"] == "partial"
        assert [path for method, path, _ in p.calls if method == "POST"] == ["/v2/groups/g/batch_remove_members"]
        with pytest.raises(RuntimeError) as replay:
            await p.instance.client.qq.group_kick("g", ["u"], blacklist=True, operation_id="partial-owned-kick")
        assert replay.value.phase == "partial"
        assert [path for method, path, _ in p.calls if method == "POST"] == ["/v2/groups/g/batch_remove_members"]
    finally:
        p.write_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
