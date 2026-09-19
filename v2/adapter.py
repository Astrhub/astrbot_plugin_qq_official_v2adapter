"""AstrBot platform skeleton; a missing transport is an error, never online."""

from astrbot.core.platform.platform import Platform, PlatformStatus
from astrbot.core.platform.platform_metadata import PlatformMetadata

from . import PLATFORM_TYPE
from .client import V2Client
from .errors import V2Error, not_ready, unsupported
from .models import InstanceKey, SessionRoute

DEFAULT_PLATFORM_CONFIG = {
    "id": "qq_v2", "type": PLATFORM_TYPE, "enable": False,
    "appid": "", "secret": "", "environment": "production",
    "transport": "websocket", "intents": 33554432, "shard": [0, 1],
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
            if (instance.identity.platform_id == identity.platform_id
                    or instance.identity.receive_key == identity.receive_key):
                raise V2Error("duplicate_receiver", "This platform or robot shard already has an instance.", status=409)
        super().__init__(platform_config, event_queue)
        self.identity = identity
        self.client = V2Client(identity)
        self._terminated = False
        # Load persisted local state before claiming ownership; a corrupt DB fails closed.
        self.local_settings = self.owner.store.get(identity.settings_key)["applied"]
        self.config_fingerprint = self.owner.control.fingerprint(platform_config)
        self.owner.instances.add(self)

    async def run(self):
        self.client.check()
        raise not_ready()

    async def terminate(self):
        if self._terminated:
            return
        self._terminated = True
        await self.client.close()
        self.status = PlatformStatus.STOPPED
        self.owner.instances.discard(self)

    def meta(self):
        return PlatformMetadata(PLATFORM_TYPE, "QQ 官方 V2（骨架，传输未实现）", self.identity.platform_id,
                                support_streaming_message=False, support_proactive_message=False)

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

    async def webhook_callback(self, request):
        raise unsupported("Webhook validation and ingestion are P2 work.")
