"""Host display names require a real robot-and-scene-matched observation."""

import copy

import pytest
from astrbot.core.message.components import At, Reply
from test_messaging_state import NOW, chat_payload

from v2.messaging.convert import convert_chat
from v2.models import InstanceKey, RobotKey
from v2.profiles.display import enrich_chat
from v2.profiles.store import ProfileStore
from v2.protocol import RawEnvelope


@pytest.mark.parametrize("sender,quoted", [("user-one", "quote-one"), ("000123", "000456")])
def test_scoped_sender_at_and_known_reply_names_do_not_change_original_payload(tmp_path, sender, quoted):
    identity = InstanceKey("p", RobotKey("appid"))
    payload = chat_payload("GROUP_MESSAGE_CREATE", sender=sender, target="g", timestamp=NOW)
    payload["d"]["author"].pop("username", None)
    chat = convert_chat(identity, RawEnvelope(payload, NOW))
    at = At(qq=quoted)
    at.qq = quoted
    reply = Reply(id="real-ref", sender_id=quoted, sender_nickname="")
    reply.sender_id = quoted  # Converted QQ references restore leading-zero OpenIDs after host construction.
    unknown = Reply(id="unknown-ref", sender_id=None, sender_nickname="")
    chat.message.message.extend([at, reply, unknown])
    raw = copy.deepcopy(chat.message.raw_message)
    store = ProfileStore(tmp_path / "profiles.sqlite3", clock=lambda: NOW)
    try:
        store.merge(identity.robot, "group", "other", sender, {"nickname": "Wrong group"}, source="current_chat", as_of=NOW)
        store.merge(RobotKey("other-app"), "group", "g", quoted, {"nickname": "Wrong robot"}, source="current_chat", as_of=NOW)
        enrich_chat(chat, store)
        assert not chat.message.sender.nickname and not at.name and not reply.sender_nickname and not unknown.sender_nickname
        store.merge(identity.robot, "group", "g", sender, {"nickname": "Correct sender"}, source="current_chat", as_of=NOW)
        store.merge(identity.robot, "group", "g", quoted, {"nickname": "Historical author"}, source="chat_history", as_of=NOW - 60)
        enrich_chat(chat, store)
        assert chat.message.sender.nickname == "Correct sender"
        assert at.name == reply.sender_nickname == "Historical author"
        assert unknown.sender_id is None and not unknown.sender_nickname
        assert chat.message.raw_message == raw and chat.message.message_str == payload["d"]["content"]
    finally:
        store.close()
