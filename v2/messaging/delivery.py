"""Bounded admission to the host queue; admission is not handler completion."""
import asyncio
import sqlite3

from ..errors import V2Error
from ..models import text_id
from ..protocol import CHAT_EVENTS, RawEnvelope
from .convert import convert_chat


class DeliverySlots:
    def __init__(self, capacity=32, queue_limit=128):
        self.capacity, self.queue_limit = capacity, queue_limit
        self.events = set()

    def available(self, queue):
        return len(self.events) < self.capacity and queue.qsize() < self.queue_limit and not queue.full()

    def admit(self, event):
        if len(self.events) >= self.capacity:
            raise asyncio.QueueFull
        self.events.add(event)
        event.delivery_finished = lambda: self.events.discard(event)

    def close(self):
        # Host pipeline tasks remain host-owned; old send clients are revoked separately.
        for event in tuple(self.events):
            event.stop_event()


class ChatConsumer:
    def __init__(self, adapter, *, sleep=asyncio.sleep):
        self.adapter, self.sleep = adapter, sleep
        self.inbox, self.store = adapter.owner.inbox, adapter.owner.messages
        self.owner_key = adapter.identity.settings_key
        self.slots = adapter.owner.delivery_slots
        self.task = None
        self.closed = False
        self.state = "configured"
        self.last_error = None
        self.enqueued = set()

    def start(self):
        if self.task is None:
            self.task = asyncio.create_task(self.run(), name="qq-v2-chat-consumer")

    def step(self):
        self.adapter.check_generation()
        for item in self.inbox.pending(self.owner_key, 1, priority=getattr(self.adapter, "extensions", None) is not None):
            receipt, payload = item["receipt"], item["payload"]
            if not isinstance(payload, dict):
                self.inbox.retain(self.owner_key, receipt, "invalid_envelope", invalid=True)
                return True
            if payload.get("t") not in CHAT_EVENTS or payload.get("op") != 0:
                data = payload.get("d")
                if payload.get("op") == 0 and payload.get("t") == "READY" and isinstance(data, dict):
                    user = data.get("user", {})
                    try:
                        text_id(data.get("session_id"))
                        bot_id = text_id(user.get("id") if isinstance(user, dict) else None)
                    except V2Error:
                        self.inbox.retain(self.owner_key, receipt, "invalid_ready", invalid=True)
                        self.state = "connection_notice_invalid"
                        return True
                    self.adapter.bot_id = bot_id
                    self.inbox.acknowledge(self.owner_key, receipt)
                    self.state = "connection_notice_handled"
                    return True
                if payload.get("op") == 0 and payload.get("t") == "RESUMED":
                    self.inbox.acknowledge(self.owner_key, receipt)
                    self.state = "connection_notice_handled"
                    return True
                extensions = getattr(self.adapter, "extensions", None)
                if extensions and payload.get("op") == 0:
                    try:
                        handled = extensions.accept(payload, item["received_at"])
                    except V2Error as exc:
                        if exc.code in {"extension_capacity", "extension_state_full", "service_stopped", "stale_generation"}:
                            raise
                        self.inbox.retain(self.owner_key, receipt, exc.code, invalid=True)
                        self.last_error, self.state = exc.code, "extension_quarantined"
                        return True
                    if handled:
                        self.inbox.acknowledge(self.owner_key, receipt)
                        self.state = "extension_dispatched"
                        return True
                self.inbox.retain(self.owner_key, receipt, "non_chat_event")
                self.state = "extension_retained"
                return True
            try:
                chat = convert_chat(self.adapter.identity, RawEnvelope(payload, item["received_at"]),
                                    isolated=self.adapter.session_isolated, bot_id=self.adapter.bot_id)
                if self.store.delivered(chat) or receipt in self.enqueued:
                    if not self.store.delivered(chat):
                        self.store.mark_delivered(chat)
                    self.inbox.acknowledge(self.owner_key, receipt)
                    self.enqueued.discard(receipt)
                    return True
                if not self.slots.available(self.adapter._event_queue):
                    self.state = "backpressured"
                    return False
                self.store.observe(chat)
            except V2Error as exc:
                if exc.code in {"message_state_full", "service_stopped"}:
                    raise
                self.inbox.retain(self.owner_key, receipt, exc.code, invalid=True)
                self.last_error, self.state = exc.code, "chat_quarantined"
                return True
            event = self.adapter.create_event(chat.message)
            self.slots.admit(event)
            unpin = self.store.pin_delivery(chat)
            release_slot = event.delivery_finished
            def finished():
                unpin()
                release_slot()
            event.delivery_finished = finished
            try:
                self.adapter.commit_event(event)
            except BaseException:
                event.delivery_finished()
                raise
            # No await between queue admission and the durable delivery marker.
            self.enqueued.add(receipt)
            self.store.mark_delivered(chat)
            self.inbox.acknowledge(self.owner_key, receipt)
            self.enqueued.discard(receipt)
            self.state, self.last_error = "delivered_to_host", None
            return True
        self.state = "idle"
        return False

    async def run(self):
        while not self.closed:
            try:
                self.adapter.check_generation()
                if self.adapter.runtime_status()["online"] or self.adapter.state == "webhook_ready":
                    if self.step():
                        await self.sleep(0)
                        continue
            except asyncio.QueueFull:
                self.state = "backpressured"
            except sqlite3.Error:
                self.last_error, self.state = "message_storage_unavailable", "backpressured"
            except V2Error as exc:
                if exc.code == "stale_generation":
                    raise
                self.last_error, self.state = exc.code, "backpressured"
            self.inbox.changed.clear()
            try:
                await asyncio.wait_for(self.inbox.changed.wait(), timeout=0.25)
            except TimeoutError:
                # Retry released host slots even without a new inbox notification.
                pass

    async def close(self):
        self.closed = True
        if self.task and self.task is not asyncio.current_task():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
