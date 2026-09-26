"""A bounded C2C typing notification; no renewal or undocumented remote clear."""
import asyncio
from urllib.parse import quote

from ..errors import V2Error, unsupported
from ..extensions.state import digest
from ..protocol import RequestSpec
from .outbound import AMBIGUOUS_CODES, EXPIRED_CODES
from .reply import ACTIVE_FALLBACK_CODES, definite_source_rejection


class TypingCore:
    def __init__(self, sender, *, settings=lambda: {}):
        self.sender, self.settings = sender, settings
        self.jobs, self.closed = {}, False

    async def start(self, route, source, *, seconds=10):
        if self.closed:
            raise V2Error("service_stopped", "Typing is stopped.", status=503)
        self.sender.check(route, source)
        if route.scene != "c2c" or not self.settings().get("typing_enabled", False):
            raise unsupported("Typing requires C2C and the explicit advanced setting.")
        if source is None:
            raise V2Error("passive_source_required", "Typing never falls back to an active notification.")
        if type(seconds) is not int or not 1 <= seconds <= 60:
            raise V2Error("invalid_typing_duration", "Typing duration must be 1..60 seconds.")
        now = self.sender.store.now()
        self.jobs = {k: v for k, v in self.jobs.items() if not v[0].done() or v[1] > now}
        key = route, source.message_id or ("event", source.event_id)
        if key not in self.jobs:
            if len(self.jobs) >= 32:
                raise V2Error("typing_capacity", "Too many typing leases.", status=429)
            task = asyncio.create_task(self._notify(route, source, seconds), name="qq-v2-typing")
            task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
            self.jobs[key] = task, min(source.expires, now + seconds)
        task = self.jobs[key][0]
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await self.stop(route, source=source)
            raise

    async def _notify(self, route, source, seconds):
        store = self.sender.store
        op_id = "typing-" + digest([route.encode(), source.message_id, source.event_id if source.message_id is None else None])
        operation = store.reserve(route, source, digest(["typing", seconds]), op_id)
        if operation["state"] == "sent":
            return operation["result"]
        attempted = False
        deadline = min(source.expires, store.now() + seconds)
        try:
            duration = min(seconds, int(deadline - store.now()))
            if duration < 1:
                raise V2Error("reply_expired", "Typing source has expired.")
            body = {"msg_type": 6, "input_notify": {"input_type": 1, "input_second": duration}, "msg_seq": operation["seq"]}
            body["msg_id" if source.message_id is not None else "event_id"] = source.message_id if source.message_id is not None else source.event_id
            def prepare():
                nonlocal attempted
                self.sender.check(route, source)
                self.sender.connected(route)
                remaining = int(deadline - store.now())
                if remaining < 1:
                    raise V2Error("typing_expired", "The typing lease expired before its wire attempt.")
                body["input_notify"]["input_second"] = remaining
                store.prepare_attempt(route, source, op_id)
                attempted = True
            async with asyncio.timeout(5):
                response = await self.sender.http.request(RequestSpec(route.robot.environment, "POST", "/v2/users/" + quote(route.target, safe="") + "/messages", json_body=body), before_send=prepare)
            if response.data is not None and not isinstance(response.data, dict):
                raise V2Error("invalid_typing_response", "Typing response is invalid.", status=502, phase="result_unknown")
            result = {"kind": "typing", "state": "notified", "operation_id": op_id, "expires_at": deadline}
            store.finish(route.robot, op_id, "sent", result=result)
            return result
        except BaseException as exc:
            phase = getattr(exc, "phase", "result_unknown" if attempted else "not_sent")
            if attempted and (getattr(exc, "business_code", None) in AMBIGUOUS_CODES or getattr(exc, "http_status", None) == 408 or (getattr(exc, "http_status", None) or 0) >= 500):
                phase = "result_unknown"
            if isinstance(exc, V2Error) and phase == "rejected" and definite_source_rejection(exc, attempted) and exc.business_code in EXPIRED_CODES:
                store.block_source(route, source, exc.business_code, allow_active=exc.business_code in ACTIVE_FALLBACK_CODES)
            store.finish(route.robot, op_id, {"not_sent": "not_sent", "rejected": "rejected"}.get(phase, "unknown"), error={"code": "typing_failed", "phase": phase})
            if isinstance(exc, V2Error):
                raise V2Error(exc.code, str(exc), retcode=exc.retcode, status=exc.status, business_code=exc.business_code,
                    trace_id=exc.trace_id, retry_after=exc.retry_after, http_status=exc.http_status, phase=phase, operation_id=op_id) from None
            if isinstance(exc, asyncio.CancelledError):
                exc.phase = phase
                raise
            raise V2Error("typing_failed", "Typing failed; inspect the operation before retrying.", status=503, phase=phase, operation_id=op_id) from None

    async def stop(self, route=None, *, source=None):
        source_id = source.message_id or ("event", source.event_id) if source is not None else None
        keys = [key for key in self.jobs if (route is None or key[0] == route) and (source_id is None or key[1] == source_id)]
        tasks = [self.jobs.pop(key)[0] for key in keys]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self):
        self.closed = True
        await self.stop()
