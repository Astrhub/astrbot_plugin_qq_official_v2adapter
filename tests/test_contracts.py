import asyncio
import json

import pytest

from v2.client import V2Client
from v2.errors import V2Error
from v2.models import InstanceKey, RobotKey, SessionRoute
from v2.protocol import (
    AcceptedEvents,
    ExpiringSet,
    IdentityCache,
    RawEnvelope,
    RequestSpec,
    avatar_url,
    decode_response,
)


def chat(*, event="GROUP_AT_MESSAGE_CREATE", uid="member", msg="message", target="group"):
    return RawEnvelope.parse(json.dumps({"op": 0, "s": 9, "t": event, "id": "outer-event",
        "d": {"id": msg, "group_openid": target, "author": {"member_openid": uid, "user_openid": uid, "id": uid},
              "content": "not an identity: 123456", "ref_idx": "reference"}}).encode(), now=100)


def test_original_envelope_and_accepted_sequence():
    envelope = chat()
    assert envelope.event_id == "outer-event" and envelope.message_id == "message"
    assert envelope.payload["d"]["ref_idx"] == "reference"
    queue = asyncio.Queue(maxsize=1)
    queue.put_nowait("busy")
    accepted = AcceptedEvents()
    with pytest.raises(asyncio.QueueFull):
        accepted.accept(envelope, queue.put_nowait)
    assert accepted.last_sequence is None and not accepted.seen.contains("outer-event")
    queue.get_nowait()
    assert accepted.accept(envelope, queue.put_nowait)
    assert accepted.last_sequence == 9 and not accepted.accept(envelope, queue.put_nowait)
    interaction = RawEnvelope.parse(b'{"op":0,"t":"INTERACTION_CREATE","id":"event","d":{"id":"interaction"}}')
    assert interaction.message_id is None
    assert interaction.payload["d"]["id"] == "interaction"
    unknown = RawEnvelope.parse(b'{"op":0,"t":"FUTURE","id":"outer","d":{"extra":[1,2]}}')
    assert unknown.payload["d"]["extra"] == [1, 2]


@pytest.mark.parametrize("raw", [b"[]", b"{}", b"null", b'{"op":true}', b"broken"])
def test_invalid_envelopes(raw):
    with pytest.raises(V2Error):
        RawEnvelope.parse(raw)


def test_dedup_ttl_independent_of_capacity():
    now = [0]
    seen = ExpiringSet(capacity=10, ttl=5, clock=lambda: now[0])
    seen.add("id")
    now[0] = 5
    assert not seen.contains("id")
    for i in range(100):
        seen.add(i)
    assert len(seen.items) == 10


@pytest.mark.parametrize("scene", ["c2c", "group", "channel", "dm"])
def test_session_roundtrip(scene):
    route = SessionRoute(RobotKey("app:/中文", "sandbox"), scene, "target:!/%", "user:!")
    assert SessionRoute.decode(route.encode()) == route


@pytest.mark.parametrize("value", ["old:group", "v2.", "v2.!", "v2.W10", None])
def test_session_invalid(value):
    with pytest.raises(V2Error):
        SessionRoute.decode(value)


@pytest.mark.parametrize("size", [0, 100, 140, 640])
def test_avatar_no_identity_side_effect(size):
    cache = IdentityCache()
    url = avatar_url(RobotKey("app/a"), "user/?中文", size)
    assert url == f"https://q.qlogo.cn/qqapp/app%2Fa/user%2F%3F%E4%B8%AD%E6%96%87/{size}"
    assert not cache.items


@pytest.mark.parametrize("size", [True, -1, 128, "100", None])
def test_avatar_invalid(size):
    with pytest.raises(V2Error):
        avatar_url(RobotKey("app"), "user", size)


def test_identity_isolation_expiry_eviction():
    now = [100]
    cache = IdentityCache(capacity=2, ttl=5, clock=lambda: now[0])
    app = RobotKey("a")
    cache.observe_chat(app, chat())
    for robot, kind, scope, uid in [(RobotKey("b"), "member_openid", "group:group", "member"),
                                   (RobotKey("a", "sandbox"), "member_openid", "group:group", "member"),
                                   (app, "user_openid", "group:group", "member"),
                                   (app, "member_openid", "group:another", "member")]:
        with pytest.raises(V2Error, match="No available") as exc:
            cache.lookup(robot, kind, scope, uid)
        assert exc.value.code == "identity_not_observed"
    assert cache.lookup(app, "member_openid", "group:group", "member")["source_message_id"] == "message"
    now[0] += 5
    with pytest.raises(V2Error):
        cache.lookup(app, "member_openid", "group:group", "member")
    for uid in ("a", "b", "c"):
        cache.observe_chat(app, chat(uid=uid))
    assert len(cache.items) == 2
    with pytest.raises(V2Error):
        cache.lookup(app, "member_openid", "group:group", "a")
    with pytest.raises(V2Error):
        cache.observe_chat(app, chat(event="INTERACTION_CREATE"))
    projected = chat()
    projected.payload["derived_from_interaction"] = True
    with pytest.raises(V2Error):
        cache.observe_chat(app, projected)


def test_http_contracts_no_global_base_and_structured_errors():
    prod = RequestSpec("production", "GET", "/v2/items", {"cursor": "a+b", "ids": ["a", "b"]})
    sandbox = RequestSpec("sandbox", "GET", "/v2/items")
    assert prod.url.endswith("?cursor=a%2Bb&ids=a&ids=b")
    assert prod.url.startswith("https://api.") and sandbox.url.startswith("https://sandbox.")
    assert decode_response(204, b"", {}) is None
    assert decode_response(200, b"[1,2]", {}) == [1, 2]
    for code in (40093001, 40093002):
        with pytest.raises(V2Error) as exc:
            decode_response(400, json.dumps({"code": code, "message": "sensitive URL"}).encode(), {"X-Tps-Trace-Id": "trace", "Retry-After": "3"})
        assert exc.value.business_code == code and exc.value.trace_id == "trace"
        assert exc.value.retry_after == "3" and "sensitive" not in str(exc.value)
    with pytest.raises(V2Error) as exc:
        decode_response(502, b"<html>upstream</html>", {}, phase="result_unknown")
    assert exc.value.phase == "result_unknown"


async def test_one_client_entrypoints_no_fake_success(config):
    client = V2Client(InstanceKey.from_config(config))
    assert client.api is client
    assert (await client.get_status())["online"] is False
    assert (await client.can_send_image())["yes"] is False
    for action in [lambda: client.api.call_action("send_group_msg", group_id="g", message="x"),
                   lambda: client.call_action("send_group_msg", group_id="g", message="x"),
                   lambda: client.send_group_msg(group_id="g", message="x")]:
        with pytest.raises(V2Error) as exc:
            await action()
        assert exc.value.code == "transport_not_ready" and exc.value.as_dict()["data"] is None
    with pytest.raises(V2Error) as exc:
        await client.call_action("does_not_exist")
    assert exc.value.retcode == 1404
    with pytest.raises(V2Error):
        await client.get_stranger_info(user_id="user", no_cache=True)
    with pytest.raises(V2Error) as exc:
        await client.get_stranger_info(user_id="user", id_kind="user_openid", scope="c2c:user")
    assert exc.value.code == "identity_not_observed"
    bound = client.bind(SessionRoute(client.identity.robot, "c2c", "user"))
    await client.close()
    with pytest.raises(V2Error) as exc:
        await bound.get_status()
    assert exc.value.code == "stale_generation"
    with pytest.raises(V2Error):
        bound.qq.avatar_url("user")
    replacement = V2Client(InstanceKey.from_config(config))
    assert replacement.identity.generation != client.identity.generation
    assert not (await replacement.get_status())["online"]


@pytest.mark.parametrize("field,value", [("environment", "custom"), ("shard", [1, 1]), ("shard", [True, 1]), ("intents", -1), ("id", "a:b")])
def test_invalid_platform_config(config, field, value):
    config[field] = value
    with pytest.raises(V2Error):
        InstanceKey.from_config(config)
