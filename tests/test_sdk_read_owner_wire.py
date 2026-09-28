"""Native read endpoints recheck the owned view at the actual HTTP wire boundary."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_transport_http import MappedSession, upstream

from v2.protocol import RequestSpec

pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


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


@pytest.mark.parametrize("entry", ["mixin", "member", "page", "iterate", "group", "management",
                                   "management_member", "management_members", "guild", "request_get", "request_head"])
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
        assert not any(call[0] in {"GET", "HEAD"} for call in p.instance.http.session.calls)
    finally:
        p.token_release.set()
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize("entry,path,method", [
    ("mixin", "/users/@me", "GET"),
    ("member", "/v2/groups/g/members/u", "GET"),
    ("page", "/v2/groups/g/members", "GET"),
    ("group", "/v2/groups/g/info", "GET"),
    ("management", "/v2/groups/g/info", "GET"),
    ("management_member", "/v2/groups/g/members/u", "GET"),
    ("guild", "/guilds/g", "GET"),
    ("request_get", "/users/@me", "GET"),
    ("request_head", "/users/@me", "HEAD"),
])
@pytest.mark.parametrize("kind", ["owned", "unowned"])
async def test_native_read_valid_owner_and_unowned_keep_literal_wire_and_response(read_wire, entry, path, method, kind):
    p = read_wire
    view = p.instance.client.qq.with_options(owner=object()) if kind == "owned" else p.instance.client.qq
    result = await named_read(view, entry, p.instance.identity.robot.environment)
    assert p.calls == [(method, path, {"cursor": ""} if entry == "page" else {})]
    calls = [call for call in p.instance.http.session.calls if call[0] != "POST"]
    assert [(call[0], call[1]) for call in calls] == [(method, "https://api.bot.qq.com" + path +
        ("?cursor=" if entry == "page" else ""))]
    if entry in {"request_get", "request_head"}:
        assert result.status == (204 if method == "HEAD" else 200)
    else:
        assert result is not None
    if entry == "page":
        assert result["next_cursor"] == "opaque&1"

@pytest.mark.parametrize("entry", ["iterate", "management_members"])
async def test_owned_member_iterators_keep_both_literal_pages(read_wire, entry):
    p = read_wire
    view = p.instance.client.qq.with_options(owner=object())
    result = await named_read(view, entry, p.instance.identity.robot.environment)
    assert [row["member_openid"] for row in result] == ["u", "v"]
    assert p.calls == [("GET", "/v2/groups/g/members", {"cursor": ""}),
                       ("GET", "/v2/groups/g/members", {"cursor": "opaque&1"})]


@pytest.mark.parametrize("entry", ["iterate", "management_members"])
async def test_revoked_owner_after_first_page_never_requests_second(read_wire, entry):
    p = read_wire
    p.business_blocked[0] = True
    owner = object()
    view = p.instance.client.qq.with_options(owner=owner)
    task = asyncio.create_task(named_read(view, entry, p.instance.identity.robot.environment))
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
        p.business_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)



async def test_native_read_retry_rechecks_owner_after_wait(read_wire):
    p = read_wire
    p.retry[0] = True
    waiting, release = asyncio.Event(), asyncio.Event()
    async def pause(_delay):
        waiting.set()
        await release.wait()
    p.instance.http.sleep = pause
    owner = object()
    task = asyncio.create_task(p.instance.client.qq.with_options(owner=owner).me())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        assert p.calls == [("GET", "/users/@me", {})]
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner))
        release.set()
        with pytest.raises(RuntimeError) as invalid:
            await task
        assert invalid.value.code == "stale_owner" and p.calls == [("GET", "/users/@me", {})]
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_raw_read_does_not_return_result_to_owner_unloaded_after_wire(read_wire):
    p = read_wire
    p.business_blocked[0] = True
    owner = object()
    view = p.instance.client.qq.with_options(owner=owner)
    task = asyncio.create_task(view.request(RequestSpec(p.instance.identity.robot.environment, "GET", "/users/@me")))
    try:
        await asyncio.wait_for(p.business_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner))
        p.business_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await task
        assert invalid.value.code == "stale_owner"
        assert p.calls == [("GET", "/users/@me", {})]
    finally:
        p.business_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_raw_read_rejects_arbitrary_writes_before_http(read_wire):
    p = read_wire
    view = p.instance.client.qq.with_options(owner=object())
    with pytest.raises(RuntimeError) as denied:
        await view.request(RequestSpec(p.instance.identity.robot.environment, "POST", "/v2/groups/g/messages", json_body={"content": "x"}))
    assert denied.value.code == "unsupported" and not p.calls and p.instance.http.session is None
