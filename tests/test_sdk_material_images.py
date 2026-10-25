"""Platform material images follow the official prepare/complete/list/delete contract."""

from types import SimpleNamespace

import pytest

from v2.client import ClientState, V2Client
from v2.errors import V2Error
from v2.extensions.state import ExtensionStore
from v2.messaging.outbound import SendingCore
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, RobotKey

PREPARE = {"upload_id": "upload_1792051409302628435_772218",
           "presigned_url": "https://cos.test/robot_upload?sig=private", "expires_in": 3600}
COMPLETE = {"image_id": "102083127_3f9a1c4b7d2e8065",
            "url": "qqbot-media://image/v1/6awbXUyjS27ckbHEMVNHr_ot0jW19fq", "ttl": 7776000,
            "preview_url": "https://cos.test/preview?sig=short-lived"}
LISTING = {"images": [{"image_id": "102083127_3f9a1c4b7d2e8065",
                       "url": "qqbot-media://image/v1/6awbXUyjS27ckbHEMVNHr_ot0jW19fq",
                       "file_name": "kline.png", "preview_url": "https://cos.test/preview?sig=x",
                       "created_at": 1792051409, "expires_at": 1792656210}], "next_cursor": "", "total": 1}


@pytest.fixture
async def material(tmp_path):
    identity = InstanceKey("platform", RobotKey("app"))
    store = MessageStore(tmp_path / "material.sqlite3", clock=lambda: 10000.0)
    extension = ExtensionStore(store)
    calls = []
    payloads = {(("POST", "/v2/media-images/prepare")): PREPARE,
                ("POST", "/v2/media-images/complete"): COMPLETE,
                ("GET", "/v2/media-images"): LISTING,
                ("DELETE", "/v2/media-images/102083127_3f9a1c4b7d2e8065/delete"): {}}

    class HTTP:
        def __init__(self, identity):
            self.identity = identity

        def check(self):
            return None

        async def request(self, spec, *, before_send=None):
            if before_send is not None:
                before_send()
            calls.append((spec.method, spec.path, spec.params, spec.json_body))
            key = (spec.method, spec.path)
            assert key in payloads, key
            return SimpleNamespace(data=payloads[key], status=200, trace_id="fixture-trace")

    class Sender:
        http = None
        media = None

        def check(self, route, source):
            return None

    http = HTTP(identity)
    sender = SendingCore(identity, http, store, is_online=lambda: True, ws_online=lambda: True)
    state = ClientState(identity)
    state.http, state.sender, state.extension_state = http, sender, extension
    client = V2Client(identity, state=state)
    try:
        yield SimpleNamespace(client=client, calls=calls, store=store, extension=extension)
    finally:
        store.close()


async def test_prepare_complete_and_delete_follow_official_wire_contract(material):
    q = material.client.qq
    prepared = await q.post_media_image_prepare("kline.png", 40156, "image/png", "e" * 32, "f" * 40,
                                                operation_id="material-prepare")
    assert prepared == PREPARE
    assert material.calls[0][0:2] == ("POST", "/v2/media-images/prepare")
    assert material.calls[0][3] == {"file_name": "kline.png", "file_size": 40156, "content_type": "image/png",
                                    "md5": "e" * 32, "sha1": "f" * 40}
    completed = await q.post_media_image_complete("upload_1792051409302628435_772218", "e" * 32,
                                                  operation_id="material-complete")
    assert completed == COMPLETE
    assert material.calls[1][0:2] == ("POST", "/v2/media-images/complete")
    assert material.calls[1][3] == {"upload_id": "upload_1792051409302628435_772218", "md5": "e" * 32}
    deleted = await q.delete_media_image("102083127_3f9a1c4b7d2e8065", operation_id="material-delete")
    assert deleted == {}
    assert material.calls[2][0:2] == ("DELETE", "/v2/media-images/102083127_3f9a1c4b7d2e8065/delete")
    # The ledger keeps durable references only; presigned and preview links stay transient.
    ledger = [material.extension.operation(material.client.identity.robot, op)["result"]
              for op in ("material-prepare", "material-complete", "material-delete")]
    assert ledger[0] == {"upload_id": PREPARE["upload_id"]}
    assert ledger[1] == {"image_id": COMPLETE["image_id"], "url": COMPLETE["url"], "ttl": COMPLETE["ttl"]}
    assert ledger[2] == {"state": "succeeded"}
    dump = "\n".join(material.store.db.iterdump())
    assert "cos.test" not in dump


async def test_list_reads_one_page_with_bounded_cursor(material):
    q = material.client.qq
    first = await q.get_media_images()
    assert first == LISTING and material.calls[0][0:3][:2] == ("GET", "/v2/media-images")
    assert material.calls[0][2] == {}
    paged = await q.get_media_images("opaque-cursor")
    assert paged == LISTING and material.calls[1][2] == {"cursor": "opaque-cursor"}


async def test_prepare_validates_documented_bounds(material):
    q = material.client.qq
    for args in (
        ("kline.png", 40156, "image/bmp", "e" * 32, "f" * 40),
        ("kline.png", 0, "image/png", "e" * 32, "f" * 40),
        ("kline.png", 10 * 1024 * 1024 + 1, "image/png", "e" * 32, "f" * 40),
        ("kline.png", 40156, "image/png", "e" * 31, "f" * 40),
        ("kline.png", 40156, "image/png", "e" * 32, "f" * 39),
        ("bad/name.png", 40156, "image/png", "e" * 32, "f" * 40),
    ):
        with pytest.raises(V2Error):
            await q.post_media_image_prepare(*args)
    with pytest.raises(V2Error):
        await q.post_media_image_complete("", "e" * 32)
    with pytest.raises(V2Error):
        await q.delete_media_image("")
    with pytest.raises(V2Error):
        await q.get_media_images("x" * 4097)
    assert material.calls == []
