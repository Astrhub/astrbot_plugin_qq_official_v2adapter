"""2026-11-16 user info, member nickname, union group id, and OneBot translation."""
from types import SimpleNamespace

import pytest
from test_messaging_state import NOW, chat_payload
from test_sdk_event_shapes import make_event

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.models import InstanceKey, RobotKey
from v2.network_events import LiveEvents
from v2.onebot_profiles import OneBotProfiles, official_member
from v2.protocol import RawEnvelope
from v2.sdk.api.reads import native_user_info
from v2.sdk.event_types import invalid_fields


def identity():
    return InstanceKey("p", RobotKey("app"))


def test_native_user_info_requires_official_fields_and_keeps_extras():
    data = {"username": "笑", "avatar": "https://thirdqq.example/a.png", "allow_proactive_msg": False,
            "union_openid": "UNION", "official_extra": {"k": 1}}
    assert native_user_info(data) is data
    missing = {"username": "x", "allow_proactive_msg": False}
    with pytest.raises(V2Error) as exc:
        native_user_info(missing)
    assert exc.value.code == "invalid_user_info"
    with pytest.raises(V2Error) as typed:
        native_user_info({**data, "union_openid": 7})
    assert typed.value.code == "invalid_user_info"
    with pytest.raises(V2Error):
        native_user_info({**data, "allow_proactive_msg": 1})
    kept = native_user_info({"username": "", "avatar": "", "allow_proactive_msg": True})
    assert "union_openid" not in kept


async def test_get_user_info_uses_official_path_and_rejects_bad_union():
    key = identity()
    client = V2Client(key, state=ClientState(key))
    calls = []

    async def request(spec, *, before_send=None):
        if before_send:
            before_send()
        calls.append((spec.method, spec.path))
        return SimpleNamespace(data={"username": "N", "avatar": "https://a.test/x", "allow_proactive_msg": True,
                                     "union_openid": "U", "keep": True})

    client._state.http = SimpleNamespace(request=request)
    row = await client.qq.get_user_info("user/1")
    assert calls == [("GET", "/v2/users/user%2F1/info")]
    assert row["keep"] is True and row["union_openid"] == "U"

    async def bad(spec, *, before_send=None):
        if before_send:
            before_send()
        return SimpleNamespace(data={"username": "N", "avatar": "https://a.test/x",
                                     "allow_proactive_msg": True, "union_openid": ["no"]})

    client._state.http = SimpleNamespace(request=bad)
    with pytest.raises(V2Error) as exc:
        await client.qq.get_user_info("user-one")
    assert exc.value.code == "invalid_user_info"


def test_group_and_c2c_author_fields_enter_observation_without_rerouting():
    group = chat_payload("GROUP_MESSAGE_CREATE", sender="member-one", target="group-one")
    group["d"]["author"]["username"] = "QQ名"
    group["d"]["author"]["member_nickname"] = "群名片"
    group["d"]["author"]["avatar"] = "https://thirdqq.example/a.png"
    group["d"]["author"]["union_openid"] = "UNION-USER"
    group["d"]["union_group_openid"] = "UNION-GROUP"
    chat = convert_chat(identity(), RawEnvelope(group, NOW))
    assert chat.route.target == "group-one"
    assert chat.message.group_id == "group-one"
    assert chat.message.sender.nickname == "群名片"
    observed = chat.observations[0]
    assert observed["nickname"] == "群名片"
    assert observed["avatar_url"] == "https://thirdqq.example/a.png"
    assert observed["union_openid"] == "UNION-USER"

    c2c = chat_payload("C2C_MESSAGE_CREATE", sender="user-one", target="user-one")
    c2c["d"]["author"]["username"] = "单聊名"
    c2c["d"]["author"]["member_nickname"] = "应忽略"
    c2c["d"]["author"]["avatar"] = "https://thirdqq.example/b.png"
    private = convert_chat(identity(), RawEnvelope(c2c, NOW))
    assert private.message.sender.nickname == "单聊名"
    assert private.observations[0]["nickname"] == "单聊名"
    assert private.route.target == "user-one"


def test_optional_union_group_and_member_nickname_shapes():
    body = {"id": "m", "author": {"member_openid": "u", "member_nickname": "群名片", "avatar": "https://a.test/x",
                                  "union_openid": "UNION"},
            "group_openid": "g", "union_group_openid": "UG", "timestamp": "2026-01-01T00:00:00Z"}
    notice = make_event("GROUP_MESSAGE_CREATE", body)
    assert notice.schema_valid and notice.d["union_group_openid"] == "UG"
    assert notice.d["author"]["member_nickname"] == "群名片"
    bad = make_event("GROUP_ADD_ROBOT", {"timestamp": 1, "group_openid": "g", "op_member_openid": "admin",
                                         "union_group_openid": 9})
    assert invalid_fields("GROUP_ADD_ROBOT", bad.raw()["d"]) == ("union_group_openid",)
    member = make_event("GROUP_MEMBER_ADD", {"timestamp": 1, "group_openid": "g", "member_openid": "u",
                                            "union_group_openid": "UG"})
    assert member.schema_valid


def test_onebot_member_projection_carries_union_without_numeric_qq():
    row = official_member("g", {"member_openid": "u", "username": "N", "member_role": "admin", "bot": False,
                               "union_openid": "UNION", "avatar": "https://a.test/x"})
    assert row["user_id"] == "u" and row["nickname"] == "N"
    assert row["_qq"]["union_openid"] == "UNION" and row["_qq"]["avatar"] == "https://a.test/x"
    assert "operator_id" not in row and type(row["user_id"]) is str


async def test_stranger_live_c2c_and_group_use_official_reads():
    key = identity()
    client = V2Client(key, state=ClientState(key))
    calls = []

    async def request(spec, *, before_send=None):
        if before_send:
            before_send()
        calls.append(spec.path)
        if spec.path.endswith("/info"):
            return SimpleNamespace(data={"username": "C2C", "avatar": "https://a.test/c.png",
                                         "allow_proactive_msg": True, "union_openid": "U1"})
        return SimpleNamespace(data={"member_openid": "001", "username": "群员", "member_role": "member",
                                     "bot": False, "union_openid": "U2"})

    client._state.http = SimpleNamespace(request=request)

    class Reads:
        async def get_group_member_info(self, group, member, *, guard=None):
            if guard:
                guard()
            return {"member_openid": member, "username": "群员", "member_role": "member", "bot": False,
                    "union_openid": "U2"}

    client._state.reads = Reads()
    compat = OneBotProfiles(client)
    c2c = await compat.stranger_live("peer", "user_openid", "c2c:peer")
    assert c2c["nickname"] == "C2C" and c2c["_qq"]["avatar"] == "https://a.test/c.png"
    assert c2c["_qq"]["union_openid"] == "U1" and c2c["_qq"]["allow_proactive_msg"] is True
    assert c2c["source"] == "official_query"
    with pytest.raises(V2Error) as mismatch:
        await compat.stranger_live("other", "user_openid", "c2c:peer")
    assert mismatch.value.code == "identity_mismatch"
    group = await compat.stranger_live("001", "member_openid", "group:g")
    assert group["nickname"] == "群员" and group["_qq"]["union_openid"] == "U2"
    assert group["scope"] == "group:g" and group["source"] == "official_query"


def test_network_message_and_notice_only_emit_proven_fields():
    payload = chat_payload("GROUP_MESSAGE_CREATE", sender="member-one", target="group-one")
    payload["d"]["author"]["member_nickname"] = "群名片"
    payload["d"]["author"]["avatar"] = "https://thirdqq.example/a.png"
    payload["d"]["author"]["union_openid"] = "UNION-USER"
    payload["d"]["union_group_openid"] = "UNION-GROUP"
    chat = convert_chat(identity(), RawEnvelope(payload, NOW))
    published = []
    events = LiveEvents(SimpleNamespace(
        observed=lambda: True,
        adapter=SimpleNamespace(
            identity=SimpleNamespace(platform_id="p", generation="gen"),
            bot_id="bot",
            owner=SimpleNamespace(messages=SimpleNamespace(now=lambda: NOW)),
        ),
        publish=published.append,
    ))
    events.chat(chat)
    frame = published[0]
    assert frame["sender"]["nickname"] == "群名片"
    assert frame["_qq"]["avatar"] == "https://thirdqq.example/a.png"
    assert frame["_qq"]["union_openid"] == "UNION-USER"
    assert frame["_qq"]["union_group_openid"] == "UNION-GROUP"
    assert frame["group_id"] == "group-one"
    assert "operator_id" not in frame and "notice_type" not in frame

    published.clear()
    events.notice("GROUP_MEMBER_ADD", {"timestamp": 11, "group_openid": "g", "member_openid": "u",
                                       "union_group_openid": "UG"})
    notice = published[0]
    assert notice["qq_type"] == "notice" and notice["group_id"] == "g" and notice["user_id"] == "u"
    assert notice["time"] == 11 and notice["_qq"]["union_group_openid"] == "UG"
    assert "operator_id" not in notice and "sub_type" not in notice and "notice_type" not in notice

    published.clear()
    events.notice("GROUP_ADD_ROBOT", {"timestamp": 12, "group_openid": "g", "op_member_openid": "admin"})
    added = published[0]
    assert added["group_id"] == "g" and "user_id" not in added
    assert added["_qq"]["op_member_openid"] == "admin"
    assert "operator_id" not in added
