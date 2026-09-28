"""OneBot current roster, historical member and scoped stranger projections."""

from types import SimpleNamespace

import pytest

from v2.client import (
    ACTION_PARAMS,
    REMOTE_ACTIONS,
    WRITE_ACTIONS,
    ClientState,
    V2Client,
)
from v2.errors import V2Error
from v2.messaging.store import IdentityView, MessageStore, robot_key
from v2.models import InstanceKey, RobotKey
from v2.profiles.service import Profiles
from v2.profiles.store import ProfileStore


@pytest.fixture
async def history(tmp_path):
    now = [10000.0]
    identity = InstanceKey("p", RobotKey("a"))
    profiles = ProfileStore(tmp_path / "profiles.sqlite3", clock=lambda: now[0])
    messages = MessageStore(tmp_path / "messages.sqlite3", clock=lambda: now[0])
    calls = []
    rows = [[{"member_openid": "001", "username": "One", "member_role": "admin", "bot": False,
              "joined_at": "2026-09-28T10:00:00+08:00"}]]
    class Reads:
        async def get_group_member_info(self, group, member, *, guard=None):
            if guard is not None:
                guard()
            calls.append(("member", group, member))
            return {"member_openid": member, "username": "From QQ", "member_role": "member", "bot": False}
        async def get_group_member_list(self, group, cursor, *, guard=None):
            if guard is not None:
                guard()
            calls.append(("page", group, cursor))
            return {"members": rows[0], "next_cursor": ""}
    service = Profiles(identity, profiles, Reads(), continuity_check=lambda: "ws-session")
    state = ClientState(identity)
    state.profiles, state.reads, state.cache = service, Reads(), IdentityView(messages, identity.robot)
    client = V2Client(identity, state=state)
    try:
        yield SimpleNamespace(client=client, service=service, profiles=profiles, messages=messages,
                              calls=calls, rows=rows, now=now, identity=identity)
    finally:
        await service.close()
        profiles.close()
        messages.close()


async def test_onebot_member_cache_refresh_and_confirmed_empty_roster(history):
    h = history
    h.profiles.merge(h.identity.robot, "group", "g", "001", {"nickname": "Cached", "last_known_role": "admin", "bot": False},
                     source="member_page", as_of=9999)
    h.profiles.member_event(h.identity.robot, "group", "g", "001", "present", 9999)
    cached = await h.client.call_action("get_group_member_info", group_id="g", user_id="001")
    assert cached["nickname"] == "Cached" and cached["role"] == "admin" and cached["_qq"]["source"] == "member_page"
    assert not h.calls
    fresh = await h.client.call_action("get_group_member_info", group_id="g", user_id="001", no_cache=True)
    assert fresh["nickname"] == "From QQ" and fresh["_qq"]["source"] == "official_query"
    assert h.calls == [("member", "g", "001")]
    first = await h.client.call_action("get_group_member_list", group_id="g", no_cache=True)
    assert [m["user_id"] for m in first] == ["001"]
    assert first[0]["join_time"] == 1790560800
    before = len(h.calls)
    second = await h.client.call_action("get_group_member_list", group_id="g")
    assert [m["user_id"] for m in second] == ["001"] and len(h.calls) == before
    h.rows[0] = []
    empty = await h.client.call_action("get_group_member_list", group_id="g", no_cache=True)
    assert empty == []
    assert await h.client.call_action("get_group_member_list", group_id="g") == []
    assert h.profiles.get_member(h.identity.robot, "group", "g", "001")["membership"] == "present"
    h.now[0] = 10002
    h.profiles.member_event(h.identity.robot, "group", "g", "001", "left", 10001)
    old = await h.client.call_action("get_group_member_info", group_id="g", user_id="001")
    assert old["nickname"] and "role" not in old and old["_qq"]["membership"] == "left"


async def test_onebot_role_freshness_is_field_scoped_and_roster_cache_isolated(history):
    h = history
    robot = h.identity.robot
    h.profiles.merge(robot, "group", "g", "001", {"last_known_role": "admin", "nickname": "Old"},
                     source="official_query", as_of=9000)
    h.profiles.set_continuity(robot, "group", "g", True)
    revision = h.profiles.revision(robot, "group", "g")
    assert h.profiles.record_roster(robot, "group", "g", [{"member_openid": "001", "username": "Fresh"}],
                                    started_revision=revision, started_at=9999)
    cached = await h.client.call_action("get_group_member_info", group_id="g", user_id="001")
    listed = await h.client.call_action("get_group_member_list", group_id="g")
    assert cached["nickname"] == listed[0]["nickname"] == "Fresh"
    assert "role" not in cached and "role" not in listed[0] and not h.calls
    assert cached["_qq"]["field_sources"]["last_known_role"]["as_of"] == 9000
    current = await h.client.call_action("get_group_member_info", group_id="g", user_id="001", no_cache=True)
    assert current["role"] == "member" and h.calls == [("member", "g", "001")]
    h.profiles.merge(robot, "group", "g", "002", {"last_known_role": "owner"},
                     source="legacy_chat", as_of=None)
    h.profiles.merge(robot, "group", "g", "002", {"nickname": "Known"},
                     source="chat", as_of=9999)
    h.profiles.member_event(robot, "group", "g", "002", "present", 9999)
    untimed = await h.client.call_action("get_group_member_info", group_id="g", user_id="002")
    assert untimed["nickname"] == "Known" and "role" not in untimed
    h.profiles.merge(robot, "group", "g", "001", {"last_known_role": "member"},
                     source="official_query", as_of=10000)
    h.profiles.merge(robot, "group", "g", "001", {"last_known_role": "admin"},
                     source="chat_history", as_of=9001)
    assert (await h.client.call_action("get_group_member_info", group_id="g", user_id="001"))["role"] == "member"
    h.profiles.merge(robot, "group", "other", "001", {"nickname": "Other", "last_known_role": "owner"},
                     source="official_query", as_of=10000)
    h.profiles.member_event(robot, "group", "other", "001", "present", 10000)
    assert (await h.client.call_action("get_group_member_info", group_id="other", user_id="001"))["role"] == "owner"
    h.profiles.merge(RobotKey("another"), "group", "g", "001", {"nickname": "Alien", "last_known_role": "admin"},
                     source="official_query", as_of=10000)
    assert (await h.client.call_action("get_group_member_info", group_id="g", user_id="001"))["nickname"] == "From QQ"
    h.now[0] = 10002
    h.profiles.member_event(robot, "group", "g", "001", "left", 10001)
    h.profiles.member_event(robot, "group", "g", "002", "left", 9999)
    assert "role" not in await h.client.call_action("get_group_member_info", group_id="g", user_id="001")
    assert "role" not in await h.client.call_action("get_group_member_info", group_id="g", user_id="002")
    assert h.calls == [("member", "g", "001")]


async def test_stranger_chat_fields_survive_profile_overlay_and_historical_fallback(history):
    h = history
    key = robot_key(h.identity.robot)
    h.messages.db.execute("INSERT INTO identities VALUES(?,?,?,?,?,?,?,?)", (
        key, "member_openid", "group:g", "001", '{"user_id":"001","id_kind":"member_openid","scope":"group:g","nickname":"Chat"}',
        9990, 9991, "chat-message"))
    h.messages.db.commit()
    h.profiles.merge(h.identity.robot, "group", "g", "001", {"nickname": "Profile"},
                     source="official_query", as_of=9995)
    live = await h.client.call_action("get_stranger_info", user_id="001", id_kind="member_openid", scope="group:g")
    assert (live["source"], live["nickname"], live["source_message_id"], live["first_seen"], live["last_seen"]) == (
        "chat_cache", "Profile", "chat-message", 9990, 9991)
    assert live["_qq"]["profile_fields"]["nickname"]["source"] == "official_query"
    h.now[0] += 86401
    historical = await h.client.call_action("get_stranger_info", user_id="001", id_kind="member_openid", scope="group:g")
    assert historical["source"] == "profile_cache" and historical["nickname"] == "Profile"
    assert "source_message_id" not in historical and "first_seen" not in historical and "last_seen" not in historical
    assert historical["as_of"] == 9995 and historical["stale"] is True
    with pytest.raises(V2Error) as scope:
        await h.client.call_action("get_stranger_info", user_id="001", id_kind="member_openid", scope="group:other")
    assert scope.value.code == "identity_not_observed"
    with pytest.raises(V2Error) as unsupported:
        await h.client.call_action("get_stranger_info", user_id="001", id_kind="member_openid", scope="group:g", no_cache=True)
    assert unsupported.value.code == "unsupported"


async def test_onebot_new_actions_are_scoped_and_writes_classified(history):
    h = history
    assert {"set_group_kick_members", "_qq_set_group_blacklist"} <= WRITE_ACTIONS
    assert {"get_group_shut_list", "_qq_get_join_approval_strategies"} <= REMOTE_ACTIONS
    assert ACTION_PARAMS["get_group_member_list"]["no_cache"] == "bool"
    with pytest.raises(V2Error):
        await h.client.call_action("set_group_kick_members", group_id="g", user_ids=["001"])
