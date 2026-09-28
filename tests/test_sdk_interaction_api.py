"""Non-managed interactions use the same ACK lane; managed tickets keep core ownership."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver
from test_messaging_state import NOW
from importlib import import_module


def interaction(instance, kind, identifier):
    return {"op": 0, "t": "INTERACTION_CREATE", "id": "outer-" + identifier,
            "d": {"id": identifier, "type": kind, "application_id": instance.identity.robot.appid,
                  "scene": "c2c", "user_openid": "user-one",
                  "timestamp": datetime.fromtimestamp(NOW, UTC).isoformat(), "data": {"resolved": {}}}}


@pytest.fixture
async def ack_fixture(receiver):
    owner, instance = receiver
    calls = []
    failure = []
    class ACK:
        identity = instance.identity
        async def request(self, spec, *, before_send=None):
            assert spec.method == "PUT"
            if before_send is not None:
                before_send()
            calls.append((spec.url, spec.json_body))
            if failure:
                raise failure.pop(0)
            return SimpleNamespace(data={}, status=200)
        async def close(self):
            pass
    instance.extensions.ack_http = ACK()
    Error = import_module(instance.identity.__class__.__module__.split('.v2.')[0] + '.v2.errors').V2Error
    yield SimpleNamespace(instance=instance, owner=owner, calls=calls, failure=failure, Error=Error)


async def test_nonmanaged_interaction_ack_one_result_and_conflict(ack_fixture):
    f = ack_fixture
    event = interaction(f.instance, 14, "non-managed")
    assert f.instance.extensions.accept(event, NOW)
    view = f.instance.client.qq.with_options(operation_id="manual-ack")
    assert await view.on_interaction_result("non-managed", 3) == {}
    assert f.calls == [("https://api.bot.qq.com/interactions/non-managed", {"code": 3})]
    assert f.owner.extension_state.operation(f.instance.identity.robot, "manual-ack")["result"] == {"code": 3}
    assert await f.instance.client.qq.on_interaction_result("non-managed", 3) == {}
    with pytest.raises(f.Error) as conflicting:
        await f.instance.client.qq.on_interaction_result("non-managed", 4)
    assert conflicting.value.code == "operation_conflict" and len(f.calls) == 1

async def test_interaction_uses_literal_https_ack_transport(ack_fixture):
    import json
    f = ack_fixture
    assert f.instance.extensions.accept(interaction(f.instance, 14, "live-path"), NOW)
    calls = []
    class Response:
        status, headers = 200, {}
        @property
        def content(self):
            class Stream:
                async def iter_chunked(self, size):
                    yield json.dumps({}).encode()
            return Stream()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
    class Session:
        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()
        async def close(self):
            pass
    async def token_provider(*, rejected=None):
        return "ack-fixture"
    Transport = import_module(f.instance.identity.__class__.__module__.split('.v2.')[0] + '.v2.transport.http').HTTPTransport
    http = Transport(f.instance.identity, "", session_factory=Session, token_provider=token_provider)
    f.instance.extensions.ack_http = http
    assert await f.instance.client.qq.on_interaction_result("live-path", 0) == {}
    assert calls[0][:2] == ("PUT", "https://api.bot.qq.com/interactions/live-path")
    assert calls[0][2]["json"] == {"code": 0}
    assert calls[0][2]["headers"]["Authorization"] == "QQBot ack-fixture"
    assert calls[0][2]["allow_redirects"] is False



async def test_managed_buttons_and_menus_cannot_be_reacknowledged_even_with_invalid_tickets(ack_fixture):
    f = ack_fixture
    for kind, identifier in ((11, "invalid-button"), (12, "managed-menu")):
        event = interaction(f.instance, kind, identifier)
        event["d"]["data"]["resolved"]["button_data"] = "invalid-owned-ticket"
        assert f.instance.extensions.accept(event, NOW)
        with pytest.raises(f.Error) as denied:
            await f.instance.client.qq.on_interaction_result(identifier, 5)
        assert denied.value.code == "interaction_owned"
    await asyncio.sleep(0)
    assert all(body == {"code": 0} for _, body in f.calls)
    assert len(f.calls) <= 2


async def test_unobserved_and_unknown_ack_do_not_replay(ack_fixture):
    f = ack_fixture
    with pytest.raises(f.Error) as unobserved:
        await f.instance.client.qq.on_interaction_result("guess", 0)
    assert unobserved.value.code == "interaction_not_observed" and not f.calls
    assert f.instance.extensions.accept(interaction(f.instance, 14, "uncertain"), NOW)
    f.failure.append(f.Error("network_failure", "fixture", phase="result_unknown", status=503))
    with pytest.raises(f.Error) as unknown:
        await f.instance.client.qq.on_interaction_result("uncertain", 1)
    assert unknown.value.phase == "result_unknown"
    with pytest.raises(f.Error) as repeated:
        await f.instance.client.qq.on_interaction_result("uncertain", 1)
    assert repeated.value.code == "extension_result_unknown" and len(f.calls) == 1
