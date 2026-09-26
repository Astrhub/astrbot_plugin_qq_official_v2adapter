"""One bounded passive-to-active conversion, retaining the logical source binding."""
import copy

from ..errors import V2Error

ACTIVE_FALLBACK_CODES = {304103, 40034005, 40034026, 40034128}


def definite_source_rejection(error, attempted):
    return (attempted and error.phase == "rejected" and error.code != "token_refresh_failed"
            and error.http_status in {200, 400})


class ReplyModeChanged(V2Error):
    def __init__(self):
        super().__init__("reply_mode_changed", "The observed reply window expired before message submission.")


class ReplyDelivery:
    def __init__(self, store, route, source, operation):
        self.store, self.route, self.source = store, route, source
        self.operation_id, self.seq = operation["op_id"], operation["seq"]
        self.data = copy.deepcopy((operation["result"] or {}).get("delivery")) or {
            "mode": "passive" if source else "active", "reason": None,
            "attempts": [{"mode": "passive" if source else "active", "state": "not_sent", "wire_attempts": 0}]}

    @property
    def wire_source(self):
        return self.source if self.data["mode"] == "passive" else None

    def finish_attempt(self, outcome, error=None):
        attempt = self.data["attempts"][-1]
        attempt["state"] = outcome
        if error is not None:
            attempt.update(code=error.code, business_code=error.business_code, http_status=error.http_status, trace_id=error.trace_id)
        return copy.deepcopy(self.data)

    def switch(self, reason, *, outcome="not_sent", error=None):
        if not self.wire_source:
            raise V2Error("operation_already_attempted", "This logical send already selected active delivery.")
        self.finish_attempt(outcome, error)
        self.data.update(mode="active", reason=reason)
        self.data["attempts"].append({"mode": "active", "state": "not_sent", "wire_attempts": 0})
        self.store.switch_active(self.route, self.source, self.operation_id, self.data)
        self.seq = None

    def before_send(self, *, continuation=False, retried_auth=False):
        if retried_auth and not continuation and self.data["mode"] == "active" and self.data["reason"]:
            raise V2Error("active_auth_rejected", "QQ rejected active authentication; fallback is not resent.", phase="rejected", http_status=401, status=401)
        reason = self.store.reply_mode(self.route, self.source) if not continuation else None
        if reason and self.wire_source:
            self.switch(reason, outcome="rejected" if self.data["attempts"][-1]["wire_attempts"] else "not_sent")
            raise ReplyModeChanged()
        if not self.wire_source:
            self.store.target(self.route)
        try:
            self.store.prepare_attempt(self.route, self.wire_source, self.operation_id, continuation=continuation)
        except V2Error as exc:
            if exc.code == "reply_expired" and self.wire_source and not continuation:
                reason = self.store.reply_mode(self.route, self.source)
                if reason:
                    self.switch(reason, outcome="rejected" if retried_auth else "not_sent")
                    raise ReplyModeChanged() from None
            raise
        self.data["attempts"][-1].setdefault("wire_started", self.store.now())
        self.data["attempts"][-1]["state"] = "in_flight"
        self.data["attempts"][-1]["wire_attempts"] += 1
        self.store.save_delivery(self.route.robot, self.operation_id, self.data)

    def apply(self, body):
        if self.wire_source:
            source = self.wire_source
            body["msg_id" if source.message_id is not None else "event_id"] = source.message_id if source.message_id is not None else source.event_id
            if self.route.scene in {"group", "c2c"}:
                body["msg_seq"] = self.seq
        return body
