"""Owned QQ connections; transport readiness is separate from P3 message delivery."""

import asyncio
import copy
import uuid

from astrbot.core.platform.platform import Platform, PlatformStatus
from astrbot.core.platform.platform_metadata import PlatformMetadata

from . import PLATFORM_TYPE
from .client import V2Client
from .errors import V2Error
from .media.service import MediaService
from .extensions.management import Management
from .extensions.interactions import ExtensionDispatcher
from .messaging.delivery import ChatConsumer
from .messaging.outbound import SendingCore
from .messaging.streaming import StreamingCore
from .messaging.typing import TypingCore
from .messaging.store import IdentityView
from .models import InstanceKey, SessionRoute
from .transport.http import HTTPTransport
from .transport.inbox import Ingress
from .transport.webhook import Webhook
from .transport.websocket import Gateway

DEFAULT_PLATFORM_CONFIG = {
    "id": "qq_v2", "type": PLATFORM_TYPE, "enable": False,
    "appid": "", "secret": "", "environment": "production",
    "transport": "websocket", "intents": 33554432, "shard": [0, 1],
    "unified_webhook_mode": False, "webhook_uuid": "",
}


class V2Adapter(Platform):
    owner = None

    def __init__(self, platform_config, platform_settings, event_queue):
        identity = InstanceKey.from_config(platform_config)
        if not isinstance(platform_config.get("secret"), str) or not platform_config["secret"]:
            raise V2Error("missing_credentials", "Configure AppID and secret in the host platform form.")
        if self.owner is None or self.owner.stopping:
            raise V2Error("service_stopped", "Plugin owner is unavailable.", status=503)
        for instance in self.owner.instances:
            if instance.identity.platform_id == identity.platform_id or instance.identity.receive_key == identity.receive_key:
                raise V2Error("duplicate_receiver", "This platform or robot shard already has an instance.", status=409)
            if instance.identity.robot == identity.robot and (identity.transport == "webhook" or instance.identity.transport == "webhook" or instance.identity.shard[1] != identity.shard[1]):
                raise V2Error("duplicate_receiver", "Do not mix webhook/WS or inconsistent shard counts for one robot.", status=409)
        if identity.transport == "webhook":
            if identity.shard != (0, 1):
                raise V2Error("invalid_shard", "Webhook does not support WS sharding.")
            try:
                uuid.UUID(platform_config.get("webhook_uuid", ""))
                if platform_config.get("unified_webhook_mode") is not True:
                    raise ValueError
                if any(c.get("webhook_uuid") == platform_config["webhook_uuid"] and c.get("id") != identity.platform_id
                       for c in self.owner.context.get_config().get("platform", [])):
                    raise ValueError
            except (ValueError, AttributeError):
                raise V2Error("invalid_webhook_config", "Save a unique V2 unified webhook configuration before loading.") from None
        super().__init__(copy.deepcopy(platform_config), event_queue)
        self.identity = identity
        self._terminated = False
        self._revoked = False
        self._run_task = None
        self._stop = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._resource_closing = None
        self._termination = None
        self.started = asyncio.Event()
        self.ready = asyncio.Event()
        self.state = "configured"
        self.failure = None
        self.failure_details = None
        self.local_settings = self.owner.store.get(identity.settings_key)["applied"]
        self.config_fingerprint = self.owner.control.fingerprint(platform_config)
        self.client = V2Client(identity)
        self.bot_id = ""
        self.session_isolated = platform_settings.get("unique_session", False) is True
        self.client._state.cache = IdentityView(self.owner.messages, identity.robot)
        self.consumer = ChatConsumer(self)
        self.client._state.guard = self.check_generation
        self.client._state.status = self.runtime_status
        self.http = HTTPTransport(identity, platform_config["secret"], guard=self.check_generation)
        self.client._state.http = self.http
        self.ingress = Ingress(self.owner.inbox, identity.settings_key, guard=self.check_generation)
        self.gateway = Gateway(self.http, self.ingress, guard=self.check_generation) if identity.transport == "websocket" else None
        self.webhook = Webhook(identity.robot.appid, platform_config["secret"], self.ingress, guard=self.check_generation) if identity.transport == "webhook" else None
        self.media = MediaService(identity, self.http, self.owner.extension_state, self.owner.media_pool,
            settings=lambda: self.owner.store.get(identity.settings_key)["applied"].get("extensions", {}), guard=self.check_generation)
        self.sender = SendingCore(identity, self.http, self.owner.messages, guard=self.check_generation,
            is_online=lambda: self.runtime_status()["online"] or self.state == "webhook_ready",
            ws_online=lambda: bool(self.gateway and self.gateway.online), media=self.media)
        self.client._state.sender = self.sender
        self.streaming = StreamingCore(self.sender, self.owner.extension_state, settings=self.media.settings)
        self.typing = TypingCore(self.sender, settings=self.media.settings)
        self.client._state.streaming, self.client._state.typing = self.streaming, self.typing
        self.management = Management(identity, self.http, self.owner.extension_state, self.owner.messages, settings=self.media.settings)
        self.client._state.management = self.management
        self.client._state.extension_state = self.owner.extension_state
        self.ack_http = HTTPTransport(identity, platform_config["secret"], guard=self.check_generation, token_provider=self.http.token)
        self.extensions = ExtensionDispatcher(self, self.ack_http)
        self.client._state.extensions = self.extensions
        self.owner.instances.add(self)

    def check_generation(self):
        current = [c for c in self.owner.context.get_config().get("platform", []) if c.get("id") == self.identity.platform_id]
        if (self._terminated or self._revoked or self.owner.stopping or len(current) != 1
                or self.owner.control.fingerprint(current[0]) != self.config_fingerprint):
            self._revoked = True
            raise V2Error("stale_generation", "Connection configuration changed; reload the platform.", status=409)

    def runtime_status(self):
        valid = not self._revoked and not self._terminated and not self.owner.stopping
        current = [c for c in self.owner.context.get_config().get("platform", []) if c.get("id") == self.identity.platform_id]
        same = len(current) == 1 and self.owner.control.fingerprint(current[0]) == self.config_fingerprint
        online = valid and same and bool(self.gateway.online if self.gateway else self.webhook.online)
        state = self.gateway.state if self.gateway and self.state == "connecting" else self.state
        if not same or self._revoked:
            state = "reload_required"
        if self._terminated:
            state = "stopped"
        return {"online": online, "good": online and self.consumer.state != "backpressured" and not self.consumer.last_error and not self.sender.storage_failed, "state": state, "failure": self.failure,
                "send_storage_failed": self.sender.storage_failed,
                "failure_details": self.failure_details,
                "last_transport_failure": self.gateway.last_failure if self.gateway else None,
                "platform_id": self.identity.platform_id, "generation": self.identity.generation,
                "transport": self.identity.transport, "message_delivery": self.consumer.state,
                "delivery_error": self.consumer.last_error,
                "raw_disposition": self.owner.inbox.diagnostics(self.identity.settings_key) if not self.owner.inbox.closed else {},
                "pending_raw": self.owner.inbox.count(self.identity.settings_key) if not self.owner.inbox.closed else None,
                "challenge_answered": bool(self.webhook and self.webhook.challenge_answered),
                "last_transport_error": self.gateway.last_error if self.gateway else None,
                "webhook_evidence": "recent_authenticated_callback" if self.webhook and online else "not_proven"}

    def revoke(self):
        self._revoked = True
        self._stop.set()
        if self._run_task and self._run_task is not asyncio.current_task():
            self._run_task.cancel()

    async def _watch_config(self):
        while True:
            await asyncio.sleep(1)
            try:
                self.check_generation()
            except V2Error:
                self.revoke()
                raise

    async def run(self):
        self._run_task = asyncio.current_task()
        tasks = set()
        try:
            self.check_generation()
            self.state = "connecting"
            self.ingress.start()
            self.consumer.start()
            tasks.add(self.consumer.task)
            self.started.set()
            watch = asyncio.create_task(self._watch_config(), name="qq-v2-generation-watch")
            tasks.add(watch)
            if self.gateway:
                tasks.add(asyncio.create_task(self.gateway.run(), name="qq-v2-gateway"))
            else:
                await self.http.token()
                self.state = "webhook_ready"
                self.ready.set()
                tasks.add(asyncio.create_task(self._stop.wait(), name="qq-v2-webhook-lifetime"))
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except V2Error as exc:
            self.state, self.failure = "failed", exc.code
            self.failure_details = exc.as_dict()
            raise
        except Exception:
            self.state, self.failure = "failed", "transport_failure"
            raise V2Error("transport_failure", "QQ transport failed; inspect configuration and reload.", status=503) from None
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            await self._close_resources()
            self.started.set()

    async def _close_resources(self):
        if self._resource_closing is None or (self._resource_closing.done() and (self._resource_closing.cancelled() or self._resource_closing.exception() is not None)):
            self._resource_closing = asyncio.create_task(self._close_resources_owned(), name="qq-v2-resources-close")
            self._resource_closing.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._resource_closing)

    async def _close_resources_owned(self):
        async with self._close_lock:
            async def close_one(service):
                try:
                    async with asyncio.timeout(2):
                        await (service.aclose() if service is self.webhook else service.close())
                except Exception:
                    return type(service).__name__
                return None
            failures = await asyncio.gather(*(close_one(s) for s in (self.consumer, self.extensions, self.management, self.streaming, self.typing, self.sender, self.media, self.webhook, self.gateway, self.ingress, self.http) if s))
            if any(failures):
                self.failure = "cleanup_failed"
                raise V2Error("cleanup_failed", "Some QQ resources exceeded or failed cleanup: " + ", ".join(f for f in failures if f), status=503)

    async def terminate(self):
        self._terminated = True
        self.revoke()
        if self._termination is None or (self._termination.done() and (self._termination.cancelled() or self._termination.exception() is not None)):
            self._termination = asyncio.create_task(self._terminate_owned(asyncio.current_task()), name="qq-v2-instance-close")
            self._termination.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._termination)

    async def _terminate_owned(self, caller):
        if self._run_task and self._run_task is not caller:
            await asyncio.gather(self._run_task, return_exceptions=True)
        await self._close_resources()
        await self.client.close()
        self.status = PlatformStatus.STOPPED
        self.owner.instances.discard(self)

    def meta(self):
        return PlatformMetadata(PLATFORM_TYPE, "QQ 官方 V2（基础消息与持久状态）", self.identity.platform_id,
                                support_streaming_message=True, support_proactive_message=True)

    def get_client(self):
        return self.client

    def create_event(self, message):
        from .event import V2MessageEvent
        route = SessionRoute.decode(message.session_id)
        return V2MessageEvent(message, self.meta(), self.client, route)

    async def send_by_session(self, session, message_chain):
        if session.platform_id != self.identity.platform_id:
            raise V2Error("identity_mismatch", "Session belongs to another platform.", status=409)
        await self.client.send(SessionRoute.decode(session.session_id), message_chain)
        await super().send_by_session(session, message_chain)

    def unified_webhook(self):
        return self.identity.transport == "webhook" and super().unified_webhook()

    async def webhook_callback(self, request):
        if not self.webhook or self._terminated or self._revoked or self.state != "webhook_ready":
            return {"code": "webhook_not_ready"}, 503
        return await self.webhook.handle(request)
