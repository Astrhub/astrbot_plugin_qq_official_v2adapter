"""P4 through the real Dashboard, host event bus and transport fixtures."""
import asyncio
import base64
import importlib
import time
from datetime import UTC, datetime

from aiohttp import web
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from test_interactions import interaction
from test_media_boundary import PNG
from test_media_upload import AssetSession
from test_messaging_state import chat_payload
from test_transport_http import MappedSession
from test_transport_receive import signed


class ExtensionProbe:
    def __init__(self):
        self.acks, self.streams, self.puts, self.uploads = [], [], [], []

    async def handle(self, request):
        if request.path == "/part/assembly":
            assert "Authorization" not in request.headers and "X-Union-Appid" not in request.headers
            self.puts.append(await request.read())
            return web.Response(status=200)
        if request.path.startswith("/interactions/"):
            assert request.method == "PUT" and request.headers["Authorization"].startswith("QQBot ")
            assert await request.json() == {"code": 0}
            self.acks.append(request.path)
            return web.Response(status=204)
        if request.path == "/v2/users/user-one/stream_messages":
            self.streams.append(await request.json())
            return web.json_response({"id": "assembly-stream-id", "remain_msg_len": 0, "ext_info": {"ref_idx": "REFIDX_stream"}})
        if request.path.endswith(("/upload_prepare", "/upload_part_finish", "/files")):
            assert request.headers["X-Union-Appid"] == "new-fixture-app"
            data = await request.json()
            self.uploads.append((request.path, data))
            if request.path.endswith("upload_prepare"):
                return web.json_response({"upload_id": "assembly-upload", "block_size": str(len(PNG)), "parts": [{"index": 0, "block_size": str(len(PNG)), "presigned_url": "https://cos.test/part/assembly?sign=fixture-private"}]})
            if request.path.endswith("upload_part_finish"):
                return web.Response(status=204)
            assert data["srv_send_msg"] is False and data["upload_id"] == "assembly-upload"
            return web.json_response({"file_info": "assembly-file", "ttl": 60})
        return None


async def extension_roundtrip(lifecycle, client, headers, owner, instance, callback, base, replies, completed, probe, previous_event, monkeypatch):
    prefix = "/api/v1/plugins/extensions/astrbot_plugin_qq_official_v2adapter"
    boot = (await client.get(prefix + "/bootstrap", headers=headers)).json()
    view = (await client.get(prefix + "/config", params={"platform_id": instance.identity.platform_id}, headers=headers)).json()
    common = {"platform_id": instance.identity.platform_id, "fingerprint": view["fingerprint"], "csrf": boot["csrf"], "scene": "group"}
    options = {"keyboard_enabled": True, "keyboard_execute": True, "typing_enabled": True}
    saved = await client.post(prefix + "/config/mutate", headers=headers, json={**common, "revision": view["revision"], "operation": "save", "patch": {"extensions": options}})
    assert saved.status_code == 200, saved.text
    applied = await client.post(prefix + "/config/mutate", headers=headers, json={**common, "revision": saved.json()["revision"], "operation": "apply", "confirm": True})
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"]["extensions"]["keyboard_enabled"]
    instance.ack_http._factory = lambda: MappedSession(base)

    async def receive(payload, *, pipeline=True):
        req = signed(payload, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
        res = await client.post(callback, content=req.raw, headers=dict(req.headers))
        assert res.status_code == 200
        body = await asyncio.wait_for(replies.get(), 5)
        event = await asyncio.wait_for(completed.get(), 5) if pipeline else None
        if not pipeline:
            await asyncio.wait_for(asyncio.gather(*tuple(instance.extensions.tasks)), 5)
        return body, event

    body, event = await receive(chat_payload(message_id="assembly-keyboard", timestamp=time.time(), text="/v2menu kb home 0"))
    assert body["msg_type"] == 2 and "keyboard" in body
    rows = body["keyboard"]["content"]["rows"]
    assert 1 <= len(rows) <= 5 and all(1 <= len(row["buttons"]) <= 5 for row in rows)
    token = rows[0]["buttons"][0]["action"]["data"]
    before = [tuple(r) for r in owner.messages.db.execute("SELECT * FROM identities")]
    cfg = {"appid": "new-fixture-app"}
    frame = interaction(cfg, token=token, event_id="assembly-outer", interaction_id="assembly-inner")
    frame["d"]["timestamp"] = datetime.now(UTC).isoformat()
    body, projected = await receive(frame)
    assert probe.acks == ["/interactions/assembly-inner"]
    assert body["event_id"] == "assembly-outer" and "msg_id" not in body and "keyboard" in body
    assert projected.command_admitted and not projected.call_llm and projected._has_send_oper
    assert projected.message_obj.message_id == "" and projected.raw_data["derived_from_interaction"]
    assert before == [tuple(r) for r in owner.messages.db.execute("SELECT * FROM identities")]
    assert instance.extensions.records()[0]["business"] == "finished_unconfirmed"
    req = signed(frame, appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
    assert (await client.post(callback, content=req.raw, headers=dict(req.headers))).status_code == 200
    assert probe.acks == ["/interactions/assembly-inner"] and replies.empty()
    # Read-only built-in sid proves two separate clicks enter the normal handler pipeline.
    help_module = importlib.import_module(owner.__module__.rsplit(".", 1)[0] + ".v2.help")
    catalog = instance.extensions.tickets.catalog(event.route)
    sid = next(n for n in catalog["nodes"] if n["command"] == "/sid")
    detail, detail_event = await receive(chat_payload(message_id="assembly-detail", timestamp=time.time(), text="/v2menu kb detail " + help_module.node_token(sid) + " 0"))
    button = next(b for row in detail["keyboard"]["content"]["rows"] for b in row["buttons"] if b["render_data"]["label"] == "请求执行")
    confirm = interaction(cfg, token=button["action"]["data"], event_id="assembly-confirm-outer", interaction_id="assembly-confirm-inner")
    confirm["d"]["timestamp"] = datetime.now(UTC).isoformat()
    confirmation, _ = await receive(confirm, pipeline=False)
    assert instance.extensions.records()[0]["business"] == "confirmation_sent" and completed.empty()
    run_button = confirmation["keyboard"]["content"]["rows"][0]["buttons"][0]
    execution = interaction(cfg, token=run_button["action"]["data"], event_id="assembly-execute-outer", interaction_id="assembly-execute-inner")
    execution["d"]["timestamp"] = datetime.now(UTC).isoformat()
    output, executed = await receive(execution)
    assert "UMO:" in output["content"] and "user-one" in output["content"]
    assert executed.command_admitted and not executed.call_llm
    assert [h.handler_name for h in executed.get_extra("activated_handlers")] == ["sid"]

    from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
    handler = next(h for h in projected.get_extra("activated_handlers") if h.handler_name == "menu")
    config = owner.context.get_config()
    config["admins_id"] = ["user-one"]
    config.save_config()
    with monkeypatch.context() as patch:
        patch.setattr(handler, "event_filters", [*handler.event_filters, PermissionTypeFilter(PermissionType.ADMIN)])
        catalog = instance.extensions.tickets.catalog(event.route)
        node = next(n for n in catalog["nodes"] if n["menu_entry"])
        ticket = instance.extensions.tickets.issue(event.route, "user-one", node, node["command"] + " kb home 0", "navigate", catalog["version"])
        commit = instance.commit_event
        def restricted(projection):
            if projection.raw_data.get("derived_from_interaction"):
                projection.set_extra("_api_key_allow_admin_role", False)
            return commit(projection)
        patch.setattr(instance, "commit_event", restricted)
        denied = interaction(cfg, token=ticket, event_id="assembly-denied-outer", interaction_id="assembly-denied-inner")
        denied["d"]["timestamp"] = datetime.now(UTC).isoformat()
        denial, blocked = await receive(denied)
        assert "权限不足" in denial["content"] and denial["event_id"] == "assembly-denied-outer"
        assert not blocked.command_admitted and not blocked.call_llm and blocked.role == "member"
        assert instance.extensions.records()[0]["business"] == "rejected"
    config["admins_id"] = ["not-the-fixture-user"]
    config.save_config()

    _, private = await receive(chat_payload("C2C_MESSAGE_CREATE", message_id="assembly-c2c", timestamp=time.time(), text="/v2menu home 0"))
    async def generated():
        yield MessageChain([Plain("first")])
        yield MessageChain([Plain(" second")])
    await private.send_streaming(generated())
    assert [v["index"] for v in probe.streams] == [0, 1, 2]
    assert all(v["msg_id"] == "assembly-c2c" and v["msg_seq"] == 2 for v in probe.streams)
    assert probe.streams[-1]["input_state"] == 10
    assert private.get_extra("qq_send_result")["message_id"] == "assembly-stream-id"
    assert private.get_extra("qq_stream_mode") == "native" and private._has_send_oper
    notified = await private.send_typing()
    notification = await asyncio.wait_for(replies.get(), 3)
    assert notified["kind"] == "typing" and "message_id" not in notified
    assert notification["msg_type"] == 6 and notification["msg_id"] == "assembly-c2c" and notification["msg_seq"] == 3
    await private.stop_typing()
    assert not instance.typing.jobs

    module = importlib.import_module(instance.media.__class__.__module__.rsplit(".", 1)[0] + ".io")
    transfers = []
    await instance.media.transfer.close()
    instance.media.transfer = module.UploadTransfer(owner.media_pool, session_factory=lambda: AssetSession(base, transfers))
    await previous_event.send(MessageChain([Image.fromBase64(base64.b64encode(PNG).decode())]))
    media_body = await asyncio.wait_for(replies.get(), 3)
    assert media_body["media"] == {"file_info": "assembly-file"} and media_body["msg_id"] == previous_event.message_obj.message_id
    assert probe.puts == [PNG] and transfers == [("PUT", "https://cos.test/part/assembly?sign=fixture-private")]
    assert owner.media_pool.used == 0 and not instance.media.blobs
    assert not instance.streaming.tasks and not instance.extensions.tasks
    await asyncio.wait_for(asyncio.gather(*lifecycle.event_bus._pending_tasks), 5)
