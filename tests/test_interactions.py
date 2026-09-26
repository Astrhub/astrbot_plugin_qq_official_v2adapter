from types import SimpleNamespace

import pytest
from test_messaging_state import NOW

from v2.errors import V2Error
from v2.extensions.events import ExtensionEvent
from v2.messaging.store import MessageStore
from v2.models import InstanceKey, SessionRoute


def interaction(config, *, kind=11, actor="user-one", target="group-one", token="", event_id="outer-event", interaction_id="inner-interaction"):
    return {"op": 0, "s": 10, "t": "INTERACTION_CREATE", "id": event_id, "d": {
        "id": interaction_id, "application_id": config["appid"], "type": kind, "scene": "group", "chat_type": 1,
        "group_openid": target, "group_member_openid": actor, "timestamp": "2027-01-15T08:00:00+00:00",
        "data": {"type": kind, "resolved": {"button_data": token, "message_id": "real-operated-message"}}}}


def authorization_notice():
    return {"op": 0, "s": 4, "t": "INTERACTION_CREATE", "id": "INTERACTION_CREATE:notice", "d": {
        "data": {"resolved": {}, "type": 2001}, "group_openid": "group-one",
        "id": "authorization-notice", "scene": "group", "timestamp": "2027-01-15T08:00:00+00:00",
        "type": 20, "version": 1}}


def test_authorization_notice_accepts_its_data_subtype_without_a_chat_actor(config):
    payload = authorization_notice()
    event = ExtensionEvent.parse(InstanceKey.from_config(config), payload, NOW)
    assert event.interaction_type == 20 and event.payload["d"]["data"]["type"] == 2001
    assert event.scene == "group" and event.target == "group-one"
    assert event.actor is None and event.message_id is None and event.payload == payload
    with pytest.raises(V2Error) as exc:
        event.route(InstanceKey.from_config(config))
    assert exc.value.code == "interaction_projection_unsupported"


@pytest.mark.parametrize("kind", [11, 12])
def test_executable_callback_rejects_a_conflicting_data_type(config, kind):
    payload = interaction(config, kind=kind)
    payload["d"]["data"]["type"] = 2001
    with pytest.raises(V2Error) as exc:
        ExtensionEvent.parse(InstanceKey.from_config(config), payload, NOW)
    assert exc.value.code == "invalid_extension_event"


def test_interaction_ids_and_typed_source_are_not_chat_ids(config):
    identity = InstanceKey.from_config(config)
    event = ExtensionEvent.parse(identity, interaction(config), NOW)
    assert event.event_id == "outer-event" and event.interaction_id == "inner-interaction"
    assert event.message_id == "real-operated-message" and event.actor == "user-one"
    assert event.payload["d"]["id"] == "inner-interaction"
    bad = interaction(config)
    bad["d"]["application_id"] = "another-bot"
    with pytest.raises(V2Error):
        ExtensionEvent.parse(identity, bad, NOW)


@pytest.fixture
def tickets(config, tmp_path):
    from v2.extensions.keyboard import TicketStore
    identity = InstanceKey.from_config(config)
    clock = [NOW]
    store = MessageStore(tmp_path / "state", clock=lambda: clock[0])
    settings = {"applied_revision": 1, "applied": {"extensions": {"keyboard_enabled": True, "keyboard_execute": True, "ticket_ttl": 120}}}
    node = {"id": "fixture.command", "binding": "source-v1", "command": "/run", "parameters": [], "group": False, "enabled": True, "permission": [], "menu_entry": False}
    catalog = {"version": "catalog-v1", "scene": "group", "nodes": [node]}
    owner = SimpleNamespace(messages=store, store=SimpleNamespace(get=lambda key: settings), context=SimpleNamespace(get_config=lambda *args: {"admins_id": []}))
    adapter = SimpleNamespace(identity=identity, owner=owner, check_generation=lambda: None)
    service = TicketStore(adapter, catalog_provider=lambda route: catalog)
    route = SessionRoute(identity.robot, "group", "group-one")
    try:
        yield SimpleNamespace(service=service, clock=clock, store=store, settings=settings, catalog=catalog, node=node, route=route, identity=identity)
    finally:
        store.close()


def test_tickets_single_use_tamper_actor_scope_generation_and_revision(config, tickets):
    s = tickets
    token = s.service.issue(s.route, "user-one", s.node, "/run", "execute", s.catalog["version"])
    assert "/run" not in token
    valid = ExtensionEvent.parse(s.identity, interaction(config, token=token), NOW)
    for payload in [interaction(config, actor="other", token=token), interaction(config, target="other", token=token)]:
        with pytest.raises(V2Error):
            s.service.redeem(token, ExtensionEvent.parse(s.identity, payload, NOW))
    with pytest.raises(V2Error):
        s.service.redeem(token + "x", valid)
    assert s.service.redeem(token, valid)["handler"] == "fixture.command"
    with pytest.raises(V2Error):
        s.service.redeem(token, valid)
    next_token = s.service.issue(s.route, "user-one", s.node, "/run", "execute", s.catalog["version"])
    s.settings["applied_revision"] = 2
    with pytest.raises(V2Error):
        s.service.redeem(next_token, valid)


@pytest.mark.parametrize("change", ["expired", "renamed", "disabled", "permission", "parameters", "source"])
def test_ticket_live_command_contract_revalidated(config, tickets, change):
    s = tickets
    token = s.service.issue(s.route, "user-one", s.node, "/run", "execute", s.catalog["version"])
    event = ExtensionEvent.parse(s.identity, interaction(config, token=token), NOW)
    if change == "expired":
        s.clock[0] += 121
    elif change == "renamed":
        s.node["command"] = "/renamed"
    elif change == "disabled":
        s.node["enabled"] = False
    elif change == "permission":
        s.node["permission"] = ["admin"]
    elif change == "parameters":
        s.node["parameters"] = [{"name": "secret", "required": True}]
    else:
        s.node["binding"] = "replacement-same-name"
    with pytest.raises(V2Error):
        s.service.redeem(token, event)
