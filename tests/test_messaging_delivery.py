import asyncio
import copy
import sqlite3

import pytest
from test_lifecycle import context
from test_lifecycle import plugin_module as plugin_module
from test_messaging_state import NOW, chat_payload

from v2.protocol import RawEnvelope


@pytest.fixture
async def receiver(plugin_module, config, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module.StarTools, "get_data_dir", lambda *args: tmp_path)
    ctx = context()
    ctx.get_config()["platform"].append(copy.deepcopy(config))
    owner = plugin_module.QQOfficialV2(ctx, {})
    await owner.initialize()
    owner.messages.clock = lambda: NOW
    instance = owner.adapter_class(ctx.get_config()["platform"][0], {"unique_session": True}, asyncio.Queue())
    try:
        yield owner, instance
    finally:
        await owner.terminate()


def accept(owner, instance, **kwargs):
    payload = chat_payload(**kwargs)
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    return payload


async def test_delivery_bounded_even_when_host_queue_is_unbounded(receiver):
    owner, instance = receiver
    owner.delivery_slots.capacity = 1
    accept(owner, instance)
    accept(owner, instance, message_id="second", text="same text")
    assert instance.consumer.step()
    assert instance._event_queue.qsize() == 1 and owner.inbox.count(instance.identity.settings_key) == 1
    first = instance._event_queue.get_nowait()
    assert first.route.user == "user-one" and first.bot._source.message_id == "msg-one"
    assert not instance.consumer.step()  # The host popped it but the pipeline has not finished.
    assert instance.consumer.state == "backpressured"
    first.cleanup_temporary_local_files()
    assert instance.consumer.step() and owner.inbox.count(instance.identity.settings_key) == 0
    second = instance._event_queue.get_nowait()
    assert second.message_obj.message_id == "second"
    second.cleanup_temporary_local_files()
    assert not owner.delivery_slots.events


async def test_failed_commit_and_cancel_retain_raw(receiver, monkeypatch):
    owner, instance = receiver
    original = accept(owner, instance)
    def full(event):
        raise asyncio.QueueFull
    monkeypatch.setattr(instance, "commit_event", full)
    with pytest.raises(asyncio.QueueFull):
        instance.consumer.step()
    assert owner.inbox.pending(instance.identity.settings_key)[0]["payload"] == original
    assert not owner.delivery_slots.events
    instance.consumer.start()
    await asyncio.sleep(0)
    await instance.consumer.close()
    assert owner.inbox.count(instance.identity.settings_key) == 1


async def test_quarantine_does_not_poison_other_chat_and_extensions_stay_outside_pipeline(receiver):
    owner, instance = receiver
    key = instance.identity.settings_key
    bad = chat_payload(message_id="bad")
    del bad["d"]["author"]
    owner.inbox.accept(key, RawEnvelope(bad, NOW))
    interaction = {"op": 0, "s": 19, "id": "event-interaction", "t": "INTERACTION_CREATE", "d": {"id": "interaction-id", "content": "/admin"}}
    owner.inbox.accept(key, RawEnvelope(interaction, NOW))
    accept(owner, instance)
    for _ in range(3):
        assert instance.consumer.step()
    assert instance._event_queue.qsize() == 1
    retained = owner.inbox.retained(key)
    assert [r["state"] for r in retained] == ["invalid", "invalid"]
    assert retained[1]["reason"] == "unsupported_extension_event"
    assert retained[1]["payload"] == interaction
    unknown = {"op": 0, "s": 20, "id": "unknown-event", "t": "FUTURE_EVENT", "d": {"content": "/admin"}}
    owner.inbox.accept(key, RawEnvelope(unknown, NOW))
    assert instance.consumer.step()
    assert owner.inbox.retained(key)[2]["state"] == "extension"
    assert owner.inbox.retained(key)[2]["payload"] == unknown
    assert not instance.consumer.step()
    assert instance._event_queue.get_nowait().raw_data["t"] == "GROUP_AT_MESSAGE_CREATE"


async def test_message_dedupe_and_ack_failure_do_not_enqueue_twice(receiver, monkeypatch):
    owner, instance = receiver
    payload = accept(owner, instance)
    real_ack = owner.inbox.acknowledge
    def fail(*args):
        raise sqlite3.OperationalError("fixture")
    monkeypatch.setattr(owner.inbox, "acknowledge", fail)
    with pytest.raises(sqlite3.Error):
        instance.consumer.step()
    assert instance._event_queue.qsize() == 1 and owner.inbox.count(instance.identity.settings_key) == 1
    monkeypatch.setattr(owner.inbox, "acknowledge", real_ack)
    assert instance.consumer.step()
    payload["id"] = "different-envelope-same-message"
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    assert instance.consumer.step() and instance._event_queue.qsize() == 1
    event = instance._event_queue.get_nowait()
    event.cleanup_temporary_local_files()
    # Same author/text with a different real message ID is not deduplicated.
    accept(owner, instance, message_id="legit-repeat")
    assert instance.consumer.step() and instance._event_queue.qsize() == 1


async def test_host_queue_watermark_and_client_close_do_not_clear_shared_observations(receiver):
    owner, instance = receiver
    accept(owner, instance)
    for _ in range(owner.delivery_slots.queue_limit):
        instance._event_queue.put_nowait(None)
    assert not instance.consumer.step()
    while not instance._event_queue.empty():
        instance._event_queue.get_nowait()
    assert instance.consumer.step()
    await instance.client.close()
    row = owner.messages.lookup(instance.identity.robot, "member_openid", "group:group-one", "user-one")
    assert row["source_message_id"] == "msg-one"
