"""Durable membership and profile merge rules remain independent of chat delivery."""

import asyncio
import sqlite3
import time
from types import SimpleNamespace

import pytest

from v2.adapter import V2Adapter
from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey
from v2.onebot_profiles import OneBotProfiles
from v2.profiles.service import Profiles
from v2.profiles.store import ProfileStore
from v2.protocol import RequestSpec
from v2.sdk.api.groups import GroupReads

pytest_plugins = ("test_management",)

@pytest.fixture
def profiles(tmp_path):
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    yield store
    store.close()


def test_history_leave_rejoin_and_conflicting_seconds(profiles):
    robot = RobotKey("app")
    profiles.merge(robot, "group", "g", "001", {"nickname": "new"}, source="current_chat", as_of=300)
    profiles.merge(robot, "group", "g", "001", {"nickname": "old", "bot": False}, source="chat_history", as_of=100)
    profiles.member_event(robot, "group", "g", "001", "present", 300, connection="s:0/1", sequence=1)
    profiles.member_event(robot, "group", "g", "001", "left", 301, connection="s:0/1", sequence=2)
    assert profiles.get_member(robot, "group", "g", "001")["fields"]["nickname"]["value"] == "new"
    assert profiles.get_member(robot, "group", "g", "001")["membership"] == "left"
    profiles.member_event(robot, "group", "g", "001", "present", 300, connection="s:0/1", sequence=1)
    assert profiles.get_member(robot, "group", "g", "001")["membership"] == "left"
    profiles.member_event(robot, "group", "g", "001", "present", 301, connection="other:0/1", sequence=4)
    assert profiles.get_member(robot, "group", "g", "001")["membership"] == "unknown"
    assert profiles.get_member(robot, "group", "g", "001")["confirmed"] == "left"
    profiles.member_event(robot, "group", "g", "001", "present", 302, connection="s:0/1", sequence=5)
    assert profiles.get_member(robot, "group", "g", "001")["membership"] == "present"
    with pytest.raises(V2Error):
        profiles.get_member(RobotKey("other"), "group", "g", "001")


def test_late_query_and_roster_gap_never_claim_current(profiles):
    robot = RobotKey("app")
    before = profiles.revision(robot, "group", "g")
    started = time.time() - 2
    profiles.member_event(robot, "group", "g", "001", "left", int(started + 1), connection="s:0/1", sequence=5)
    assert not profiles.query_member(robot, "group", "g", "001", started_revision=before,
                                     started_at=started, fields={"nickname": "known", "last_known_role": "admin"})
    profiles.merge(robot, "group", "g", "001", {"nickname": "newer"}, source="current_chat", as_of=started + 1, received=started + 1)
    assert not profiles.query_member(robot, "group", "g", "001", started_revision=before,
                                     started_at=started, fields={"nickname": "older"})
    result = profiles.get_member(robot, "group", "g", "001")
    assert result["membership"] == "left" and result["fields"]["last_known_role"]["value"] == "admin"
    assert result["fields"]["nickname"]["value"] == "newer"
    assert not profiles.record_roster(robot, "group", "g", [], started_revision=before, started_at=started)
    current_revision = profiles.revision(robot, "group", "g")
    assert not profiles.record_roster(robot, "group", "g", [], started_revision=current_revision, started_at=time.time())
    assert profiles.roster_status(robot, "group", "g")["continuous"] is False
    known = profiles.list_known_members(robot, "group", "g")["members"]
    assert len(known) == 1 and known[0]["user_id"] == "001"

def test_same_second_query_and_event_conflict_is_unknown(profiles):
    robot = RobotKey("app")
    started = time.time()
    assert profiles.query_member(robot, "group", "g", "001", started_revision=0, started_at=started,
                                 fields={"nickname": "known"})
    profiles.member_event(robot, "group", "g", "001", "left", int(started), connection="s:0/1", sequence=1)
    assert profiles.get_member(robot, "group", "g", "001")["membership"] == "unknown"


async def test_cache_only_in_sandbox_does_not_touch_network(profiles):
    identity = InstanceKey("p", RobotKey("app", "sandbox"))
    profiles.merge(identity.robot, "group", "g", "001", {"nickname": "cached"}, source="history", as_of=100)
    class Reads:
        http = SimpleNamespace(check=lambda: (_ for _ in ()).throw(AssertionError("network access")))
    service = Profiles(identity, profiles, Reads())
    try:
        result = await service.get_member("g", "001", mode="cache_only")
        assert result["fields"]["nickname"]["value"] == "cached"
    finally:
        await service.close()



def test_capacity_and_restart_keep_existing_history(tmp_path):
    path = tmp_path / "profiles.sqlite3"
    robot = RobotKey("app")
    store = ProfileStore(path, max_profiles=1)
    store.merge(robot, "group", "g", "001", {"nickname": "known"}, source="chat", as_of=100)
    store.merge(RobotKey("other"), "group", "g", "003", {"nickname": "other"}, source="chat", as_of=100)
    assert store.get_member(RobotKey("other"), "group", "g", "003")["fields"]["nickname"]["value"] == "other"
    with pytest.raises(V2Error) as exc:
        store.merge(robot, "group", "g", "002", {"nickname": "new"}, source="chat", as_of=100)
    assert exc.value.code == "cache_capacity"
    store.member_event(robot, "group", "g", "001", "left", 100)
    with pytest.raises(V2Error) as full_member:
        store.member_event(robot, "group", "g", "002", "present", 101)
    assert full_member.value.code == "cache_capacity"
    store.member_event(RobotKey("other"), "group", "g", "003", "present", 101)
    store.close()
    reopened = ProfileStore(path, max_profiles=1)
    try:
        assert reopened.get_member(robot, "group", "g", "001")["fields"]["nickname"]["value"] == "known"
        assert reopened.get_member(robot, "group", "g", "001")["membership"] == "left"
        reopened.db.execute("PRAGMA user_version=2")
        reopened.db.commit()
    finally:
        reopened.close()
    with pytest.raises(V2Error, match="schema"):
        ProfileStore(path)


async def test_profile_capacity_error_is_scoped_across_robots_and_groups(tmp_path):
    now = 10_000.0
    store = ProfileStore(tmp_path / "profiles.sqlite3", max_profiles=2, clock=lambda: now)
    a = InstanceKey("a", RobotKey("robot-a"))
    b = InstanceKey("b", RobotKey("robot-b"))
    calls, expanded = [], [False]
    class Reads:
        def __init__(self, label):
            self.label = label
        async def get_group_member_list(self, group, cursor):
            calls.append((self.label, group, cursor))
            assert cursor == ""
            members = [{"member_openid": self.label + "-" + group, "username": "Known"}]
            if self.label == "a" and group == "g" and expanded[0]:
                members.append({"member_openid": "g-two", "username": "Recovered"})
            return {"members": members, "next_cursor": ""}
    view_a = Profiles(a, store, Reads("a"), continuity_check=lambda: "a-connected")
    view_b = Profiles(b, store, Reads("b"), continuity_check=lambda: "b-connected")
    try:
        for group in ("g", "h"):
            assert (await view_a.refresh_roster(group))["continuous"]
        assert (await view_b.refresh_roster("g"))["continuous"]
        assert view_a.cached_roster("h") and view_b.cached_roster("g")
        a_revision = store.revision(a.robot, "group", "g")
        b_revision = store.revision(b.robot, "group", "g")
        with pytest.raises(V2Error) as full:
            store.merge(a.robot, "group", "g", "g-two", {"nickname": "Unstored"}, source="chat", as_of=now)
        assert full.value.code == "cache_capacity" and store.last_error == "cache_capacity"
        status = view_a.get_roster_status("g")
        assert not status["complete"] and not status["continuous"] and status["reason"] == "cache_capacity"
        assert store.revision(a.robot, "group", "g") > a_revision
        assert store.revision(b.robot, "group", "g") == b_revision
        assert view_a.get_roster_status("h")["continuous"] and view_a.cached_roster("h")
        assert view_b.get_roster_status("g")["continuous"]
        before = list(calls)
        state = ClientState(b)
        state.profiles = view_b
        members = await OneBotProfiles(V2Client(b, state=state)).members("g")
        assert [row["user_id"] for row in members] == ["b-g"] and calls == before
        with pytest.raises(V2Error) as member_full:
            store.member_event(a.robot, "group", "g", "g-extra", "present", int(now))
        assert member_full.value.code == "cache_capacity" and not view_a.get_roster_status("g")["continuous"]
        assert view_a.get_roster_status("h")["continuous"] and view_b.get_roster_status("g")["continuous"]
        store.max_profiles = 4
        expanded[0] = True
        recovered = await view_a.refresh_roster("g")
        assert recovered["complete"] and recovered["continuous"] and len(view_a.cached_roster("g")) == 2
        store.db.execute("""CREATE TRIGGER fail_specific_profile BEFORE INSERT ON profiles
            WHEN NEW.subject='faulty' BEGIN SELECT RAISE(FAIL,'disk failure'); END""")
        with pytest.raises(sqlite3.Error):
            store.merge(a.robot, "group", "g", "faulty", {"nickname": "Lost"}, source="chat", as_of=now)
        assert view_a.get_roster_status("g")["reason"] == "profile_storage_unavailable"
        assert view_b.get_roster_status("g")["continuous"] and view_a.get_roster_status("h")["continuous"]
        store.db.execute("DROP TRIGGER fail_specific_profile")
        assert (await view_a.refresh_roster("g"))["continuous"]
        store.mark_gap(b.robot, "group", "g", reason="receiver_disconnected")
        assert store.revision(b.robot, "group", "g") > b_revision
        assert not view_b.get_roster_status("g")["continuous"] and view_b.cached_roster("g") is None
        assert view_a.get_roster_status("h")["continuous"]
    finally:
        await view_a.close()
        await view_b.close()
        store.close()

@pytest.mark.parametrize("fault", ["capacity", "sqlite_write"])
@pytest.mark.parametrize("first", ["public", "explicit"])
async def test_scoped_profile_fault_recovers_without_member_intent_from_public_roster(tmp_path, fault, first):
    now = [10000.0]
    store = ProfileStore(tmp_path / "profiles.sqlite3", max_profiles=1, clock=lambda: now[0])
    a, b = InstanceKey("a", RobotKey("robot-a")), InstanceKey("b", RobotKey("robot-b"))
    calls, grow = [], [False]
    class Reads:
        def __init__(self, label):
            self.label = label
        async def get_group_member_list(self, group, cursor):
            calls.append((self.label, group, cursor))
            assert cursor == ""
            members = [{"member_openid": self.label + "-g", "username": "Known"}]
            if self.label == "a" and grow[0]:
                members.append({"member_openid": "extra", "username": "New"})
            return {"members": members, "next_cursor": ""}
    view_a, view_b = Profiles(a, store, Reads("a"), continuity_check=lambda: None), Profiles(
        b, store, Reads("b"), continuity_check=lambda: "b-online")
    try:
        assert (await view_a.refresh_roster("g"))["complete"]
        assert (await view_b.refresh_roster("g"))["continuous"]
        assert view_b.cached_roster("g") is not None
        if fault == "capacity":
            with pytest.raises(V2Error) as full:
                store.merge(a.robot, "group", "g", "extra", {"nickname": "Failed"}, source="chat", as_of=now[0])
            assert full.value.code == "cache_capacity"
            grow[0] = True
            with pytest.raises(V2Error):
                await view_a.refresh_roster("g")
            grow[0] = False
            store.max_profiles = 2
        else:
            store.db.execute("""CREATE TRIGGER fail_profile BEFORE UPDATE ON profiles
                WHEN NEW.subject='a-g' BEGIN SELECT RAISE(FAIL,'disk I/O fault'); END""")
            with pytest.raises(sqlite3.Error):
                store.merge(a.robot, "group", "g", "a-g", {"nickname": "Failed"},
                            source="chat", as_of=now[0] + 1)
            store.db.execute("DROP TRIGGER fail_profile")
        broken = view_a.get_roster_status("g")
        assert not broken["complete"] and not broken["continuous"]
        stale_revision = store.revision(a.robot, "group", "g") - 1
        assert not store.record_roster(a.robot, "group", "g", [], started_revision=stale_revision, started_at=now[0])
        assert not view_a.get_roster_status("g")["complete"]
        state = ClientState(a)
        state.profiles = view_a
        client = V2Client(a, state=state)
        before = len(calls)
        if first == "explicit":
            refreshed = await view_a.refresh_roster("g")
            assert refreshed["complete"] and not refreshed["continuous"] and len(calls) == before + 1
            before += 1
        for count in (1, 2):
            members = await client.call_action("get_group_member_list", group_id="g")
            assert [m["user_id"] for m in members] == ["a-g"]
            assert members[0]["_qq"]["current"] is False
            assert len(calls) == before + count
        status = view_a.get_roster_status("g")
        assert status["complete"] and not status["continuous"] and status["reason"] == "receiver_unavailable"
        assert view_b.cached_roster("g") is not None and view_b.get_roster_status("g")["continuous"]
    finally:
        await view_a.close()
        await view_b.close()
        store.close()


@pytest.mark.parametrize("online_at_start", [False, True])
async def test_failed_fresh_gap_requires_roster_to_span_one_stable_connection(tmp_path, online_at_start):
    identity = InstanceKey("p", RobotKey("a"), intents=1 << 24)
    other = InstanceKey("q", RobotKey("b"), intents=1 << 24)
    store = ProfileStore(tmp_path / "profiles.sqlite3", clock=lambda: 10000.0)
    for robot in (identity.robot, other.robot):
        store.set_continuity(robot, "group", "g", True)
        assert store.record_roster(robot, "group", "g", [{"member_openid": "u", "username": "Known"}],
                                   started_revision=store.revision(robot, "group", "g"), started_at=9999)
    gateway = SimpleNamespace(identity=identity, owner=SimpleNamespace(profiles=store), check_generation=lambda: None)
    store.db.execute("PRAGMA query_only=ON")
    V2Adapter._profile_fresh(gateway)
    assert store.robot_gap(identity.robot, "group", "g") == "profile_storage_unavailable"
    connection, requests = ["new-session" if online_at_start else None], []
    class Reads:
        async def get_group_member_list(self, group, cursor):
            requests.append((group, cursor, connection[0]))
            connection[0] = "new-session"
            store.db.execute("PRAGMA query_only=OFF")
            return {"members": [{"member_openid": "u", "username": "Known"}], "next_cursor": ""}
    view = Profiles(identity, store, Reads(), continuity_check=lambda: connection[0] if identity.intents & (1 << 24) else None)
    other_view = Profiles(other, store, Reads(), continuity_check=lambda: "other-session")
    try:
        first = await view.refresh_roster("g", with_rows=True)
        assert first["complete"] and [row["member_openid"] for row in first["rows"]] == ["u"]
        assert first["continuous"] is online_at_start
        assert (view.cached_roster("g") is not None) is online_at_start
        assert other_view.cached_roster("g") is not None
        assert requests == [("g", "", "new-session" if online_at_start else None)]
        if not online_at_start:
            next_roster = await view.refresh_roster("g")
            assert next_roster["complete"] and next_roster["continuous"]
            assert view.cached_roster("g") is not None
            assert requests[-1] == ("g", "", "new-session") and len(requests) == 2
    finally:
        store.db.execute("PRAGMA query_only=OFF")
        await view.close()
        await other_view.close()
        store.close()


async def test_failed_roster_commit_cannot_resurrect_previous_continuity(tmp_path, monkeypatch):
    identity = InstanceKey("p", RobotKey("a"), intents=1 << 24)
    store = ProfileStore(tmp_path / "profiles.sqlite3", clock=lambda: 10000.0)
    store.set_continuity(identity.robot, "group", "g", True)
    assert store.record_roster(identity.robot, "group", "g", [{"member_openid": "u", "username": "Old"}],
                               started_revision=store.revision(identity.robot, "group", "g"), started_at=9999)
    connection = [None]
    class Reads:
        async def get_group_member_list(self, group, cursor):
            assert (group, cursor) == ("g", "")
            connection[0] = "new-session"
            return {"members": [{"member_openid": "u", "username": "New"}], "next_cursor": ""}
    view = Profiles(identity, store, Reads(), continuity_check=lambda: connection[0])
    def failed_commit(*args, **kwargs):
        raise sqlite3.OperationalError("fixture disk full")
    monkeypatch.setattr(store, "record_roster", failed_commit)
    try:
        with pytest.raises(sqlite3.Error):
            await view.refresh_roster("g")
        assert store.roster_status(identity.robot, "group", "g")["continuous"] is False
        assert view.get_roster_status("g")["continuous"] is False
        assert view.cached_roster("g") is None
    finally:
        await view.close()
        store.close()


async def test_profile_status_never_masks_closed_or_unreadable_sqlite(tmp_path):
    store = ProfileStore(tmp_path / "profiles.sqlite3")
    view = Profiles(InstanceKey("p", RobotKey("app")), store, object(), continuity_check=lambda: "ready")
    store.db.close()
    try:
        with pytest.raises(sqlite3.Error):
            view.get_roster_status("g")
        store.close()
        with pytest.raises(V2Error) as closed:
            view.get_roster_status("g")
        assert closed.value.code == "service_stopped"
    finally:
        await view.close()
        store.close()


async def test_refresh_coalesces_and_late_event_wins(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []
    class Reads:
        http = SimpleNamespace(check=lambda: None)
        async def get_group_member_info(self, group, member):
            calls.append((group, member))
            started.set()
            await finish.wait()
            return {"member_openid": member, "username": "Official", "member_role": "admin"}
    service = Profiles(identity, profiles, Reads())
    first = asyncio.create_task(service.get_member("g", "001", mode="refresh"))
    await started.wait()
    second = asyncio.create_task(service.get_member("g", "001", mode="refresh"))
    await asyncio.sleep(0)
    profiles.member_event(identity.robot, "group", "g", "001", "left", int(time.time()) + 1)
    finish.set()
    one, two = await asyncio.gather(first, second)
    assert len(calls) == 1 and one["membership"] == two["membership"] == "left"
    assert one["fields"]["nickname"]["value"] == "Official"
    await service.close()

async def test_roster_absence_is_not_leave_evidence_and_revision_protects_snapshot(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    profiles.member_event(identity.robot, "group", "g", "001", "present", 100)
    class Reads:
        http = SimpleNamespace(check=lambda: None)
        async def get_group_member_list(self, group, cursor):
            if inject[0]:
                profiles.member_event(identity.robot, "group", "g", "002", "present", 101)
                inject[0] = False
            return {"members": [], "next_cursor": ""}
    service = Profiles(identity, profiles, Reads(), continuity_check=lambda: "same-ws-session")
    inject = [True]
    try:
        stale = await service.refresh_roster("g")
        assert stale["complete"] is False
        assert profiles.get_member(identity.robot, "group", "g", "002")["membership"] == "present"
        current = await service.refresh_roster("g")
        assert current["complete"] and current["continuous"] and current["count"] == 0
        assert profiles.get_member(identity.robot, "group", "g", "001")["membership"] == "present"
    finally:
        await service.close()

async def test_roster_refresh_coalesces_per_group_even_when_one_waiter_cancels(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Reads:
        async def get_group_member_list(self, group, cursor):
            calls.append((group, cursor))
            started.set()
            await release.wait()
            return {"members": [{"member_openid": "one", "username": "From QQ"}], "next_cursor": ""}
    service = Profiles(identity, profiles, Reads())
    first = asyncio.create_task(service.refresh_roster("g"))
    try:
        await started.wait()
        second = asyncio.create_task(service.refresh_roster("g"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        assert result["complete"] and calls == [("g", "")]
        assert profiles.get_member(identity.robot, "group", "g", "one")["fields"]["nickname"]["value"] == "From QQ"
    finally:
        release.set()
        await service.close()

async def test_roster_refreshes_are_isolated_by_group_and_robot(profiles):
    first_robot = InstanceKey("p1", RobotKey("app"))
    second_robot = InstanceKey("p2", RobotKey("another-app"))
    started, release = asyncio.Event(), asyncio.Event()
    calls = []
    class Reads:
        def __init__(self, label):
            self.label = label
        async def get_group_member_list(self, group, cursor):
            calls.append((self.label, group, cursor))
            if len(calls) == 3:
                started.set()
            await release.wait()
            return {"members": [{"member_openid": "shared", "username": self.label + group}], "next_cursor": ""}
    service_a = Profiles(first_robot, profiles, Reads("a"), continuity_check=lambda: "ws:a")
    service_b = Profiles(second_robot, profiles, Reads("b"), continuity_check=lambda: "ws:b")
    jobs = [asyncio.create_task(service_a.refresh_roster("g")),
            asyncio.create_task(service_a.refresh_roster("h")),
            asyncio.create_task(service_b.refresh_roster("g"))]
    try:
        await asyncio.wait_for(started.wait(), 2)
        release.set()
        pages = await asyncio.gather(*jobs)
        assert len(calls) == 3 and all(page["complete"] for page in pages)
        assert profiles.get_member(first_robot.robot, "group", "g", "shared")["fields"]["nickname"]["value"] == "ag"
        assert profiles.get_member(first_robot.robot, "group", "h", "shared")["fields"]["nickname"]["value"] == "ah"
        assert profiles.get_member(second_robot.robot, "group", "g", "shared")["fields"]["nickname"]["value"] == "bg"
    finally:
        release.set()
        await asyncio.gather(*jobs, return_exceptions=True)
        await service_a.close()
        await service_b.close()


async def test_close_cancels_roster_and_new_view_can_refresh(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    started, cancelled = asyncio.Event(), asyncio.Event()
    calls = []
    class Reads:
        async def get_group_member_list(self, group, cursor):
            calls.append((group, cursor))
            if len(calls) == 1:
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return {"members": [{"member_openid": "fresh", "username": "new view"}], "next_cursor": ""}
    reads = Reads()
    service = Profiles(identity, profiles, reads)
    unfinished = asyncio.create_task(service.refresh_roster("g"))
    await asyncio.wait_for(started.wait(), 2)
    await service.close()
    with pytest.raises(asyncio.CancelledError):
        await unfinished
    assert cancelled.is_set() and not service.roster_pending
    with pytest.raises(V2Error, match="closed"):
        await service.refresh_roster("g")
    newer = Profiles(identity, profiles, reads)
    try:
        result = await newer.refresh_roster("g")
        assert len(calls) == 2 and result["count"] == 1
    finally:
        await newer.close()


async def test_failed_roster_future_is_not_reused_as_a_cached_success(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    calls = []
    class Reads:
        async def get_group_member_list(self, group, cursor):
            calls.append((group, cursor))
            if len(calls) == 1:
                raise V2Error("qq_rate_limited", "Temporary read rejection", status=429, phase="rejected")
            return {"members": [{"member_openid": "observed", "username": "next read"}], "next_cursor": ""}
    service = Profiles(identity, profiles, Reads())
    try:
        with pytest.raises(V2Error) as first:
            await service.refresh_roster("g")
        assert first.value.code == "qq_rate_limited"
        with pytest.raises(V2Error) as missing:
            profiles.get_member(identity.robot, "group", "g", "observed")
        assert missing.value.code == "identity_not_observed"
        fresh = await service.refresh_roster("g")
        assert len(calls) == 2 and fresh["count"] == 1
        assert profiles.get_member(identity.robot, "group", "g", "observed")["fields"]["nickname"]["value"] == "next read"
    finally:
        await service.close()




async def test_roster_populates_present_members_without_elevating_cached_role(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    class Reads:
        http = SimpleNamespace(check=lambda: None)
        async def get_group_member_list(self, group, cursor):
            return {"members": [{"member_openid": "001", "username": "Name", "member_role": "admin",
                                "bot": False, "joined_at": "2025-01-01T00:00:00Z"}], "next_cursor": ""}
    service = Profiles(identity, profiles, Reads(), continuity_check=lambda: "session-1")
    try:
        roster = await service.refresh_roster("g")
        assert roster["complete"] and roster["continuous"] and roster["count"] == 1
        member = await service.get_member("g", "001", mode="cache_only")
        assert member["fields"]["nickname"]["value"] == "Name"
        assert member["fields"]["last_known_role"]["value"] == "admin"
        assert member["membership"] == "present" and "role" not in member
    finally:
        await service.close()


async def test_permission_failure_cools_ordinary_lookup_without_false_leave(profiles):
    identity = InstanceKey("p", RobotKey("app"))
    calls = []
    class Reads:
        http = SimpleNamespace(check=lambda: None)
        async def get_group_member_info(self, group, member):
            calls.append((group, member))
            raise V2Error("qq_api_error", "fixture denial", business_code=11253, phase="rejected")
    service = Profiles(identity, profiles, Reads())
    try:
        with pytest.raises(V2Error) as first:
            await service.get_member("g", "001", mode="refresh")
        assert first.value.business_code == 11253
        profiles.merge(identity.robot, "group", "g", "001", {"bot": False}, source="history", as_of=1)
        partial = await service.get_member("g", "001", mode="prefer_cache")
        assert partial["membership"] == "unknown" and partial["refresh_error"] == "qq_api_error"
        assert partial["missing_fields"] == ["nickname"] and len(calls) == 1
    finally:
        await service.close()



async def test_native_member_page_preserves_url_and_fields():
    identity = InstanceKey("p", RobotKey("app"))
    requests = []
    class FakeHTTP:
        async def request(self, spec):
            requests.append(spec)
            return SimpleNamespace(data={"members": [], "next_cursor": "", "extra": True})
    reads = GroupReads(identity, FakeHTTP(), RequestSpec)
    page = await reads.get_group_member_list("g/01", "")
    assert page["extra"] is True
    assert requests[0].url == "https://api.bot.qq.com/v2/groups/g%2F01/members?cursor="
    assert requests[0].method == "GET"


async def test_native_member_reads_use_real_transport_qq_origin(management):
    m = management
    reads = GroupReads(m.service.identity, m.http, RequestSpec)
    member = await reads.get_group_member_info("g", "u")
    assert member["member_openid"] == "u"
    assert m.http.session.calls[-1][1] == "https://api.bot.qq.com/v2/groups/g/members/u"
    assert m.calls[-1][0] == "GET"
    page = await reads.get_group_member_list("g", "")
    assert page["next_cursor"] == "opaque &+"
    assert m.http.session.calls[-1][1] == "https://api.bot.qq.com/v2/groups/g/members?cursor="
    assert m.calls[-1][0] == "GET"
    await m.service.group_info("g")
    assert m.http.session.calls[-1][1] == "https://api.bot.qq.com/v2/groups/g/info"
    assert m.calls[-1][0] == "GET"
