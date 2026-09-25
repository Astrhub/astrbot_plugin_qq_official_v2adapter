"""QQ envelope, request, replay and response contracts."""

import copy
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlsplit

from .errors import V2Error
from .models import RobotKey, text_id


@dataclass(frozen=True)
class RawEnvelope:
    payload: dict
    received_at: float

    @classmethod
    def parse(cls, raw: bytes, *, now=None):
        if len(raw) > 1024 * 1024:
            raise V2Error("event_too_large", "Event exceeds 1 MiB.")
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict) or type(payload.get("op")) is not int:
                raise ValueError
            if payload.get("id") is not None:
                text_id(payload["id"])
            if payload.get("s") is not None and type(payload["s"]) is not int:
                raise ValueError
            if payload.get("t") is not None and not isinstance(payload["t"], str):
                raise ValueError
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise V2Error("invalid_event", "Invalid QQ event envelope.") from exc
        return cls(copy.deepcopy(payload), time.time() if now is None else now)

    @property
    def event_id(self):
        return self.payload.get("id")

    @property
    def message_id(self):
        data = self.payload.get("d")
        return data.get("id") if isinstance(data, dict) and self.payload.get("t") in CHAT_EVENTS else None


CHAT_EVENTS = {
    "GROUP_AT_MESSAGE_CREATE": ("member_openid", "group", "member_openid"),
    "GROUP_MESSAGE_CREATE": ("member_openid", "group", "member_openid"),
    "C2C_MESSAGE_CREATE": ("user_openid", "c2c", "user_openid"),
    "AT_MESSAGE_CREATE": ("channel_user_id", "channel", "id"),
    "MESSAGE_CREATE": ("channel_user_id", "channel", "id"),
    "DIRECT_MESSAGE_CREATE": ("channel_user_id", "dm", "id"),
}


class ExpiringSet:
    def __init__(self, *, capacity=4096, ttl=300, clock=time.monotonic):
        if capacity < 1 or ttl <= 0:
            raise ValueError("capacity and ttl must be positive")
        self.capacity, self.ttl, self.clock = capacity, ttl, clock
        self.items = OrderedDict()

    def contains(self, key):
        expiry = self.items.get(key)
        if expiry is None:
            return False
        if expiry <= self.clock():
            del self.items[key]
            return False
        return True

    def add(self, key):
        self.items[key] = self.clock() + self.ttl
        self.items.move_to_end(key)
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)


def avatar_url(robot: RobotKey, openid: str, size=100):
    text_id(openid)
    if type(size) is not int or size not in (0, 100, 140, 640):
        raise V2Error("invalid_size", "Avatar size must be 0, 100, 140 or 640.")
    return f"https://q.qlogo.cn/qqapp/{quote(robot.appid, safe='')}/{quote(openid, safe='')}/{size}"


OPENAPI_BASE = "https://api.bot.qq.com"


def openapi_base(environment):
    if environment == "sandbox":
        raise V2Error("unsupported_environment", "Current official sandbox routing is unconfirmed; no network request is allowed.", status=501)
    if environment != "production":
        raise V2Error("invalid_environment", "Unknown QQ environment.")
    return OPENAPI_BASE


def validate_openapi_url(url, environment):
    base = urlsplit(openapi_base(environment))
    try:
        target = urlsplit(url)
        if (target.scheme != "https" or target.hostname != base.hostname or target.port not in (None, 443)
                or target.username is not None or target.password is not None or target.fragment
                or "\\" in url or any(ord(c) <= 32 for c in url)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise V2Error("invalid_request", "Only the documented QQ HTTPS origin is allowed.") from None


@dataclass(frozen=True)
class RequestSpec:
    environment: str
    method: str
    path: str
    params: dict | None = None
    json_body: object = None
    multipart: object = None

    @property
    def url(self):
        base = openapi_base(self.environment)
        if not isinstance(self.path, str) or not self.path.startswith("/") or self.path.startswith("//") or any(x in self.path for x in "?#\\\r\n"):
            raise V2Error("invalid_request", "Only instance-local QQ API paths are allowed.")
        if self.json_body is not None and self.multipart is not None:
            raise V2Error("invalid_request", "JSON and multipart are mutually exclusive.")
        query = urlencode(self.params or {}, doseq=True)
        url = base + self.path + ("?" + query if query else "")
        validate_openapi_url(url, self.environment)
        return url


def decode_response(status: int, body: bytes, headers: dict, *, phase="response_received", path=None):
    headers = {k.lower(): v for k, v in headers.items()}
    try:
        data = json.loads(body) if body else None
    except (ValueError, UnicodeError, RecursionError):
        data = None
        if 200 <= status < 300:
            raise V2Error("invalid_response", "QQ returned non-JSON content.", status=502, phase=phase,
                          http_status=status, trace_id=headers.get("x-tps-trace-id"), retry_after=headers.get("retry-after"))
    code = data.get("code") if isinstance(data, dict) else None
    if code is not None and type(code) is not int:
        raise V2Error("invalid_response", "QQ business code is not an integer.", status=502, phase=phase,
                      http_status=status, trace_id=headers.get("x-tps-trace-id"), retry_after=headers.get("retry-after"))
    if not 200 <= status < 300 or code not in (None, 0):
        if code not in (None, 0):
            phase = "rejected"
        if code == 40034128:
            error_code, message = "passive_quota_exhausted", "QQ rejected the passive reply time or count."
        elif (status == 429 or code == 40034100 or
              (code == 50002 and isinstance(path, str) and path.startswith("/v2/users/") and path.endswith("/stream_messages"))):
            error_code, message = "qq_rate_limited", "QQ rate limited the request."
        else:
            error_code, message = "qq_api_error", "QQ API rejected the request."
        # Do not echo untrusted bodies, URLs or tokens in error messages.
        error_status = status if status >= 400 else 502
        if error_code != "qq_api_error":
            error_status = 429
        raise V2Error(error_code, message, status=error_status,
                      business_code=code, trace_id=headers.get("x-tps-trace-id"),
                      retry_after=headers.get("retry-after"), phase=phase, http_status=status)
    return data
