import json

import pytest
from test_messaging_state import NOW, chat_payload

from v2.commands import layout_preview
from v2.errors import V2Error
from v2.messaging.convert import convert_chat
from v2.messaging.store import MessageStore
from v2.models import InstanceKey
from v2.protocol import RawEnvelope
from v2.settings import DEFAULTS


@pytest.mark.parametrize("data", [{"id": "m", "author": {"member_openid": "u"}},
                                   {"id": "m", "group_openid": "g", "author": "u"},
                                   {"group_openid": "g", "author": {"member_openid": "u"}}])
def test_incomplete_chat_cannot_create_identity(data, config, tmp_path):
    store = MessageStore(tmp_path / "identities", clock=lambda: NOW)
    payload = chat_payload()
    payload["d"] = {"timestamp": payload["d"]["timestamp"], **data}
    envelope = RawEnvelope.parse(json.dumps(payload).encode(), now=NOW)
    try:
        with pytest.raises(V2Error):
            store.observe(convert_chat(InstanceKey.from_config(config), envelope))
        for table in ("identities", "targets", "sources"):
            assert store.db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize("node", [{}, [], 1])
def test_invalid_preview_node(node):
    with pytest.raises(V2Error) as exc:
        layout_preview({"scene": "group", "nodes": []}, DEFAULTS, node=node)
    assert exc.value.code == "invalid_preview"


def test_settings_capacity_and_restore_type(store):
    encoded = json.dumps(DEFAULTS)
    store.db.executemany("INSERT INTO settings VALUES (?, 0, ?, 0, ?, '')", ((str(n), encoded, encoded) for n in range(256)))
    store.db.commit()
    with pytest.raises(V2Error) as exc:
        store.mutate("over-limit", 0, "fixture", operation="save", patch={"title": "x"})
    assert exc.value.code == "settings_capacity"
    assert store.get("0")["revision"] == 0
    with pytest.raises(V2Error) as exc:
        store.mutate("0", 0, "fixture", operation="restore", restore_revision=[])
    assert exc.value.code == "invalid_settings"
    assert store.get("0")["revision"] == 0
