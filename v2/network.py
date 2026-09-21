"""Optional instance-owned OneBot HTTP/forward-WS transport over the existing client."""
import asyncio
import json
import logging
import math
import re
import secrets
from urllib.parse import parse_qsl

from aiohttp import WSMsgType, web

from .client import (
    ACTION_PARAMS,
    LOCAL_ACTIONS,
    REMOTE_ACTIONS,
    UNSUPPORTED_ACTIONS,
    WRITE_ACTIONS,
)
from .errors import V2Error
from .models import text_id
from .network_config import network_config
from .network_events import LiveEvents, Peer

MAX_FRAME = 256 * 1024


def malformed():
    return V2Error("invalid_request", "Malformed, duplicate, conflicting or out-of-bound protocol input.")


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise malformed()
        result[key] = value
    return result


def json_loads(raw):
    try:
        if len(raw.encode() if isinstance(raw, str) else raw) > MAX_FRAME:
            raise malformed()
        def constant(value):
            raise malformed()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        result = json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)
        stack, count = [(result, 0)], 0
        while stack:
            item, depth = stack.pop()
            count += 1
            if depth > 20 or count > 4096 or isinstance(item, float) and not math.isfinite(item):
                raise malformed()
            if isinstance(item, dict):
                stack.extend((value, depth + 1) for value in item.values())
                for key in item:
                    key.encode("utf-8")
            elif isinstance(item, list):
                stack.extend((value, depth + 1) for value in item)
            elif isinstance(item, str):
                item.encode("utf-8")
        return result
    except (ValueError, UnicodeError, RecursionError, TypeError):
        raise malformed() from None


def form_loads(raw):
    try:
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise malformed()
        return pairs(parse_qsl(raw, keep_blank_values=True, strict_parsing=True, errors="strict", max_num_fields=128))
    except (ValueError, UnicodeError):
        raise malformed() from None


def normalize(action, params):
    if not isinstance(params, dict):
        raise malformed()
    result = dict(params)
    for key, kind in ACTION_PARAMS.get(action, {}).items():
        if key not in result:
            continue
        value = result[key]
        if kind == "bool":
            if value in ("true", "false"):
                value = value == "true"
            if type(value) is not bool:
                raise malformed()
        elif kind == "int":
            if isinstance(value, str) and re.fullmatch(r"-?(0|[1-9][0-9]{0,18})", value):
                value = int(value)
            if type(value) is not int or not -(2**63) <= value < 2**63:
                raise malformed()
        elif kind == "id":
            text_id(value)
        elif kind == "str" and not isinstance(value, str):
            raise malformed()
        result[key] = value
    if (isinstance(result.get("message"), str) and result.get("auto_escape") is not True
            and re.match(r"^\s*\[\s*(?:\{|\])", result["message"])):
        result["message"] = json_loads(result["message"])
        if not isinstance(result["message"], list):
            raise malformed()
    return result


class OneBotServer:
    max_peers = 16
    max_requests = 32
    queue_frames = 32
    peer_bytes = 1024 * 1024
    total_bytes = 4 * 1024 * 1024
    heartbeat = 15
    send_timeout = 5
    action_timeout = 120

    def __init__(self, adapter, config=None):
        self.adapter = adapter
        self.config = network_config(config)
        self.runner = self.site = None
        self.listening = False
        self.closed = False
        self.requests, self.peers = set(), set()
        self.cleanups = set()
        self.queued_bytes = self.disconnections = 0
        self.last_error = None
        self.events = LiveEvents(self)
        self._closing = None
        self.stopped = asyncio.Event()

    def check(self):
        self.adapter.check_generation()
        if self.closed or not self.listening or self.adapter.owner.config.get("onebot_network_enabled") is not True:
            raise V2Error("network_not_ready", "The OneBot listener is stopped or requires reload.", status=503)

    def status(self):
        return {"state": "listening" if self.listening and not self.closed else "stopped" if self.closed else "disabled",
                "host": self.config["host"], "port": self.config["port"], "writes": self.config["writes"],
                "active_ws_actions": sum(peer.action is not None and not peer.action.done() for peer in self.peers),
                "connections": len(self.peers), "requests": len(self.requests), "queued_bytes": self.queued_bytes,
                "reply_contexts": len(self.events.contexts), "disconnections": self.disconnections, "last_error": self.last_error}

    def capabilities(self):
        return {"support": "partial", **self.status(), "http": "GET/POST /:action[/] query/form/JSON",
                "websocket": {"/api": "actions", "/event": "events; inbound data ignored, no quick operations", "/": "mixed"},
                "authentication": "dedicated Bearer or query access_token; conflicting tokens rejected; no access logging",
                "events": "qq_event partial live OpenID views; known self_id lifecycle/heartbeat; no history replay",
                "missing_event_fields": ["font", "unobserved identity/profile/role", "private relationship/subtype"],
                "reply": "_qq_reply_context: observed source handle, expires with original source or reload/eviction",
                "idempotency": "_qq_operation_id only, echo is not an operation ID",
                "unsupported": ["reverse_ws", "http_post_events", "quick_operations", "cross_instance_routing", "strict_numeric_ids"],
                "limits": {"ws": self.max_peers, "requests": self.max_requests, "frame_bytes": MAX_FRAME,
                           "actions_per_ws": 1,
                           "queue_frames_per_peer": self.queue_frames, "bytes_per_peer": self.peer_bytes,
                           "json_depth": 20, "json_nodes": 4096, "action_timeout_seconds": self.action_timeout,
                           "input_types": "IDs stay strings; bool true/false; signed decimal integers; no coercion of numeric IDs",
                           "total_queue_bytes": self.total_bytes, "reply_contexts": self.events.capacity}}

    async def start(self):
        if self.closed:
            raise V2Error("network_not_ready", "A closed listener cannot be restarted; reload the instance.")
        if self.listening or not self.config["enable"] or self.adapter.owner.config.get("onebot_network_enabled") is not True:
            return
        self.adapter.check_generation()
        app = web.Application(client_max_size=MAX_FRAME)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        # Neither parser failures nor request URLs may log a query token.
        logger = logging.Logger("qq-v2-onebot", level=logging.CRITICAL + 1)
        self.runner = web.AppRunner(app, access_log=None, logger=logger, handler_cancellation=True,
                                    shutdown_timeout=1, keepalive_timeout=5, max_line_size=4096, max_field_size=4096)
        try:
            await self.runner.setup()
            self.site = web.TCPSite(self.runner, self.config["host"], self.config["port"], backlog=32, reuse_port=False)
            await self.site.start()
            self.listening = True
        except BaseException as exc:
            await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            self.last_error = "network_bind_failed"
            raise V2Error("network_bind_failed", "Cannot bind the configured OneBot endpoint; configuration retained.", status=503) from None

    def authenticate(self, request, query):
        headers = request.headers.getall("Authorization", [])
        supplied = query.pop("access_token", None)
        if len(headers) > 1:
            raise V2Error("auth_conflict", "Supply one authorization header.", retcode=1403, status=403)
        bearer = None
        if headers:
            parts = headers[0].split(" ")
            if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
                raise V2Error("invalid_token", "Invalid authorization.", retcode=1403, status=403)
            bearer = parts[1]
        if bearer is None and supplied is None:
            raise V2Error("missing_token", "Dedicated access token required.", retcode=1401, status=401)
        values = [v for v in (bearer, supplied) if v is not None]
        if any(not secrets.compare_digest(v.encode(), self.config["token"].encode()) for v in values):
            raise V2Error("invalid_token", "Invalid or conflicting access tokens.", retcode=1403, status=403)

    def contains_credential(self, value):
        stack = [value]
        while stack:
            item = stack.pop()
            if isinstance(item, str) and self.config["token"] and self.config["token"] in item:
                return True
            if isinstance(item, dict):
                if any(key.lower() in {"access_token", "authorization"} for key in item):
                    return True
                stack.extend(item.keys())
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)
        return False

    def encode(self, result):
        if self.contains_credential(result):
            result = {"post_type": "qq_event", "qq_type": "redacted", "reason": "credential_in_output"} if "post_type" in result else V2Error("credential_in_output", "Response withheld to protect authentication material.", phase="unknown").as_dict()
        try:
            frame = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            if len(frame.encode()) > MAX_FRAME:
                raise ValueError
            return frame
        except (ValueError, TypeError, RecursionError):
            raise V2Error("network_frame_exceeded", "Output exceeds the network frame contract; inspect retained operation state.", phase="unknown") from None

    def response(self, result):
        try:
            return self.encode(result)
        except V2Error as exc:
            error = exc.as_dict()
            if "echo" in result:
                error["echo"] = result["echo"]
            return self.encode(error)

    def execution_failure(self, code, params):
        operation_id = params.get("_qq_operation_id")
        phase = "unknown"
        if operation_id and self.adapter.client._state.sender:
            try:
                operation = self.adapter.client._state.sender.store.operation(self.adapter.identity.robot, operation_id)
                phase = "result_unknown" if operation["state"] == "unknown" else operation["state"]
            except V2Error:
                # Unavailable ledger details cannot prove the operation was never attempted.
                pass
        return V2Error(code, "Execution interrupted; inspect the retained operation before any retry.",
                       phase=phase, operation_id=operation_id).as_dict()

    async def action(self, action, params):
        if not isinstance(action, str):
            raise malformed()
        if action not in LOCAL_ACTIONS | REMOTE_ACTIONS | UNSUPPORTED_ACTIONS:
            raise V2Error("unknown_action", "No such explicit OneBot action.", retcode=1404, status=404)
        params = normalize(action, params)
        if self.contains_credential(params):
            raise malformed()
        try:
            self.check()
            if action in WRITE_ACTIONS and not self.config["writes"]:
                raise V2Error("network_read_only", "Explicit network write confirmation is required.", retcode=1403)
            async with asyncio.timeout(self.action_timeout):
                data = await self.adapter.client.call_action(action, **params)
            return {"status": "ok", "retcode": 0, "data": data}
        except V2Error as exc:
            result = exc.as_dict()
            if exc.code == "unsupported":
                result["retcode"] = 1501
            return result
        except TimeoutError:
            return self.execution_failure("action_timeout", params)

    async def http_params(self, request, query):
        if request.method not in {"GET", "POST"}:
            raise V2Error("invalid_method", "Only GET and POST actions are supported.", status=405)
        if request.method == "GET":
            if request.can_read_body:
                raise malformed()
            return query
        if (request.content_type not in {"application/json", "application/x-www-form-urlencoded"}
                or request.headers.get("Content-Encoding") or request.charset not in {None, "utf-8", "UTF-8"}):
            raise V2Error("unsupported_content_type", "Use UTF-8 JSON or urlencoded form data.", status=406)
        async with asyncio.timeout(10):
            raw = await request.read()
        body = json_loads(raw) if request.content_type == "application/json" else form_loads(raw.decode("utf-8"))
        if not isinstance(body, dict) or query.keys() & body.keys():
            raise malformed()
        return {**query, **body}

    async def handle(self, request):
        task = asyncio.current_task()
        try:
            query = form_loads(request.rel_url.raw_query_string)
            self.authenticate(request, query)
            if len(self.requests) >= self.max_requests:
                raise V2Error("network_capacity", "Active request capacity reached.", retcode=1429, status=200)
            self.requests.add(task)
            if request.headers.get("Upgrade", "").lower() == "websocket":
                self.check()
                if query or request.path not in {"/api", "/api/", "/event", "/event/", "/"} or request.method != "GET":
                    raise malformed()
                return await self.websocket(request)
            params = await self.http_params(request, query)
            path = request.path
            action = path[1:-1] if path.endswith("/") else path[1:]
            result = await self.action(action, params)
            return web.Response(text=self.response(result), content_type="application/json", headers={"Cache-Control": "no-store"})
        except V2Error as exc:
            return web.json_response(exc.as_dict(), status=exc.status, headers={"Cache-Control": "no-store"})
        except (ValueError, UnicodeError, web.HTTPRequestEntityTooLarge, TimeoutError):
            return web.json_response(malformed().as_dict(), status=400)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.last_error = "network_internal_error"
            return web.json_response(V2Error("network_internal_error", "Request failed; inspect retained operations.", phase="unknown").as_dict(), status=200)
        finally:
            self.requests.discard(task)

    async def websocket(self, request):
        if len(self.peers) >= self.max_peers:
            raise V2Error("network_capacity", "WebSocket connection capacity reached.", status=503)
        ws = web.WebSocketResponse(max_msg_size=MAX_FRAME, compress=False, heartbeat=30, timeout=1)
        peer = Peer(self, ws, request.path not in {"/api", "/api/"})
        self.peers.add(peer)
        tasks = set()
        try:
            await ws.prepare(request)
            peer.writer = asyncio.create_task(self.write_peer(peer), name="qq-v2-onebot-writer")
            tasks.add(peer.writer)
            tasks.add(asyncio.create_task(self.read_peer(peer, request.path not in {"/event", "/event/"}), name="qq-v2-onebot-reader"))
            if peer.events:
                peer.put(self.encode(self.events.meta("connect")))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if not task.cancelled():
                    try:
                        task.result()
                    except V2Error as exc:
                        peer.failure = self.last_error = exc.code
                    except Exception:
                        peer.failure = self.last_error = "network_internal_error"
        except web.HTTPException:
            raise malformed() from None
        finally:
            cleanup = asyncio.create_task(self.close_peer(peer, tasks), name="qq-v2-onebot-peer-close")
            self.cleanups.add(cleanup)
            cleanup.add_done_callback(self.cleanups.discard)
            await asyncio.shield(cleanup)
        return ws

    async def close_peer(self, peer, tasks):
        tasks = set(tasks)
        if peer.action is not None:
            tasks.add(peer.action)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        peer.action = None
        peer.clear()
        try:
            async with asyncio.timeout(2):
                if peer.ws.prepared:
                    await peer.ws.close(code=1013 if peer.failure else 1001, message=(peer.failure or "generation_closed").encode())
        except (TimeoutError, ConnectionError):
            self.last_error = "peer_close_timeout"
        finally:
            self.peers.discard(peer)

    async def read_peer(self, peer, actions):
        async for message in peer.ws:
            if peer.failure:
                return
            if message.type == WSMsgType.ERROR:
                peer.abort("invalid_ws_frame")
                return
            if not actions:
                continue  # /event never executes replies or quick operations.
            echo = {}
            try:
                if message.type != WSMsgType.TEXT:
                    raise malformed()
                body = json_loads(message.data)
                if not isinstance(body, dict) or self.contains_credential(body):
                    raise malformed()
                echo = {"echo": body["echo"]} if "echo" in body else {}
                if body.keys() - {"action", "params", "echo"}:
                    raise malformed()
                if peer.action is not None and not peer.action.done():
                    result = V2Error("network_action_capacity", "One action per WebSocket may execute at a time.", retcode=1429).as_dict()
                    if "echo" in body:
                        result["echo"] = body["echo"]
                    self.reply(peer, result)
                    continue
                # Keep receiving control/close frames while the single bounded action executes.
                peer.action = asyncio.create_task(self.answer(peer, body), name="qq-v2-onebot-action")
                peer.action.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            except V2Error as exc:
                self.reply(peer, {**exc.as_dict(), **echo})

    async def answer(self, peer, body):
        try:
            result = await self.action(body.get("action"), body.get("params", {}))
        except V2Error as exc:
            result = exc.as_dict()
        except asyncio.CancelledError:
            raise
        except Exception:
            result = V2Error("network_internal_error", "Request failed; inspect retained operations.", phase="unknown").as_dict()
        if "echo" in body:
            result["echo"] = body["echo"]
        self.reply(peer, result)

    def reply(self, peer, result):
        try:
            peer.put(self.response(result))
        except V2Error:
            peer.abort("response_frame_exceeded")

    async def write_peer(self, peer):
        loop = asyncio.get_running_loop()
        last_heartbeat = loop.time()
        while not peer.failure:
            # Heartbeats follow elapsed time, not only idle queue time.
            if peer.events and loop.time() - last_heartbeat >= self.heartbeat:
                self.check()
                peer.put(self.encode(self.events.meta("heartbeat")))
                last_heartbeat = loop.time()
                if peer.failure:
                    return
            timeout = max(0, last_heartbeat + self.heartbeat - loop.time()) if peer.events else self.heartbeat
            try:
                frame, size = await asyncio.wait_for(peer.queue.get(), timeout=timeout)
            except TimeoutError:
                self.check()
                continue
            try:
                self.check()
                async with asyncio.timeout(self.send_timeout):
                    await peer.ws.send_str(frame)
            except (TimeoutError, ConnectionError):
                peer.failure = self.last_error = "subscriber_stalled"
                self.disconnections += 1
                return
            finally:
                peer.bytes -= size
                self.queued_bytes -= size

    def observed(self):
        return self.listening and not self.closed and any(peer.events and not peer.failure for peer in self.peers)

    def publish(self, event):
        if not self.observed():
            return
        try:
            frame = self.encode(event)
        except V2Error:
            for peer in self.peers:
                if peer.events:
                    peer.abort("event_frame_exceeded")
            return
        for peer in self.peers:
            if peer.events:
                peer.put(frame)

    def observe_chat(self, chat):
        try:
            self.events.chat(chat)
        except Exception:
            self.observation_failed()

    def observe_extension(self, event):
        try:
            self.events.extension(event)
        except Exception:
            self.observation_failed()

    def observation_failed(self):
        self.last_error = "event_projection_failed"
        for peer in self.peers:
            if peer.events:
                peer.abort("event_projection_failed")

    def bind_context(self, key, action, params):
        return self.events.bind(key, action, params)

    def revoke(self):
        self.closed = True
        self.listening = False
        self.events.contexts.clear()
        for peer in self.peers:
            peer.abort("generation_closed")

    async def close(self):
        if self._closing is None:
            self.revoke()
            self._closing = asyncio.create_task(self._close(), name="qq-v2-onebot-close")
        await asyncio.shield(self._closing)

    async def _close(self):
        if self.site:
            await self.site.stop()
        tasks = self.requests - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.cleanups:
            await asyncio.gather(*self.cleanups, return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
        self.site = self.runner = None
        self.stopped.set()
