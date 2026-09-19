import asyncio
import base64
import copy
import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from aiohttp import web
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from test_transport_http import MappedSession, upstream
from test_transport_receive import StepClock
from v2.connections import Connections
from v2.errors import V2Error
from v2.onboarding import Onboarding
from v2.transport.http import HTTPTransport


class HostConfig(dict):
    def __init__(self, path, config):
        super().__init__({"platform": [copy.deepcopy(config)], "other_setting": "preserve"})
        self.config_path, self.saves, self.fail = str(path), 0, False
        self.save_config()

    def save_config(self):
        if self.fail:
            raise OSError("simulated disk full")
        self.saves += 1
        from pathlib import Path
        Path(self.config_path).write_text(json.dumps(self))


@pytest.fixture
def owner(config, tmp_path):
    cfg = HostConfig(tmp_path / "host.json", config)
    manager = SimpleNamespace(calls=[])
    async def reload(value):
        manager.calls.append(copy.deepcopy(value))
    manager.reload = reload
    value = SimpleNamespace(context=SimpleNamespace(get_config=lambda: cfg, platform_manager=manager),
        stopping=False, instances=set(), control=SimpleNamespace(fingerprint=lambda value:
            hmac.new(b"fixture-hmac", json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()))
    value.connections = Connections(value)
    return value


async def test_single_authority_partial_save_conflict_rollback_and_clear(owner, config):
    connections = owner.connections
    cfg = owner.context.get_config()
    view = connections.view(config["id"])
    assert config["secret"] not in json.dumps(view)
    saved = await connections.save(config["id"], view["fingerprint"], {"intents": 1}, confirm=True)
    assert cfg["platform"][0]["secret"] == config["secret"] and cfg["other_setting"] == "preserve"
    assert not owner.context.platform_manager.calls
    with pytest.raises(V2Error) as exc:
        await connections.save(config["id"], view["fingerprint"], {}, confirm=True)
    assert exc.value.code == "config_conflict"
    cfg.fail = True
    with pytest.raises(V2Error) as exc:
        await connections.save(config["id"], saved["fingerprint"], {"intents": 2}, confirm=True)
    assert exc.value.code == "config_save_failed" and cfg["platform"][0]["intents"] == 1
    cfg.fail = False
    with pytest.raises(V2Error):
        await connections.save(config["id"], saved["fingerprint"], {}, secret_action="clear", confirm=True)
    cleared = await connections.save(config["id"], saved["fingerprint"], {"enable": False}, secret_action="clear", confirm=True, confirm_secret=True)
    assert cfg["platform"][0]["secret"] == "" and not cleared["credentials_configured"]
    await connections.reload(config["id"], cleared["fingerprint"], confirm=True)
    assert len(owner.context.platform_manager.calls) == 1


async def test_external_disk_edits_and_foreign_instance_are_not_overwritten(owner, config):
    from pathlib import Path
    conn, cfg = owner.connections, owner.context.get_config()
    original = copy.deepcopy(dict(cfg))
    view = conn.view(config["id"])
    external = copy.deepcopy(original)
    external["other_setting"] = "external update"
    Path(cfg.config_path).write_text(json.dumps(external))
    with pytest.raises(V2Error) as exc:
        await conn.save(config["id"], view["fingerprint"], {}, confirm=True)
    assert exc.value.code == "config_conflict"
    assert json.loads(Path(cfg.config_path).read_text()) == external and dict(cfg) == original
    cfg["platform"][0]["type"] = "another_platform"
    with pytest.raises(V2Error) as exc:
        conn.view(config["id"])
    assert exc.value.code == "instance_not_owned"


async def test_new_disabled_target_and_uuid_persist_without_reload(owner):
    conn, cfg = owner.connections, owner.context.get_config()
    view = conn.view("new-v2")
    result = await conn.save("new-v2", view["fingerprint"], {"transport": "webhook"}, confirm=True)
    assert result["exists"] and result["webhook_path"]
    target = cfg["platform"][-1]
    assert not target["enable"] and not target["secret"] and target["unified_webhook_mode"]
    before = target["webhook_uuid"]
    conn.prepare_webhooks()
    assert cfg["platform"][-1]["webhook_uuid"] == before and not owner.context.platform_manager.calls


class Portal:
    def __init__(self, *, bad_cipher=False, status=2, bad_token=False, redirect=False):
        self.key = None
        self.calls = []
        self.bad_cipher, self.status, self.bad_token, self.redirect = bad_cipher, status, bad_token, redirect

    async def handle(self, request):
        self.calls.append(request.path)
        data = await request.json()
        if self.redirect:
            return web.Response(status=302, headers={"Location": "https://evil.invalid/"})
        if request.path == "/lite/create_bind_task":
            self.key = base64.b64decode(data["key"], validate=True)
            assert len(self.key) == 32
            result = {"task_id": "fixture-portal-task"}
        elif request.path == "/lite/poll_bind_result":
            assert data["task_id"] == "fixture-portal-task"
            nonce = b"123456789012"
            cipher = nonce + AESGCM(self.key).encrypt(nonce, b"new-fixture-secret", None)
            result = {"status": self.status, "bot_appid": "new-fixture-app", "user_openid": "not-a-chat-observation",
                      "bot_encrypt_secret": "broken" if self.bad_cipher else base64.b64encode(cipher).decode()}
        elif request.path == "/app/getAppAccessToken":
            assert data == {"appId": "new-fixture-app", "clientSecret": "new-fixture-secret"}
            if self.bad_token:
                return web.json_response({"code": 100016})
            return web.json_response({"access_token": "fixture-validated-token", "expires_in": 7200})
        else:
            raise AssertionError("Unexpected upstream action")
        return web.json_response({"retcode": 0, "data": result})


async def wait_ready_clock(clock):
    # Reaching the next scheduled sleep means the previous I/O/state step has completed.
    return await asyncio.wait_for(clock.waits.get(), 2)


async def test_qr_concurrency_decryption_validation_and_idempotent_commit(owner, config, monkeypatch):
    portal, clock = Portal(), StepClock()
    async with upstream(portal.handle) as base:
        monkeypatch.setattr(HTTPTransport, "_make_session", lambda self: MappedSession(base))
        onb = Onboarding(owner, session_factory=lambda: MappedSession(base), clock=lambda: clock.now, sleep=clock.sleep)
        fingerprint = owner.connections.view(config["id"])["fingerprint"]
        try:
            one, two = await asyncio.gather(*(onb.start("alice", config["id"], fingerprint, confirm=True) for _ in range(2)))
            assert one["ticket"] == two["ticket"]
            item = onb.bindings[one["ticket"]]
            await clock.tick()
            await wait_ready_clock(clock)
            assert portal.calls == ["/lite/create_bind_task", "/lite/poll_bind_result", "/app/getAppAccessToken"]
            assert item.state == "ready_to_commit" and not item.key
            public = await onb.status("alice", config["id"], item.ticket, renew=True)
            serialized = json.dumps(public)
            assert "new-fixture-secret" not in serialized and base64.b64encode(portal.key).decode() not in serialized
            assert "not-a-chat-observation" not in serialized and "fixture-validated-token" not in serialized
            with pytest.raises(V2Error):
                await onb.status("bob", config["id"], item.ticket)
            with pytest.raises(V2Error):
                await onb.status("alice", "another", item.ticket)
            with pytest.raises(V2Error):
                await onb.commit("alice", config["id"], item.ticket, "wrong", confirm=True)
            with pytest.raises(V2Error) as exc:
                await onb.commit("alice", config["id"], item.ticket, public["commit_handle"], confirm=True, confirm_secret=True)
            assert exc.value.code == "identity_confirmation_required"
            results = await asyncio.gather(*(onb.commit("alice", config["id"], item.ticket, public["commit_handle"],
                confirm=True, confirm_secret=True, confirm_identity=True) for _ in range(2)))
            assert results[0] == results[1] and results[0]["state"] == "configured"
            saved = owner.context.get_config()["platform"][0]
            assert saved["appid"] == "new-fixture-app" and saved["secret"] == "new-fixture-secret" and not saved["enable"]
            assert owner.context.get_config().saves == 2 and not owner.context.platform_manager.calls
            assert not item.key and not item.credential and item.qr is None
        finally:
            await onb.close()
        assert onb.session.closed and not onb.bindings


@pytest.mark.parametrize("kind", ["expired", "cancel", "cipher", "upstream_expired", "token", "redirect", "config_changed"])
async def test_binding_failure_cleanup_keeps_original_config(owner, config, monkeypatch, kind):
    portal = Portal(bad_cipher=kind == "cipher", status=3 if kind == "upstream_expired" else 2,
                    bad_token=kind == "token", redirect=kind == "redirect")
    clock = StepClock()
    original = copy.deepcopy(owner.context.get_config()["platform"])
    async with upstream(portal.handle) as base:
        monkeypatch.setattr(HTTPTransport, "_make_session", lambda self: MappedSession(base))
        onb = Onboarding(owner, session_factory=lambda: MappedSession(base), clock=lambda: clock.now, sleep=clock.sleep)
        try:
            start = await onb.start("alice", config["id"], owner.connections.view(config["id"])["fingerprint"], confirm=True)
            item = onb.bindings[start["ticket"]]
            if kind == "redirect":
                await asyncio.wait_for(item.task, 2)
            elif kind in {"expired", "cancel", "config_changed"}:
                delay, future = await wait_ready_clock(clock)
                assert item.qr and not item.credential
                if kind == "expired":
                    clock.now = 31
                    future.set_result(None)
                elif kind == "cancel":
                    await onb.cancel("alice", config["id"], item.ticket)
                else:
                    owner.context.get_config()["platform"][0]["intents"] = 1
                    future.set_result(None)
                await asyncio.wait_for(item.task, 2)
            else:
                await clock.tick()
                await asyncio.wait_for(item.task, 2)
            assert item.state in {"failed", "expired", "cancelled"}
            assert not item.key and not item.credential and item.qr is None
            if kind != "config_changed":
                assert owner.context.get_config()["platform"] == original
            assert owner.context.get_config().saves == 1 and not owner.context.platform_manager.calls
        finally:
            await onb.close()


async def test_stop_during_create_cancels_io(owner, config):
    entered, release = asyncio.Event(), asyncio.Event()
    async def blocked(request):
        entered.set()
        await release.wait()
        return web.json_response({"retcode": 0, "data": {"task_id": "late"}})
    async with upstream(blocked) as base:
        onb = Onboarding(owner, session_factory=lambda: MappedSession(base))
        view = owner.connections.view(config["id"])
        result = await onb.start("alice", config["id"], view["fingerprint"], confirm=True)
        item = onb.bindings[result["ticket"]]
        await asyncio.wait_for(entered.wait(), 1)
        await onb.close()
        release.set()
        assert item.task.done() and not item.key and not item.credential and onb.session.closed
        assert owner.context.get_config().saves == 1


async def test_ready_lease_renewal_absolute_ttl_and_no_revival(owner, config):
    from v2.onboarding import Binding
    now = [0]
    onb = Onboarding(owner, clock=lambda: now[0])
    fingerprint = owner.connections.view(config["id"])["fingerprint"]
    item = Binding("ticket", "alice", config["id"], fingerprint, 180, 30, state="ready_to_commit",
                   credential="fixture-secret", key=b"x" * 32, handle="handle")
    onb.bindings[item.ticket] = item
    try:
        for moment in range(20, 180, 20):
            now[0] = moment
            state = await onb.status("alice", config["id"], item.ticket, renew=True)
            assert state["state"] == "ready_to_commit" and item.lease <= 180
        now[0] = 180
        assert (await onb.status("alice", config["id"], item.ticket, renew=True))["state"] == "expired"
        assert not item.key and not item.credential
        with pytest.raises(V2Error):
            await onb.commit("alice", config["id"], item.ticket, "handle", confirm=True, confirm_secret=True)
        assert owner.context.get_config().saves == 1
    finally:
        await onb.close()


async def test_global_binding_capacity_stops_before_network(owner, config):
    from v2.onboarding import Binding
    onb = Onboarding(owner, clock=lambda: 0)
    for n in range(4):
        onb.bindings[str(n)] = Binding(str(n), "alice", f"other{n}", "fp", 180, 30)
    try:
        with pytest.raises(V2Error) as exc:
            await onb.start("alice", config["id"], owner.connections.view(config["id"])["fingerprint"], confirm=True)
        assert exc.value.code == "binding_capacity" and onb.session is None
    finally:
        await onb.close()


async def test_stop_while_commit_waits_for_configuration_lock(owner, config):
    from v2.onboarding import Binding
    onb = Onboarding(owner, clock=lambda: 0)
    item = Binding("ticket", "alice", config["id"], owner.connections.view(config["id"])["fingerprint"], 180, 30,
                   state="ready_to_commit", credential="fixture-new-secret", appid="new-app", handle="handle")
    onb.bindings[item.ticket] = item
    await owner.connections.lock.acquire()
    task = asyncio.create_task(onb.commit("alice", config["id"], item.ticket, "handle", confirm=True, confirm_secret=True, confirm_identity=True))
    await asyncio.sleep(0)
    await onb.close()
    owner.connections.lock.release()
    with pytest.raises(V2Error):
        await asyncio.wait_for(task, 1)
    assert owner.context.get_config().saves == 1 and not item.credential


@pytest.mark.parametrize("mask", ["********", "••••••", "[REDACTED]"])
async def test_mask_is_not_a_secret_replacement(owner, config, mask):
    conn = owner.connections
    with pytest.raises(V2Error) as exc:
        await conn.save(config["id"], conn.view(config["id"])["fingerprint"], {}, secret_action="replace",
                        secret=mask, confirm=True, confirm_secret=True)
    assert exc.value.code == "invalid_credentials" and owner.context.get_config().saves == 1


async def test_poll_network_retry_budget_with_virtual_scheduler(owner, config):
    portal, clock = Portal(), StepClock()
    polls = []
    async def handler(request):
        if request.path == "/lite/poll_bind_result":
            polls.append(1)
            return web.Response(status=503)
        return await portal.handle(request)
    async with upstream(handler) as base:
        onb = Onboarding(owner, session_factory=lambda: MappedSession(base), clock=lambda: clock.now, sleep=clock.sleep)
        try:
            result = await onb.start("alice", config["id"], owner.connections.view(config["id"])["fingerprint"], confirm=True)
            item = onb.bindings[result["ticket"]]
            for _ in range(5):  # Three poll ticks and two bounded backoff ticks.
                await clock.tick()
            await asyncio.wait_for(item.task, 1)
            assert len(polls) == 3 and item.state == "failed" and item.error == "binding_transient_error"
            assert not item.key and not item.credential and owner.context.get_config().saves == 1
        finally:
            await onb.close()


async def test_same_robot_shards_cannot_mix_counts_or_webhook(owner, config):
    conn, cfg = owner.connections, owner.context.get_config()
    cfg["platform"][0]["shard"] = [0, 2]
    cfg.save_config()
    view = conn.view("second-shard")
    await conn.save("second-shard", view["fingerprint"], {"appid": config["appid"], "enable": True, "shard": [1, 2]},
                    secret_action="replace", secret=config["secret"], confirm=True, confirm_secret=True)
    for patch in ({"transport": "webhook"}, {"transport": "websocket", "shard": [0, 1]}):
        target = "overlapping-receiver"
        with pytest.raises(V2Error) as exc:
            await conn.save(target, conn.view(target)["fingerprint"], {"appid": config["appid"], "enable": True, **patch},
                            secret_action="replace", secret=config["secret"], confirm=True, confirm_secret=True)
        assert exc.value.code == "duplicate_receiver"
    assert len(cfg["platform"]) == 2 and not owner.context.platform_manager.calls
