import json

import pytest
from test_messaging_state import NOW, chat_payload

from v2.client import V2Client
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.store import IdentityView, MessageStore
from v2.models import InstanceKey, RobotKey, SessionRoute
from v2.protocol import (
    ExpiringSet,
    RawEnvelope,
    RequestSpec,
    avatar_url,
    decode_response,
)
from v2.transport.inbox import Ingress, RawInbox


def chat(*, event="GROUP_AT_MESSAGE_CREATE", uid="member", msg="message", target="group"):
    return RawEnvelope.parse(json.dumps({"op": 0, "s": 9, "t": event, "id": "outer-event",
        "d": {"id": msg, "group_openid": target, "author": {"member_openid": uid, "user_openid": uid, "id": uid},
              "content": "not an identity: 123456", "ref_idx": "reference"}}).encode(), now=100)


async def test_original_envelope_and_durable_accepted_sequence(tmp_path):
    envelope = chat()
    assert envelope.event_id == "outer-event" and envelope.message_id == "message"
    assert envelope.payload["d"]["ref_idx"] == "reference"
    inbox = RawInbox(tmp_path / "inbox", max_rows=0)
    ingress = Ingress(inbox, "app")
    ingress.start()
    try:
        with pytest.raises(V2Error):
            await ingress.accept(envelope)
        assert ingress.last_sequence is None and inbox.count("app") == 0
        inbox.max_rows = 1
        assert await ingress.accept(envelope)
        assert ingress.last_sequence == 9 and not await ingress.accept(envelope)
        pending = inbox.pending("app")
        assert len(pending) == 1 and pending[0]["payload"] == envelope.payload
        inbox.acknowledge("another-app", pending[0]["receipt"])
        assert inbox.count("app") == 1
        inbox.acknowledge("app", pending[0]["receipt"])
        assert inbox.count("app") == 0 and not await ingress.accept(envelope)
    finally:
        await ingress.close()
        inbox.close()
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
def test_avatar_no_identity_side_effect(size, tmp_path):
    store = MessageStore(tmp_path / "identities")
    try:
        cache = IdentityView(store, RobotKey("app/a"))
        url = avatar_url(cache.robot, "user/?中文", size)
        assert url == f"https://q.qlogo.cn/qqapp/app%2Fa/user%2F%3F%E4%B8%AD%E6%96%87/{size}"
        assert not cache.items
    finally:
        store.close()


@pytest.mark.parametrize("size", [True, -1, 128, "100", None])
def test_avatar_invalid(size):
    with pytest.raises(V2Error):
        avatar_url(RobotKey("app"), "user", size)


def test_identity_isolation_expiry_eviction(config, tmp_path):
    now = [NOW]
    identity = InstanceKey.from_config(config)
    store = MessageStore(tmp_path / "identities", identity_capacity=2, clock=lambda: now[0])
    cache = IdentityView(store, identity.robot)
    def observe(**kwargs):
        store.observe(convert_chat(identity, RawEnvelope(chat_payload(**kwargs), now[0])))
    try:
        observe()
        app = identity.robot
        for robot, kind, scope in [(RobotKey("other"), "member_openid", "group:group-one"),
                                   (RobotKey(app.appid, "sandbox"), "member_openid", "group:group-one"),
                                   (app, "user_openid", "group:group-one"),
                                   (app, "member_openid", "group:another")]:
            with pytest.raises(V2Error) as exc:
                store.lookup(robot, kind, scope, "user-one")
            assert exc.value.code == "identity_not_observed"
        assert cache.lookup(app, "member_openid", "group:group-one", "user-one")["source_message_id"] == "msg-one"
        now[0] += 86400
        with pytest.raises(V2Error) as exc:
            cache.lookup(app, "member_openid", "group:group-one", "user-one")
        assert exc.value.code == "identity_not_observed"
        for uid in ("a", "b", "c"):
            observe(sender=uid, message_id=uid, timestamp=now[0])
        assert len(cache.items) == 2
        with pytest.raises(V2Error):
            cache.lookup(app, "member_openid", "group:group-one", "a")
        for projected in (False, True):
            payload = chat_payload()
            if projected:
                payload["derived_from_interaction"] = True
            else:
                payload["t"] = "INTERACTION_CREATE"
            with pytest.raises(V2Error) as exc:
                store.observe(convert_chat(identity, RawEnvelope(payload, now[0])))
            assert exc.value.code == "not_chat_source" and len(cache.items) == 2
    finally:
        store.close()


def test_http_contracts_no_global_base_and_structured_errors():
    prod = RequestSpec("production", "GET", "/v2/items", {"cursor": "a+b", "ids": ["a", "b"]})
    sandbox = RequestSpec("sandbox", "GET", "/v2/items")
    assert prod.url == "https://api.bot.qq.com/v2/items?cursor=a%2Bb&ids=a&ids=b"
    with pytest.raises(V2Error) as exc:
        _ = sandbox.url
    assert exc.value.code == "unsupported_environment" and exc.value.phase == "not_sent"
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
    assert exc.value.code == "transport_not_ready"
    assert client._state.cache is None and client.capabilities()["identity_cache"] == "not_ready"
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


async def test_client_uses_only_attached_durable_identity_view_and_close_keeps_history(config, tmp_path):
    identity = InstanceKey.from_config(config)
    store = MessageStore(tmp_path / "durable-identities", clock=lambda: NOW)
    client = V2Client(identity)
    route = SessionRoute(identity.robot, "group", "group-one")
    client._state.cache = IdentityView(store, identity.robot)
    bound = client.bind(route)
    try:
        assert client.capabilities()["identity_cache"].startswith("durable")
        with pytest.raises(V2Error) as exc:
            await bound.get_stranger_info(user_id="user-one")
        assert exc.value.code == "identity_not_observed" and not client._state.cache.items
        store.observe(convert_chat(identity, RawEnvelope(chat_payload(), NOW)))
        record = await bound.get_stranger_info(user_id="user-one")
        assert record["source"] == "chat_cache" and record["source_message_id"] == "msg-one"
        record["nickname"] = "not persisted"
        assert (await bound.get_stranger_info(user_id="user-one"))["nickname"] == "same-name"
        await client.close()
        with pytest.raises(V2Error) as exc:
            await bound.get_stranger_info(user_id="user-one")
        assert exc.value.code == "stale_generation"
        replacement = V2Client(InstanceKey.from_config(config))
        replacement._state.cache = IdentityView(store, replacement.identity.robot)
        assert (await replacement.bind(route).get_stranger_info(user_id="user-one"))["source_message_id"] == "msg-one"
        await replacement.close()
        assert len(client._state.cache.items) == 1
    finally:
        await client.close()
        store.close()


@pytest.mark.parametrize("field,value", [("environment", "custom"), ("shard", [1, 1]), ("shard", [True, 1]), ("intents", -1), ("id", "a:b")])
def test_invalid_platform_config(config, field, value):
    config[field] = value
    with pytest.raises(V2Error):
        InstanceKey.from_config(config)
