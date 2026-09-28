"""Owned profile handles preserve lifecycle checks across shared reads and QQ I/O."""

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from test_transport_http import MappedSession, upstream

pytest_plugins = ("test_lifecycle", "test_messaging_delivery")


@pytest.fixture
async def profile_wire(receiver):
    owner, instance = receiver
    calls = []
    token_entered, token_release = asyncio.Event(), asyncio.Event()
    page_entered, page_release = asyncio.Event(), asyncio.Event()
    member_entered, member_release = asyncio.Event(), asyncio.Event()
    hold_token, hold_page, hold_member, deny = [False], [False], [False], [False]
    async def handler(request):
        if request.path == "/app/getAppAccessToken":
            if hold_token[0]:
                token_entered.set()
                await token_release.wait()
            return web.json_response({"access_token": "profile-fixture", "expires_in": 7200})
        assert request.method == "GET" and request.headers["Authorization"] == "QQBot profile-fixture"
        calls.append((request.method, request.path, dict(request.query)))
        if request.path in {"/v2/groups/g/members/u", "/v2/groups/g/members/v"}:
            if hold_member[0]:
                member_entered.set()
                await member_release.wait()
            if deny[0]:
                return web.json_response({"code": 11253}, status=403)
            return web.json_response({"member_openid": request.path.rsplit("/", 1)[-1],
                                      "username": "Official", "member_role": "member"})
        if request.path == "/v2/groups/g/members":
            cursor = request.query.get("cursor", "")
            if not cursor:
                if hold_page[0]:
                    page_entered.set()
                    await page_release.wait()
                return web.json_response({"members": [{"member_openid": "u", "username": "Official"}], "next_cursor": "opaque&1"})
            assert cursor == "opaque&1"
            return web.json_response({"members": [{"member_openid": "v", "username": "Second"}], "next_cursor": ""})
        raise AssertionError(request.path)
    async with upstream(handler) as base:
        instance.http._factory = lambda: MappedSession(base)
        try:
            yield SimpleNamespace(owner=owner, instance=instance, calls=calls, hold_token=hold_token,
                token_entered=token_entered, token_release=token_release, hold_page=hold_page,
                page_entered=page_entered, page_release=page_release, hold_member=hold_member,
                member_entered=member_entered, member_release=member_release, deny=deny)
        finally:
            token_release.set()
            page_release.set()
            member_release.set()
            await instance.http.close()


@pytest.mark.parametrize("entry", ["cache_only", "prefer_cache", "refresh", "roster", "known", "status", "cached", "diagnostics"])
async def test_cached_owned_profile_handle_rejects_every_public_read_after_unload(receiver, entry):
    owner, instance = receiver
    root = instance.profiles
    root.store.merge(instance.identity.robot, "group", "g", "u", {"nickname": "Observed"}, source="chat_history", as_of=1)
    plugin = object()
    handle = instance.client.qq.with_options(owner=plugin).profiles
    if entry == "cache_only":
        with pytest.raises(AttributeError):
            _ = handle.reads
        with pytest.raises(AttributeError):
            _ = handle.store
    calls = []
    async def request(spec, *, before_send=None):
        calls.append((spec.method, spec.url))
        return SimpleNamespace(data={"members": [], "next_cursor": ""} if spec.path.endswith("/members")
                               else {"member_openid": "u", "username": "Never"})
    instance.http.request = request
    await owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
    with pytest.raises(RuntimeError) as error:
        if entry in {"cache_only", "prefer_cache", "refresh"}:
            await handle.get_member("g", "u", mode=entry)
        elif entry == "roster":
            await handle.refresh_roster("g", with_rows=True)
        else:
            {"known": lambda: handle.list_known_members("g"),
             "status": lambda: handle.get_roster_status("g"),
             "cached": lambda: handle.cached_roster("g"),
             "diagnostics": handle.diagnostics}[entry]()
    assert error.value.code == "stale_owner" and calls == []
    assert handle is not root and instance.http.session is None


async def test_owned_member_token_wait_revoked_before_business_get(profile_wire):
    p = profile_wire
    p.hold_token[0] = True
    plugin = object()
    handle = p.instance.client.qq.with_options(owner=plugin).profiles
    task = asyncio.create_task(handle.get_member("g", "u", mode="refresh"))
    try:
        await asyncio.wait_for(p.token_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.token_release.set()
        with pytest.raises(RuntimeError) as error:
            await task
        assert error.value.code == "stale_owner"
        assert not p.calls
        assert not p.instance.profiles.store.db.execute("SELECT 1 FROM refresh_state").fetchone()
    finally:
        p.token_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_revoked_owned_roster_stops_before_next_page_and_does_not_publish_complete(profile_wire):
    p = profile_wire
    p.hold_page[0] = True
    plugin = object()
    handle = p.instance.client.qq.with_options(owner=plugin).profiles
    task = asyncio.create_task(handle.refresh_roster("g", with_rows=True))
    try:
        await asyncio.wait_for(p.page_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.page_release.set()
        with pytest.raises(RuntimeError) as error:
            await task
        assert error.value.code == "stale_owner"
        assert p.calls == [("GET", "/v2/groups/g/members", {"cursor": ""})]
        assert not p.instance.profiles.get_roster_status("g")["complete"]
    finally:
        p.page_release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("second_kind", ["owned", "unowned"])
async def test_shared_member_refresh_survives_one_owner_unload(profile_wire, second_kind):
    p = profile_wire
    p.hold_member[0] = True
    first_owner, second_owner = object(), object()
    first = p.instance.client.qq.with_options(owner=first_owner).profiles
    second = (p.instance.client.qq.with_options(owner=second_owner).profiles if second_kind == "owned"
              else p.instance.client.qq.profiles)
    if second_kind == "owned":
        assert second is not p.instance.profiles and second._service is p.instance.profiles
    else:
        assert second is p.instance.profiles
    one = asyncio.create_task(first.get_member("g", "u", mode="refresh"))
    two = None
    try:
        await asyncio.wait_for(p.member_entered.wait(), 2)
        joined = asyncio.Event()
        async def another():
            joined.set()
            return await second.get_member("g", "u", mode="refresh")
        two = asyncio.create_task(another())
        await asyncio.wait_for(joined.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=first_owner))
        p.member_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await one
        assert invalid.value.code == "stale_owner"
        result = await two
        assert result["fields"]["nickname"]["value"] == "Official"
        assert p.calls == [("GET", "/v2/groups/g/members/u", {})]
        assert not p.instance.profiles.store.db.execute("SELECT 1 FROM refresh_state").fetchone()
    finally:
        p.member_release.set()
        for task in (one, two):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("second_kind", ["owned", "unowned"])
async def test_shared_roster_continues_one_pagination_after_owner_unload(profile_wire, second_kind):
    p = profile_wire
    p.hold_page[0] = True
    first_owner, second_owner = object(), object()
    first = p.instance.client.qq.with_options(owner=first_owner).profiles
    second = (p.instance.client.qq.with_options(owner=second_owner).profiles if second_kind == "owned"
              else p.instance.client.qq.profiles)
    one = asyncio.create_task(first.refresh_roster("g", with_rows=True))
    two = None
    try:
        await asyncio.wait_for(p.page_entered.wait(), 2)
        joined = asyncio.Event()
        async def another():
            joined.set()
            return await second.refresh_roster("g", with_rows=True)
        two = asyncio.create_task(another())
        await asyncio.wait_for(joined.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=first_owner))
        p.page_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await one
        assert invalid.value.code == "stale_owner"
        result = await two
        assert result["complete"] and [row["member_openid"] for row in result["rows"]] == ["u", "v"]
        assert p.calls == [("GET", "/v2/groups/g/members", {"cursor": ""}),
                           ("GET", "/v2/groups/g/members", {"cursor": "opaque&1"})]
        assert [call[1] for call in p.instance.http.session.calls if call[0] == "GET"] == [
            "https://api.bot.qq.com/v2/groups/g/members?cursor=",
            "https://api.bot.qq.com/v2/groups/g/members?cursor=opaque%261"]
    finally:
        p.page_release.set()
        for task in (one, two):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_all_owners_revoked_during_shared_token_wait_do_not_send_business_get(profile_wire):
    p = profile_wire
    p.hold_token[0] = True
    first_owner, second_owner = object(), object()
    one = asyncio.create_task(p.instance.client.qq.with_options(owner=first_owner).profiles.get_member("g", "u", mode="refresh"))
    two = None
    try:
        await asyncio.wait_for(p.token_entered.wait(), 2)
        joined = asyncio.Event()
        async def another():
            joined.set()
            return await p.instance.client.qq.with_options(owner=second_owner).profiles.get_member("g", "u", mode="refresh")
        two = asyncio.create_task(another())
        await asyncio.wait_for(joined.wait(), 2)
        for plugin in (first_owner, second_owner):
            await p.owner.catalog_unloaded(SimpleNamespace(star_cls=plugin))
        p.token_release.set()
        for task in (one, two):
            with pytest.raises(RuntimeError) as invalid:
                await task
            assert invalid.value.code == "stale_owner"
        assert not p.calls and not p.instance.profiles.store.db.execute("SELECT 1 FROM refresh_state").fetchone()
        fresh = p.instance.client.qq.with_options(owner=object()).profiles
        assert (await fresh.get_member("g", "u", mode="refresh"))["fields"]["nickname"]["value"] == "Official"
        assert p.calls == [("GET", "/v2/groups/g/members/u", {})]
    finally:
        p.token_release.set()
        for task in (one, two):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_valid_owner_keeps_one_business_get_after_another_revokes_during_token(profile_wire):
    p = profile_wire
    p.hold_token[0] = True
    first_owner, second_owner = object(), object()
    one = asyncio.create_task(p.instance.client.qq.with_options(owner=first_owner).profiles.get_member("g", "u", mode="refresh"))
    two = None
    try:
        await asyncio.wait_for(p.token_entered.wait(), 2)
        joined = asyncio.Event()
        async def another():
            joined.set()
            return await p.instance.client.qq.with_options(owner=second_owner).profiles.get_member("g", "u", mode="refresh")
        two = asyncio.create_task(another())
        await asyncio.wait_for(joined.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=first_owner))
        p.token_release.set()
        with pytest.raises(RuntimeError) as invalid:
            await one
        assert invalid.value.code == "stale_owner"
        assert (await two)["fields"]["nickname"]["value"] == "Official"
        assert p.calls == [("GET", "/v2/groups/g/members/u", {})]
        assert not p.instance.profiles.store.db.execute("SELECT 1 FROM refresh_state").fetchone()
    finally:
        p.token_release.set()
        for task in (one, two):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_closing_one_bound_profile_view_keeps_shared_request_and_root_alive(profile_wire):
    p = profile_wire
    p.hold_member[0] = True
    first = p.instance.client.qq.with_options(owner=object()).profiles
    second = p.instance.client.qq.with_options(owner=object()).profiles
    one = asyncio.create_task(first.get_member("g", "u", mode="refresh"))
    two = None
    try:
        await asyncio.wait_for(p.member_entered.wait(), 2)
        joined = asyncio.Event()
        async def another():
            joined.set()
            return await second.get_member("g", "u", mode="refresh")
        two = asyncio.create_task(another())
        await asyncio.wait_for(joined.wait(), 2)
        await first.close()
        p.member_release.set()
        with pytest.raises(RuntimeError) as stopped:
            await one
        assert stopped.value.code == "service_stopped"
        assert (await two)["fields"]["nickname"]["value"] == "Official"
        assert p.calls == [("GET", "/v2/groups/g/members/u", {})]
        assert (await second.get_member("g", "u", mode="cache_only"))["fields"]["nickname"]["value"] == "Official"
        await p.instance.profiles.close()
        with pytest.raises(RuntimeError) as root_stopped:
            await second.get_member("g", "u", mode="cache_only")
        assert root_stopped.value.code == "service_stopped"
    finally:
        p.member_release.set()
        for task in (one, two):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


async def test_platform_termination_closes_shared_profile_root(receiver):
    _, instance = receiver
    bound = instance.client.qq.with_options(owner=object()).profiles
    root = instance.profiles
    await instance.terminate()
    assert root.closed and instance.client._state.closed
    with pytest.raises(RuntimeError) as stopped:
        await bound.get_member("g", "u", mode="cache_only")
    assert stopped.value.code == "stale_generation"


async def test_unloaded_sole_owner_does_not_record_late_qq_denial_as_shared_cooldown(profile_wire):
    p = profile_wire
    p.hold_member[0] = True
    p.deny[0] = True
    owner = object()
    old = p.instance.client.qq.with_options(owner=owner).profiles
    waiting = asyncio.create_task(old.get_member("g", "v", mode="refresh"))
    try:
        await asyncio.wait_for(p.member_entered.wait(), 2)
        await p.owner.catalog_unloaded(SimpleNamespace(star_cls=owner))
        p.member_release.set()
        with pytest.raises(RuntimeError) as revoked:
            await waiting
        assert revoked.value.code == "stale_owner"
        assert not p.instance.profiles.store.db.execute("SELECT 1 FROM refresh_state").fetchone()
        p.deny[0] = False
        new = p.instance.client.qq.with_options(owner=object()).profiles
        assert (await new.get_member("g", "v", mode="prefer_cache"))["fields"]["nickname"]["value"] == "Official"
        assert p.calls == [("GET", "/v2/groups/g/members/v", {}), ("GET", "/v2/groups/g/members/v", {})]
    finally:
        p.member_release.set()
        if not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)


async def test_unowned_cache_and_official_denial_cooldown_remain_shared(profile_wire):
    p = profile_wire
    root = p.instance.profiles
    assert p.instance.client.qq.profiles is root
    owner = object()
    handle = p.instance.client.qq.with_options(owner=owner).profiles
    root.store.merge(p.instance.identity.robot, "group", "g", "u", {"nickname": "Observed"}, source="chat", as_of=1)
    assert (await handle.get_member("g", "u", mode="cache_only"))["fields"]["nickname"]["value"] == "Observed"
    assert not p.calls and p.instance.http.session is None
    p.deny[0] = True
    with pytest.raises(RuntimeError) as denied:
        await handle.get_member("g", "v", mode="refresh")
    assert denied.value.business_code == 11253 and len(p.calls) == 1
    with pytest.raises(RuntimeError) as cooling:
        await p.instance.client.qq.with_options(owner=object()).profiles.get_member("g", "v", mode="prefer_cache")
    assert cooling.value.code == "qq_api_error" and len(p.calls) == 1
    p.deny[0] = False
    fresh = p.instance.client.qq.with_options(owner=object()).profiles
    assert (await fresh.get_member("g", "v", mode="refresh"))["fields"]["nickname"]["value"] == "Official"
    assert p.calls == [("GET", "/v2/groups/g/members/v", {}), ("GET", "/v2/groups/g/members/v", {})]
