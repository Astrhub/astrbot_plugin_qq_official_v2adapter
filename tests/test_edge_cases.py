import json

import pytest

from v2.commands import layout_preview
from v2.errors import V2Error
from v2.models import RobotKey
from v2.protocol import IdentityCache, RawEnvelope
from v2.settings import DEFAULTS


@pytest.mark.parametrize("data", [{"id": "m", "author": {"member_openid": "u"}},
                                   {"id": "m", "group_openid": "g", "author": "u"},
                                   {"group_openid": "g", "author": {"member_openid": "u"}}])
def test_incomplete_chat_cannot_create_identity(data):
    cache = IdentityCache()
    envelope = RawEnvelope.parse(json.dumps({"op": 0, "t": "GROUP_AT_MESSAGE_CREATE", "d": data}).encode())
    with pytest.raises(V2Error):
        cache.observe_chat(RobotKey("app"), envelope)
    assert not cache.items


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
