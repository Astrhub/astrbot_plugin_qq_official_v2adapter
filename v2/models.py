"""Immutable instance identity and reversible, robot-scoped sessions."""

import base64
import json
from dataclasses import dataclass, field
from uuid import uuid4

from .errors import V2Error

SCENES = ("c2c", "group", "channel", "dm")


def text_id(value):
    if not isinstance(value, str) or not value or len(value) > 512 or any(ord(c) < 32 for c in value):
        raise V2Error("invalid_id", "Expected a nonempty string identifier.")
    return value


@dataclass(frozen=True)
class RobotKey:
    appid: str
    environment: str = "production"

    def __post_init__(self):
        text_id(self.appid)
        if self.environment not in ("production", "sandbox"):
            raise V2Error("invalid_environment", "Use production or sandbox.")


@dataclass(frozen=True)
class InstanceKey:
    platform_id: str
    robot: RobotKey
    transport: str = "websocket"
    shard: tuple[int, int] = (0, 1)
    intents: int = 0
    generation: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    def from_config(cls, config):
        platform_id = text_id(config.get("id"))
        if any(c in platform_id for c in ":!"):
            raise V2Error("invalid_id", "Platform ID cannot contain ':' or '!'.")
        transport = config.get("transport", "websocket")
        shard = config.get("shard", [0, 1])
        intents = config.get("intents", 0)
        if transport not in ("websocket", "webhook"):
            raise V2Error("invalid_transport", "Unknown transport.")
        if (not isinstance(shard, (list, tuple)) or len(shard) != 2
                or any(type(n) is not int for n in shard) or not 0 <= shard[0] < shard[1] <= 1024):
            raise V2Error("invalid_shard", "Expected [index, count].")
        if type(intents) is not int or not 0 <= intents < 2**32:
            raise V2Error("invalid_intents", "Expected a uint32 intents mask.")
        return cls(platform_id, RobotKey(config.get("appid"), config.get("environment", "production")),
                   transport, tuple(shard), intents)

    @property
    def settings_key(self):
        return json.dumps([self.platform_id, self.robot.appid, self.robot.environment], separators=(",", ":"))

    @property
    def receive_key(self):
        # Different transports must not open duplicate receivers for the same shard.
        return self.robot, self.shard


@dataclass(frozen=True)
class SessionRoute:
    robot: RobotKey
    scene: str
    target: str
    user: str | None = None

    def __post_init__(self):
        if self.scene not in SCENES:
            raise V2Error("invalid_session", "Unknown scene.")
        text_id(self.target)
        if self.user is not None:
            text_id(self.user)

    def encode(self):
        data = [self.robot.appid, self.robot.environment, self.scene, self.target, self.user]
        return "v2." + base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")

    @classmethod
    def decode(cls, value):
        try:
            if not isinstance(value, str) or len(value) > 4096 or not value.startswith("v2."):
                raise ValueError
            raw = value[3:]
            data = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4), altchars=b"-_", validate=True))
            if not isinstance(data, list) or len(data) != 5:
                raise ValueError
            return cls(RobotKey(data[0], data[1]), *data[2:])
        except (ValueError, TypeError, UnicodeError) as exc:
            raise V2Error("invalid_session", "Invalid V2 session route.") from exc
