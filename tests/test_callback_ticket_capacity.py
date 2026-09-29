"""Shared callback-ticket storage must not couple separate QQ robots."""
import asyncio
import hashlib

import pytest
from test_button_callbacks import callback_case as callback_case
from test_extension_dispatch import dispatch as dispatch
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW

from v2.messaging.store import robot_key


async def test_callback_ticket_capacity_isolated_by_appid_and_environment(callback_case):
    s, event, star, _ = callback_case
    owner, instance = s.owner, s.instance
    clock = [NOW]
    owner.messages.clock = lambda: clock[0]
    route = event.route
    first = event.qq.callback_button(star.direct, label="执行")
    key = robot_key(route.robot)
    stored = owner.messages.db.execute(
        "SELECT expires,body FROM callback_tickets WHERE token=? AND robot=?",
        (hashlib.sha256(first["action"]["data"].encode()).hexdigest(), key),
    ).fetchone()
    assert stored is not None and stored["expires"] == NOW + 120

    # Seed a full set of real-schema receipts in one transaction; issuance and lookup below use live Star/adapter paths.
    with owner.messages.transaction():
        owner.messages.db.executemany(
            "INSERT INTO callback_tickets VALUES(?,?,?,?,?,?,?)",
            ((hashlib.sha256(f"qv2cb.fixture-{i:04d}".encode()).hexdigest(), key,
              stored["expires"], 0, 0, None, stored["body"]) for i in range(4095)),
        )
    assert owner.messages.db.execute("SELECT count(*) FROM callback_tickets WHERE robot=?", (key,)).fetchone()[0] == 4096
    with pytest.raises(RuntimeError) as full:
        event.qq.callback_button(star.direct, label="满额")
    assert full.value.code == "ticket_capacity"
    assert owner.messages.db.execute("SELECT count(*) FROM callback_tickets WHERE robot=?", (key,)).fetchone()[0] == 4096

    # A different AppID shares this MessageStore, without sharing a ticket budget.
    configs = [
        {**s.config, "id": "second-platform", "appid": "second-app"},
        {**s.config, "id": "third-platform", "appid": "third-app"},
    ]
    siblings = []
    for config in configs:
        owner.context.get_config()["platform"].append(config)
        sibling = owner.adapter_class(config, {}, asyncio.Queue())
        settings_key = sibling.identity.settings_key
        current = owner.store.get(settings_key)
        saved = owner.store.mutate(settings_key, current["revision"], "fixture", operation="save",
                                   patch={"extensions": {"keyboard_enabled": True}})
        owner.store.mutate(settings_key, saved["revision"], "fixture", operation="apply")
        siblings.append(sibling)
    keys = {key, *(robot_key(sibling.identity.robot) for sibling in siblings)}
    assert len(keys) == 3 and len({id(sibling.owner.messages) for sibling in (instance, *siblings)}) == 1
    page_size = owner.messages.db.execute("PRAGMA page_size").fetchone()[0]
    max_pages = owner.messages.db.execute("PRAGMA max_page_count").fetchone()[0]
    assert 0 < max_pages * page_size <= 128 * 1024 * 1024

    clock[0] = NOW + 119  # Original tickets are still live; new siblings' tickets outlive them.
    for sibling in siblings:
        sibling_route = sibling.client.route_for("group", "group-one")
        button = sibling.extensions.callbacks.issue(sibling_route, "user-one", star.direct, label="独立")
        ticket = button["action"]["data"]
        sibling.extensions.callbacks.validate(sibling_route, ticket, button["action"]["permission"])
        with pytest.raises(RuntimeError) as mismatch:
            instance.extensions.callbacks._lookup(ticket)
        assert mismatch.value.code == "ticket_unavailable"
        assert owner.messages.db.execute(
            "SELECT count(*) FROM callback_tickets WHERE robot=?", (robot_key(sibling_route.robot),)
        ).fetchone()[0] == 1
    with pytest.raises(RuntimeError) as still_full:
        event.qq.callback_button(star.direct, label="仍满")
    assert still_full.value.code == "ticket_capacity"

    clock[0] = NOW + 121
    renewed = event.qq.callback_button(star.direct, label="已回收")
    instance.extensions.callbacks.validate(route, renewed["action"]["data"], renewed["action"]["permission"])
    assert owner.messages.db.execute("SELECT count(*) FROM callback_tickets WHERE robot=?", (key,)).fetchone()[0] == 1
    for sibling in siblings:
        assert owner.messages.db.execute(
            "SELECT count(*) FROM callback_tickets WHERE robot=?", (robot_key(sibling.identity.robot),)
        ).fetchone()[0] == 1
