import sqlite3
from importlib import import_module

import pytest
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver
from test_messaging_state import NOW, chat_payload, observed

from v2.errors import V2Error
from v2.messaging.store import MessageStore
from v2.protocol import RawEnvelope


@pytest.fixture
def cache(tmp_path):
    clock = [NOW]
    store = MessageStore(tmp_path / "history.db", clock=lambda: clock[0], source_capacity=2)
    yield store, clock
    store.close()


@pytest.mark.parametrize("event", ["GROUP_MESSAGE_CREATE", "AT_MESSAGE_CREATE"])
async def test_rolling_history_caches_do_not_stop_real_delivery(receiver, event):
    owner, instance = receiver
    clock = [NOW]
    owner.messages.clock = owner.inbox.clock = lambda: clock[0]
    owner.messages.source_capacity = 2
    key = instance.identity.settings_key
    for number in range(12):
        payload = chat_payload(event, message_id=f"rolling-{number}", timestamp=clock[0])
        owner.inbox.accept(key, RawEnvelope(payload, clock[0]))
        assert instance.consumer.step()
        delivered = instance._event_queue.get_nowait()
        assert delivered.message_obj.message_id == f"rolling-{number}"
        delivered.cleanup_temporary_local_files()
        clock[0] += 301
        assert owner.messages.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] <= 4
        assert owner.messages.db.execute("SELECT count(*) FROM refs").fetchone()[0] <= 4
    assert owner.inbox.count(key) == 0 and not owner.messages._delivery_pins
    ids = [row[0] for row in owner.messages.db.execute("SELECT message_id FROM deliveries ORDER BY accepted")]
    assert ids == [f"rolling-{n}" for n in range(8, 12)]


def test_delivery_lru_preserves_unknown_sources_and_live_pipeline_pins(cache, config):
    store, clock = cache
    unknown = observed(config, message_id="unknown-source")
    store.observe(unknown)
    store.reserve(unknown.route, unknown.source, "digest", "unknown")
    store.mark_in_flight(unknown.route.robot, "unknown")
    store.finish(unknown.route.robot, "unknown", "unknown")
    store.mark_delivered(unknown)
    active = observed(config, message_id="active-pipeline")
    release = store.pin_delivery(active)
    store.mark_delivered(active)
    for number in range(10):
        clock[0] += 1
        store.mark_delivered(observed(config, message_id=f"history-{number}"))
    assert store.delivered(unknown) and store.delivered(active)
    assert store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 4
    clock[0] += 86401
    store.prune()
    assert store.delivered(unknown) and store.delivered(active)
    assert store.operation(unknown.route.robot, "unknown")["state"] == "unknown"
    assert tuple(store.db.execute("SELECT seq,used FROM sources").fetchone()) == (1, 1)
    release()
    release()
    store.prune()
    assert not store.delivered(active) and store.delivered(unknown)
    assert not store._delivery_pins


def test_delivery_lru_keeps_valid_sources_and_duplicate_marks_do_not_evict(cache, config):
    store, clock = cache
    valid = observed(config)
    store.observe(valid)
    store.mark_delivered(valid)
    for number in range(8):
        clock[0] += 1
        store.mark_delivered(observed(config, message_id=f"history-{number}"))
    assert store.delivered(valid)
    before = [tuple(row) for row in store.db.execute("SELECT * FROM deliveries ORDER BY accepted,rowid")]
    store.observe(valid)
    store.mark_delivered(valid)
    assert [tuple(row) for row in store.db.execute("SELECT * FROM deliveries ORDER BY accepted,rowid")] == before


def test_only_protected_delivery_records_backpressure_until_release(cache, config):
    store, _ = cache
    releases = []
    for number in range(4):
        chat = observed(config, message_id=f"active-{number}")
        releases.append(store.pin_delivery(chat))
        store.mark_delivered(chat)
    newcomer = observed(config, message_id="new")
    with pytest.raises(V2Error) as exc:
        store.mark_delivered(newcomer)
    assert exc.value.code == "message_state_full"
    assert store.db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 4
    releases[0]()
    store.mark_delivered(newcomer)
    assert store.delivered(newcomer)
    for release in releases:
        release()
    assert not store._delivery_pins


def test_reference_cache_evicts_oldest_and_misses_fail_explicitly(cache, config):
    store, clock = cache
    first = observed(config, message_id="first")
    store.observe(first)
    clock[0] = first.source.expires
    for number in range(3):
        chat = observed(config, message_id=f"ref-{number}")
        store.observe(chat)
        clock[0] += 1
    assert store.db.execute("SELECT count(*) FROM refs").fetchone()[0] == 4
    with pytest.raises(V2Error) as exc:
        store.reference(first.route, "first")
    assert exc.value.code == "reference_not_observed"
    assert store.reference(chat.route, chat.source.message_id) == chat.source.ref_idx


async def test_cache_admission_failure_does_not_duplicate_queued_event(receiver, plugin_module):
    owner, instance = receiver
    error_type = import_module(plugin_module.__package__ + ".v2.errors").V2Error
    clock = [NOW]
    owner.messages.clock = owner.inbox.clock = lambda: clock[0]
    owner.messages.source_capacity = 1
    key = instance.identity.settings_key
    for number in range(3):
        payload = chat_payload("AT_MESSAGE_CREATE", message_id=f"in-flight-{number}", timestamp=clock[0])
        owner.inbox.accept(key, RawEnvelope(payload, clock[0]))
        if number < 2:
            assert instance.consumer.step()
        else:
            with pytest.raises(error_type) as exc:
                instance.consumer.step()
            assert exc.value.code == "message_state_full"
        clock[0] += 301
    assert instance._event_queue.qsize() == 3 and owner.inbox.count(key) == 1
    first = instance._event_queue.get_nowait()
    first.cleanup_temporary_local_files()
    assert instance.consumer.step()
    assert instance._event_queue.qsize() == 2 and owner.inbox.count(key) == 0
    while not instance._event_queue.empty():
        instance._event_queue.get_nowait().cleanup_temporary_local_files()
    assert not owner.messages._delivery_pins


async def test_failed_host_commit_releases_delivery_pin(receiver, monkeypatch):
    owner, instance = receiver
    payload = chat_payload()
    owner.inbox.accept(instance.identity.settings_key, RawEnvelope(payload, NOW))
    def fail(event):
        raise RuntimeError("fixture rejected queue admission")
    monkeypatch.setattr(instance, "commit_event", fail)
    with pytest.raises(RuntimeError, match="queue admission"):
        instance.consumer.step()
    assert not owner.messages._delivery_pins and not owner.delivery_slots.events


def test_delivery_eviction_rolls_back_if_new_marker_cannot_commit(cache, config):
    store, _ = cache
    for number in range(4):
        store.mark_delivered(observed(config, message_id=f"kept-{number}"))
    before = [tuple(row) for row in store.db.execute("SELECT * FROM deliveries ORDER BY rowid")]
    store.db.execute("CREATE TRIGGER fail_new_delivery BEFORE INSERT ON deliveries BEGIN SELECT RAISE(ABORT, 'fixture marker failure'); END")
    with pytest.raises(sqlite3.Error):
        store.mark_delivered(observed(config, message_id="cannot-commit"))
    assert [tuple(row) for row in store.db.execute("SELECT * FROM deliveries ORDER BY rowid")] == before


def test_pipeline_pin_is_scoped_and_safe_to_release_after_close(cache, config):
    store, _ = cache
    pinned = observed(config, message_id="same-id")
    other = observed({**config, "appid": "another-app"}, message_id="same-id")
    release = store.pin_delivery(pinned)
    store.mark_delivered(pinned)
    store.mark_delivered(other)
    for number in range(4):
        store.mark_delivered(observed(config, message_id=f"new-{number}"))
    assert store.delivered(pinned) and not store.delivered(other)
    store.close()
    release()
    assert not store._delivery_pins
