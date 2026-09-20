"""Generic IDs cannot fill missing scene-specific identities."""
import pytest
from astrbot.core.message.components import At, Reply
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW, chat_payload

from v2.messaging.convert import convert_chat
from v2.protocol import RawEnvelope


@pytest.mark.parametrize("event,field", [("GROUP_AT_MESSAGE_CREATE", "member_openid"), ("C2C_MESSAGE_CREATE", "user_openid")])
@pytest.mark.parametrize("missing", ["absent", "null", "empty"])
async def test_missing_scene_openid_is_quarantined_without_identity_or_delivery(receiver, event, field, missing):
    owner, instance = receiver
    key = instance.identity.settings_key
    payload = chat_payload(event, message_id="bad-author")
    payload["d"]["author"] = {"id": "generic-only"}
    if missing != "absent":
        payload["d"]["author"][field] = None if missing == "null" else ""
    assert owner.inbox.accept(key, RawEnvelope(payload, NOW))
    assert instance.consumer.step()
    assert instance._event_queue.empty()
    assert owner.messages.db.execute("SELECT count(*) FROM identities").fetchone()[0] == 0
    assert owner.messages.db.execute("SELECT count(*) FROM targets").fetchone()[0] == 0
    retained = owner.inbox.retained(key)
    assert len(retained) == 1 and retained[0]["state"] == "invalid"
    assert retained[0]["payload"] == payload
    good = chat_payload(event, message_id="valid-author")
    assert owner.inbox.accept(key, RawEnvelope(good, NOW))
    assert instance.consumer.step()
    delivered = instance._event_queue.get_nowait()
    assert delivered.message_obj.sender.user_id == "user-one"
    delivered.cleanup_temporary_local_files()


@pytest.mark.parametrize("event,field,kind", [
    ("GROUP_AT_MESSAGE_CREATE", "member_openid", "member_openid"),
    ("C2C_MESSAGE_CREATE", "user_openid", "user_openid"),
])
@pytest.mark.parametrize("typed_actors", [False, True])
async def test_optional_actors_never_borrow_generic_ids(receiver, event, field, kind, typed_actors):
    owner, instance = receiver
    payload = chat_payload(event)
    payload["d"]["author"]["id"] = "generic-sender"
    mention, quoted = {"id": "generic-mention"}, {"id": "generic-quote"}
    if typed_actors:
        mention[field], quoted[field] = "typed-mention", "typed-quote"
    payload["d"]["mentions"] = [mention]
    payload["d"]["msg_elements"] = [{"message_type": 103, "msg_idx": "REFIDX_quoted", "author": quoted, "content": "real quote"}]
    payload["d"]["message_scene"]["ext"].append("ref_msg_idx=REFIDX_quoted")
    chat = convert_chat(instance.identity, RawEnvelope(payload, NOW))
    owner.messages.observe(chat)
    expected = {"user-one", "typed-mention", "typed-quote"} if typed_actors else {"user-one"}
    assert {record["user_id"] for record in chat.observations} == expected
    assert [part.qq for part in chat.message.message if isinstance(part, At)] == (["typed-mention"] if typed_actors else [])
    reply = next(part for part in chat.message.message if isinstance(part, Reply))
    assert reply.sender_id == ("typed-quote" if typed_actors else None)
    rows = owner.messages.db.execute("SELECT subject,kind,scope FROM identities").fetchall()
    assert {tuple(row) for row in rows} == {(user, kind, f"{chat.route.scene}:{chat.route.target}") for user in expected}


@pytest.mark.parametrize("event", ["AT_MESSAGE_CREATE", "MESSAGE_CREATE", "DIRECT_MESSAGE_CREATE"])
async def test_channel_scenes_still_observe_the_documented_id(receiver, event):
    owner, instance = receiver
    payload = chat_payload(event)
    payload["d"]["author"].update(member_openid="not-channel-id", user_openid="not-channel-id")
    payload["d"]["mentions"] = [{"id": "channel-mention", "member_openid": "wrong-mention"}]
    chat = convert_chat(instance.identity, RawEnvelope(payload, NOW))
    owner.messages.observe(chat)
    assert chat.message.sender.user_id == "user-one"
    assert {record["user_id"] for record in chat.observations} == {"user-one", "channel-mention"}
    assert {record["id_kind"] for record in chat.observations} == {"channel_user_id"}
