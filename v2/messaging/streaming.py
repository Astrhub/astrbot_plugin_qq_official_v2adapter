"""One durable logical reply per stream, with separately bounded wire fragments."""
import asyncio
from urllib.parse import quote
from uuid import uuid4

from astrbot.core.message.components import Plain
from astrbot.core.message.message_event_result import MessageChain

from ..errors import V2Error, unsupported
from ..extensions.state import digest
from ..models import text_id
from ..protocol import RequestSpec
from ..transport.http import retry_delay
from .outbound import AMBIGUOUS_CODES, EXPIRED_CODES, build_body, parse_message


class StreamingCore:
    def __init__(self, sender, state, *, settings=lambda: {}, sleep=asyncio.sleep, timeout_factory=asyncio.timeout):
        self.sender, self.state, self.settings, self.sleep = sender, state, settings, sleep
        self.timeout_factory = timeout_factory
        self.tasks, self.closed = set(), False

    def mode(self, route, use_fallback=False):
        if type(use_fallback) is not bool:
            raise V2Error("invalid_stream", "Fallback selection must be boolean.")
        if route.scene == "c2c" and not use_fallback:
            return "native"
        if use_fallback or self.settings().get("stream_fallback", "aggregate") == "aggregate":
            return "aggregate"
        raise unsupported("This scene has no native stream; explicitly enable bounded aggregation.")

    def check(self, route, source):
        if self.closed:
            raise V2Error("service_stopped", "Streaming is stopped.", status=503)
        self.sender.check(route, source)
        self.sender.connected(route)
        if source:
            self.sender.store.check_source(route, source)
        else:
            self.sender.store.target(route)

    async def send(self, route, generator, *, source=None, use_fallback=False, input_mode="append", operation_id=None):
        self.check(route, source)
        mode = self.mode(route, use_fallback)
        if input_mode not in {"append", "replace"} or not hasattr(generator, "__aiter__"):
            raise V2Error("invalid_stream", "Use an asynchronous message-chain generator and append or replace mode.")
        if len(self.tasks) >= 8:
            raise V2Error("stream_capacity", "Too many pending streams.", status=429)
        policy = self.settings()
        maximum = policy.get("stream_max_chars", 4096)
        timeout = policy.get("stream_timeout", 120)
        op_id = text_id(operation_id) if operation_id is not None else uuid4().hex
        task = asyncio.current_task()
        iterator = generator.__aiter__()
        if not hasattr(iterator, "__anext__"):
            raise V2Error("invalid_stream", "The stream did not provide an asynchronous iterator.")
        self.tasks.add(task)
        total, format_md = "", None
        operation, last_id, last_result = None, None, None
        first_wire_started = None
        index, attempted, complete = 0, False, False
        self.last_mode = mode
        store = self.sender.store

        async def fragment(raw, final=False):
            nonlocal index, last_id, last_result, attempted
            self.check(route, source)
            body = {"input_mode": input_mode, "input_state": 10 if final else 1, "index": index,
                    "content_type": "markdown" if format_md else "text", "content_raw": raw}
            if source:
                body["msg_id" if source.message_id is not None else "event_id"] = source.message_id if source.message_id is not None else source.event_id
                body["msg_seq"] = operation["seq"]
            if last_id:
                body["stream_msg_id"] = last_id
            path = "/v2/users/" + quote(route.target, safe="") + "/stream_messages"
            def before_send():
                nonlocal attempted, first_wire_started
                self.check(route, source)
                store.prepare_attempt(route, source, op_id, continuation=index > 0)
                attempted = True
                if index == 0:
                    first_wire_started = store.now()
            def validate(data):
                if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not 1 <= len(data["id"]) <= 512 or last_id and data["id"] != last_id:
                    raise V2Error("invalid_stream_response", "Stream response lacks its real stable message ID.", status=502, phase="result_unknown")
                remain = data.get("remain_msg_len")
                if remain is not None and (type(remain) is not int or not 0 <= remain <= 2147483647):
                    raise V2Error("invalid_stream_response", "Stream remaining-length metadata is invalid.", status=502, phase="result_unknown")
                result = {"message_id": data["id"], "operation_id": op_id, "msg_seq": operation["seq"], "mode": "native", "index": index, "remain_msg_len": remain}
                if isinstance(data.get("timestamp"), str) and len(data["timestamp"]) <= 80:
                    result["timestamp"] = data["timestamp"]
                ext = data.get("ext_info")
                if isinstance(ext, dict) and isinstance(ext.get("ref_idx"), str) and ext["ref_idx"]:
                    result["ref_idx"] = text_id(ext["ref_idx"])
                return result
            for retry in range(2):
                try:
                    last_result = await self.state.execute(self.sender.http, RequestSpec(route.robot.environment, "POST", path, json_body=body),
                        op_id="stream-" + digest([op_id, index, retry]), kind="stream_frame", validate=validate, before_send=before_send, ambiguous_codes=AMBIGUOUS_CODES | {50001},
                        context={"parent_operation_id": op_id, "index": index, "final": final, "stream_msg_id": last_id})
                    last_id = last_result["message_id"]
                    index += 1
                    return
                except V2Error as exc:
                    if retry or exc.phase != "rejected" or not (exc.http_status == 429 or exc.business_code == 50002):
                        raise
                    delay = retry_delay(exc.retry_after, retry)
                    if delay is None:
                        raise
                    await self.sleep(delay)

        try:
            async with self.timeout_factory(timeout):
                count = 0
                async for chain in iterator:
                    count += 1
                    if count > 4096:
                        raise V2Error("stream_too_large", "Stream fragment count exceeded its bound.")
                    self.check(route, source)
                    if not isinstance(chain, MessageChain):
                        raise V2Error("invalid_stream", "AstrBot streaming must yield MessageChain values.")
                    if chain.type not in (None, "plain", "break"):
                        raise unsupported("Private reasoning, tool and audio stream records are not public text fragments.")
                    if chain.type == "break":
                        chain = MessageChain([Plain("\n")]).use_markdown(format_md is True)
                    if not chain.chain:
                        continue
                    atoms, markdown = parse_message(chain)
                    if any(kind not in {"text", "markdown"} for kind, _ in atoms):
                        raise unsupported("Streams cannot silently drop media, mentions or references.")
                    raw = "".join(value for _, value in atoms)
                    if not raw:
                        continue
                    if format_md is not None and markdown != format_md:
                        raise V2Error("stream_format_changed", "A logical stream cannot switch text/Markdown format.")
                    format_md = markdown
                    candidate = total + raw if input_mode == "append" else raw
                    if index and not candidate.startswith(total):
                        raise V2Error("stream_prefix_changed", "Previously submitted stream content cannot be replaced.")
                    if len(candidate) > maximum or len(candidate.encode()) > 16384:
                        raise V2Error("stream_too_large", "Stream text exceeds its bounded message size.")
                    # Validate the complete prefix so split QQ directives cannot bypass markup checks.
                    if not candidate.strip():
                        total = candidate
                        continue
                    compiled = build_body(route, [("text", candidate)], markdown, store)
                    if mode == "native":
                        if operation is None:
                            operation = store.reserve(route, source, digest(["stream", input_mode, markdown, candidate]), op_id)
                            if operation["state"] == "sent":
                                raise V2Error("operation_already_attempted", "Query the retained stream result; finished streams are not regenerated.", status=409)
                        wire_total = compiled["markdown"]["content"] if markdown else compiled["content"]
                        previous = build_body(route, [("text", total)], markdown, store) if total.strip() else None
                        wire_previous = (previous["markdown"]["content"] if markdown else previous["content"]) if previous else ""
                        await fragment(wire_total if input_mode == "replace" else wire_total[len(wire_previous):])
                    total = candidate
                if not total.strip():
                    raise V2Error("stream_empty", "The generator produced no sendable content.")
                if mode == "aggregate":
                    result = await self.sender.send(route, MessageChain([Plain(total)]).use_markdown(format_md), source=source, operation_id=op_id)
                    complete = True
                    result = {**result, "mode": "aggregate"}
                    return result
                compiled = build_body(route, [("text", total)], format_md, store)
                final = compiled["markdown"]["content"] if format_md else compiled["content"]
                await fragment(final if input_mode == "replace" else "", final=True)
                result = {**last_result, "state": "sent", "wire_started": first_wire_started}
                store.finish(route.robot, op_id, "sent", result=result)
                complete = True
                return result
        except BaseException as exc:
            if getattr(exc, "business_code", None) in EXPIRED_CODES:
                store.block_source(route, source, exc.business_code)
            phase = getattr(exc, "phase", "result_unknown" if attempted else "not_sent")
            if last_id or getattr(exc, "business_code", None) == 50001:
                phase = "result_unknown"
            if operation is not None and operation["state"] != "sent" and not complete:
                phase = getattr(exc, "phase", "result_unknown" if attempted else "not_sent")
                if last_id or getattr(exc, "business_code", None) == 50001:
                    phase = "result_unknown"
                outcome = {"not_sent": "not_sent", "rejected": "rejected"}.get(phase, "unknown")
                store.finish(route.robot, op_id, outcome, error={"code": "stream_interrupted", "phase": phase, "partial_message_id": last_id, "next_index": index})
            if isinstance(exc, V2Error):
                raise V2Error(exc.code, str(exc), retcode=exc.retcode, status=exc.status, business_code=exc.business_code,
                    trace_id=exc.trace_id, retry_after=exc.retry_after, http_status=exc.http_status, phase=phase, operation_id=op_id,
                    details={"partial_message_id": last_id, "next_index": index, "frame_operation_id": exc.operation_id}) from None
            if isinstance(exc, asyncio.CancelledError):
                exc.phase = phase
                raise
            raise V2Error("stream_interrupted", "Stream generation or delivery failed; partial output must not be replayed.", status=503,
                          phase="result_unknown" if attempted else "not_sent", operation_id=op_id,
                          details={"partial_message_id": last_id, "next_index": index}) from None
        finally:
            try:
                if hasattr(iterator, "aclose"):
                    async with asyncio.timeout(1):
                        await iterator.aclose()
            except Exception:
                self.last_cleanup_error = "generator_close_failed"
                if complete:
                    result["cleanup_error"] = self.last_cleanup_error
            finally:
                self.tasks.discard(task)

    async def close(self):
        self.closed = True
        tasks = self.tasks - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
