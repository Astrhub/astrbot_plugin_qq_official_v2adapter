"""Durable extension intake and an ACK lane independent of ordinary sends and downloads."""
import asyncio
import json
from urllib.parse import quote

from ..errors import V2Error
from ..messaging.store import robot_key
from ..models import text_id
from ..protocol import RequestSpec
from .callbacks import CallbackTickets
from .events import ExtensionEvent
from .keyboard import TicketStore, make_keyboard
from .management import empty
from .projection import CommandProjection
from .state import digest


class ExtensionDispatcher:
    def __init__(self, adapter, ack_http, *, ack_timeout=2.5, event_capacity=32768):
        self.adapter, self.ack_http = adapter, ack_http
        self.event_capacity = event_capacity
        self.ack_timeout = ack_timeout
        self.timeout_factory = asyncio.timeout
        self.store = adapter.owner.messages
        self.state = adapter.owner.extension_state
        self.tickets = TicketStore(adapter)
        self.robot = robot_key(adapter.identity.robot)
        self.callbacks = CallbackTickets(adapter)
        self.tasks, self.intake, self.business = set(), set(), set()
        self.closed, self.last_error = False, None

    def update(self, key, *, ack=None, business=None, error=None):
        if self.store.closed:
            return
        with self.store.transaction():
            self.store.db.execute("UPDATE extension_events SET ack=coalesce(?,ack),business=coalesce(?,business),error=?,updated=? WHERE robot=? AND event_key=?",
                (ack, business, json.dumps(error) if error else None, self.store.now(), self.robot, key))

    def records(self, limit=32):
        if type(limit) is not int or not 1 <= limit <= 128:
            raise V2Error("invalid_limit", "Read at most 128 typed extension records.")
        return [{**dict(row), "metadata": json.loads(row["metadata"]), "error": json.loads(row["error"]) if row["error"] else None}
                for row in self.store.db.execute("SELECT event_key,name,received,metadata,ack,business,error,updated FROM extension_events WHERE robot=? ORDER BY received DESC,rowid DESC LIMIT ?", (self.robot, limit))]

    def accept(self, payload, received):
        if payload.get("t") not in {"INTERACTION_CREATE", "GROUP_JOIN_REQUEST"}:
            return False
        if self.closed:
            raise V2Error("service_stopped", "Extension dispatcher is stopped.", status=503)
        self.adapter.check_generation()
        event = ExtensionEvent.parse(self.adapter.identity, payload, received)
        key = "interaction:" + event.interaction_id if event.interaction_id else "join:" + digest([payload["d"][k] for k in ("group_openid", "member_openid", "join_request_id")])
        if event.name == "GROUP_JOIN_REQUEST" and payload["d"].get("auto_approved") is not None:
            self.adapter.management.observe_request(event.target, payload["d"], received=received)
        with self.store.transaction():
            old = self.store.db.execute("SELECT metadata FROM extension_events WHERE robot=? AND event_key=?", (self.robot, key)).fetchone()
            if old:
                previous = json.loads(old[0])
                if any(previous.get(field) != event.metadata().get(field) for field in ("interaction_type", "scene", "target", "actor")):
                    raise V2Error("extension_event_conflict", "A retained event ID changed its original scope.", status=409)
                return True
            if event.interaction_type in {11, 12} and min(event.sent_at, event.received_at) + 3 <= self.store.now():
                raise V2Error("interaction_expired", "The original interaction exceeded the end-to-end ACK deadline.", status=409)
            if len(self.intake) >= 8:
                raise V2Error("extension_capacity", "ACK/intake workers are full; the raw event remains pending.", status=503)
            self.store.db.execute("DELETE FROM extension_events WHERE updated<=? AND ack NOT IN ('pending','unknown') AND business NOT IN ('pending','queued','admitted','unknown')", (self.store.now() - 86400,))
            count = self.store.db.execute("SELECT count(*) FROM extension_events").fetchone()[0]
            if count >= self.event_capacity:
                self.store.db.execute("DELETE FROM extension_events WHERE rowid IN (SELECT rowid FROM extension_events WHERE ack NOT IN ('pending','unknown') AND business NOT IN ('pending','queued','admitted','unknown') AND json_extract(metadata,'$.sent_at')<=? ORDER BY received LIMIT ?)",
                    (self.store.now() - 300, count - self.event_capacity + 1))
            if self.store.db.execute("SELECT count(*) FROM extension_events").fetchone()[0] >= self.event_capacity:
                raise V2Error("extension_state_full", "Protected extension receipts hold their bounded capacity.", status=503)
            ack = "pending" if event.interaction_type in {11, 12} else "not_required"
            self.store.db.execute("INSERT INTO extension_events VALUES(?,?,?,?,?,?,?,?,?)", (self.robot, key, event.name, received, json.dumps(event.metadata()), ack, "pending", None, self.store.now()))
        if network := getattr(self.adapter, "network", None):
            network.observe_extension(event)
        task = asyncio.create_task(self.process(key, event), name="qq-v2-extension-dispatch")
        self.tasks.add(task)
        self.intake.add(task)
        def finished(t):
            self.tasks.discard(t)
            self.intake.discard(t)
            self.business.discard(t)
            if t.cancelled() and not self.store.closed and event.interaction_type in {11, 12}:
                try:
                    outcome = self.state.operation(self.adapter.identity.robot, "ack-" + digest(event.interaction_id))["state"]
                except V2Error:
                    outcome = "not_sent"
                self.update(key, ack=outcome, business="unknown", error={"code": "extension_cancelled"})
            if not t.cancelled():
                t.exception()
        task.add_done_callback(finished)
        return True

    async def acknowledge(self, key, event):
        op_id = "ack-" + digest(event.interaction_id)
        spec = RequestSpec(self.adapter.identity.robot.environment, "PUT", "/interactions/" + quote(event.interaction_id, safe=""), json_body={"code": 0})
        deadline = min(event.sent_at, event.received_at) + 3
        remaining = deadline - self.store.now()
        if remaining <= 0:
            self.update(key, ack="not_sent", business="not_executed", error={"code": "ack_deadline"})
            return False
        def before_send():
            self.adapter.check_generation()
            if self.store.now() >= deadline:
                raise V2Error("ack_deadline", "Interaction ACK exceeded its original deadline.", status=409)
        try:
            async with self.timeout_factory(min(self.ack_timeout, remaining)):
                await self.state.execute(self.ack_http, spec, op_id=op_id, kind="interaction_ack", priority=True,
                    before_send=before_send, validate=empty)
            self.update(key, ack="succeeded")
            return True
        except (TimeoutError, V2Error) as exc:
            try:
                outcome = self.state.operation(self.adapter.identity.robot, op_id)["state"]
            except V2Error:
                outcome = "not_sent"
            error = exc.as_dict() if isinstance(exc, V2Error) else {"code": "ack_deadline", "operation_id": op_id}
            self.update(key, ack=outcome, business="not_executed", error=error)
            self.last_error = error["code"]
            return False

    async def reply_interaction(self, interaction_id, code, *, operation_id=None, guard=lambda: None):
        """ACK only non-managed interactions through the existing durable ACK lane."""
        self.adapter.check_generation()
        interaction_id = text_id(interaction_id)
        if interaction_id.startswith("INTERACTION_CREATE:") or type(code) is not int or code not in range(6):
            raise V2Error("invalid_interaction", "Use d.id and one official result code 0..5.")
        key = "interaction:" + interaction_id
        op_id = text_id(operation_id) if operation_id is not None else "ack-" + digest(interaction_id)
        with self.store.transaction():
            row = self.store.db.execute("SELECT ack,metadata FROM extension_events WHERE robot=? AND event_key=?", (self.robot, key)).fetchone()
            if not row:
                raise V2Error("interaction_not_observed", "The current bot has no retained interaction.", status=404)
            metadata = json.loads(row["metadata"])
            if metadata.get("interaction_type") in (11, 12):
                raise V2Error("interaction_owned", "The core owns this button or menu ACK, even for invalid tickets.", status=409)
            prior = metadata.get("native_ack")
            if prior is not None:
                if prior["code"] != code:
                    raise V2Error("operation_conflict", "An interaction cannot change its ACK code.", status=409)
                if row["ack"] == "succeeded":
                    return {}
                raise V2Error("extension_result_unknown" if row["ack"] in ("pending", "unknown") else "operation_already_attempted",
                              "The retained interaction ACK cannot be replayed.", status=409)
            if row["ack"] != "not_required":
                raise V2Error("interaction_owned", "Another ACK owner already claimed this interaction.", status=409)
            deadline = min(metadata["sent_at"], metadata["received_at"]) + 300
            if self.store.now() >= deadline:
                raise V2Error("interaction_expired", "Interaction ACK window has expired.", status=409)
            metadata["native_ack"] = {"code": code, "operation_id": op_id}
            self.store.db.execute("UPDATE extension_events SET metadata=?,ack='pending',updated=? WHERE robot=? AND event_key=?",
                                  (json.dumps(metadata), self.store.now(), self.robot, key))
        spec = RequestSpec(self.adapter.identity.robot.environment, "PUT",
                           "/interactions/" + quote(interaction_id, safe=""), json_body={"code": code})
        captured = []
        def validate(data):
            if data not in (None, {}):
                raise V2Error("invalid_ack_response", "QQ ACK response is not empty.", status=502, phase="result_unknown")
            captured.append(data)
            return {"code": code}
        def before_send():
            guard()
            self.adapter.check_generation()
            if self.store.now() >= deadline:
                raise V2Error("interaction_expired", "Interaction ACK window has expired.", status=409)
        try:
            await self.state.execute(self.ack_http, spec, op_id=op_id, kind="interaction_ack", priority=True,
                                     validate=validate, before_send=before_send)
            if not captured:
                raise V2Error("operation_result_not_retained", "Interaction ACK payload was not retained.", status=410)
            self.update(key, ack="succeeded")
            return captured[0]
        except asyncio.CancelledError:
            self.update(key, ack="unknown")
            raise
        except V2Error as exc:
            try:
                retained = self.state.operation(self.adapter.identity.robot, op_id)["state"]
            except V2Error:
                retained = "not_sent"
            self.update(key, ack=retained if retained in ("unknown", "not_sent", "rejected") else "unknown")
            raise


    async def process(self, key, event):
        task = asyncio.current_task()
        try:
            try:
                if event.interaction_type in {11, 12} and not await self.acknowledge(key, event):
                    return
            finally:
                self.intake.discard(task)
            if event.name == "GROUP_JOIN_REQUEST":
                # Approval flags come from an explicit fresh application-list read, not chat observation.
                self.update(key, business="typed_notice")
                return
            if event.interaction_type != 11:
                self.update(key, business="typed_notice")
                return
            if len(self.business) >= 8:
                self.update(key, business="not_executed", error={"code": "callback_business_capacity"})
                return
            self.business.add(task)
            token = event.payload["d"].get("data", {}).get("resolved", {}).get("button_data")
            if isinstance(token, str) and token.startswith("qv2cb."):
                self.update(key, business="admitted")
                outcome = await self.callbacks.dispatch(event, token)
                self.update(key, business=outcome)
                return
            ticket = self.tickets.redeem(token, event, consume=False)
            slots = self.adapter.owner.delivery_slots
            if ticket["intent"] != "confirm" and not slots.available(self.adapter._event_queue):
                raise V2Error("delivery_backpressure", "The host queue is full; this click did not execute and its ticket remains available.", status=503)
            ticket = self.tickets.redeem(token, event)
            projected = CommandProjection(self.adapter, event, ticket, self.tickets)
            self.store.register_event_source(projected.bot._source)
            if ticket["intent"] == "confirm":
                await self.confirm(projected, ticket)
                self.update(key, business="confirmation_sent")
                return
            # The durable marker precedes queue admission; restart never re-executes a possibly admitted command.
            self.update(key, business="queued")
            slots.admit(projected)
            release = projected.delivery_finished
            def completed():
                release()
                self.update(key, business="finished_unconfirmed" if projected.command_admitted else "rejected",
                            error={"code": projected.projection_error or "command_not_admitted"} if not projected.command_admitted else None)
            projected.delivery_finished = completed
            try:
                self.adapter.commit_event(projected)
            except BaseException:
                release()
                self.update(key, business="not_executed")
                raise
        except asyncio.CancelledError:
            self.update(key, business="unknown", error={"code": "extension_cancelled"})
            raise
        except V2Error as exc:
            self.last_error = exc.code
            self.update(key, business="unknown" if exc.phase == "result_unknown" else "rejected", error=exc.as_dict())
        except Exception:
            self.last_error = "extension_processing_failed"
            self.update(key, business="unknown", error={"code": self.last_error})
        finally:
            self.tasks.discard(asyncio.current_task())
            self.intake.discard(task)
            self.business.discard(task)
            self.adapter.owner.inbox.changed.set()

    async def confirm(self, event, ticket):
        from ..help import render_help
        catalog = self.tickets.catalog(event.route)
        settings = self.adapter.owner.store.get(self.adapter.identity.settings_key)["applied"]
        page = render_help(catalog, settings, "home 0", admin=ticket["actor"] in self.tickets.config(event.route).get("admins_id", []), markdown=True)
        keyboard = make_keyboard(self.tickets, event, catalog, page, confirmation=ticket)
        await event.send_card("确认运行所选无参数指令；最终权限仍由正常管线校验。", keyboard)

    async def close(self):
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.ack_http.close()
