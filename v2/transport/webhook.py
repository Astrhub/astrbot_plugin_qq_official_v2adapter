"""Official raw-body Ed25519 verification on the host's unified webhook route."""

import asyncio
import hashlib
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from ..errors import V2Error
from ..protocol import ExpiringSet, RawEnvelope


def signing_key(secret):
    seed = secret.encode("utf-8")
    if not seed:
        raise V2Error("missing_credentials", "Webhook signing secret is missing.")
    return Ed25519PrivateKey.from_private_bytes((seed * ((32 + len(seed) - 1) // len(seed)))[:32])


async def bounded_body(request, limit=1024 * 1024):
    """AstrBot 4.28.1 DashboardRequest wraps a Starlette Request with streaming support."""
    try:
        length = request.headers.get("content-length")
        if length is not None and (int(length) < 0 or int(length) > limit):
            raise V2Error("request_too_large", "Callback body exceeds the limit.", status=413)
    except ValueError:
        raise V2Error("invalid_request", "Invalid Content-Length.") from None
    raw = getattr(request, "_request", request)
    if not hasattr(raw, "stream"):
        raise V2Error("host_contract_mismatch", "Host request streaming interface is unavailable.", status=503)
    body = bytearray()
    async with asyncio.timeout(5):
        async for chunk in raw.stream():
            if len(body) + len(chunk) > limit:
                raise V2Error("request_too_large", "Callback body exceeds the limit.", status=413)
            body.extend(chunk)
    return bytes(body)


class Webhook:
    def __init__(self, appid, secret, ingress, *, guard=lambda: None, clock=time.time):
        self.appid, self.ingress, self.guard, self.clock = appid, ingress, guard, clock
        self._key = signing_key(secret)
        self.replays = ExpiringSet(capacity=4096, ttl=300, clock=clock)
        self.last_authenticated = None
        self.challenge_answered = False
        self.active = 0
        self.stopped = False
        self.tasks = set()

    @property
    def online(self):
        return not self.stopped and self.last_authenticated is not None and self.clock() - self.last_authenticated < 300

    def _timestamp(self, value):
        try:
            if not isinstance(value, str) or not value.isascii() or not value.isdigit() or len(value) > 12:
                raise ValueError
            if not self.clock() - 300 <= int(value) <= self.clock() + 60:
                raise ValueError
        except (ValueError, TypeError):
            raise V2Error("stale_signature", "Signature timestamp is outside the accepted window.", status=401) from None

    async def handle(self, request):
        if self.stopped:
            return {"code": "service_stopped"}, 503
        if self.active >= 16 or self.ingress.inbox.callback_active >= 32:
            return {"code": "callback_capacity"}, 503
        self.active += 1
        self.ingress.inbox.callback_active += 1
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            self.guard()
            if request.method != "POST":
                raise V2Error("method_not_allowed", "QQ callbacks require POST.", status=405)
            if request.headers.get("X-Bot-Appid") != self.appid:
                raise V2Error("appid_mismatch", "Callback AppID does not match this instance.", status=401)
            body = await bounded_body(request)
            self.guard()
            if self.stopped:
                raise V2Error("service_stopped", "Webhook is stopped.", status=503)
            envelope = RawEnvelope.parse(body)
            timestamp = request.headers.get("X-Signature-Timestamp")
            signature = request.headers.get("X-Signature-Ed25519")
            is_challenge = envelope.payload["op"] == 13
            if timestamp is not None or signature is not None or not is_challenge:
                self._timestamp(timestamp)
                try:
                    if not isinstance(signature, str) or len(signature) != 128:
                        raise ValueError
                    sig = bytes.fromhex(signature)
                    if len(sig) != 64 or sig[63] & 224:
                        raise ValueError
                    self._key.public_key().verify(sig, timestamp.encode() + body)
                except (ValueError, InvalidSignature):
                    raise V2Error("invalid_signature", "Callback signature is invalid.", status=401) from None
            self.guard()
            if is_challenge:
                # Official op13 can be unsigned; answering it is NOT proof of QQ reachability.
                data = envelope.payload.get("d")
                if not isinstance(data, dict) or not isinstance(data.get("plain_token"), str) or not 1 <= len(data["plain_token"]) <= 1024:
                    raise V2Error("invalid_challenge", "Invalid validation challenge.")
                nonce = data["plain_token"]
                if timestamp is None and signature is None and (not nonce.isascii() or any(not (c.isalnum() or c in "_-.") for c in nonce)):
                    # An unsigned challenge must not become a signing oracle for a JSON dispatch body.
                    raise V2Error("invalid_challenge", "Unsigned validation requires an opaque ASCII nonce.")
                self._timestamp(data.get("event_ts"))
                signed = self._key.sign((data["event_ts"] + data["plain_token"]).encode()).hex()
                self.challenge_answered = True
                return {"plain_token": data["plain_token"], "signature": signed}, 200
            if envelope.payload["op"] != 0:
                raise V2Error("unsupported_opcode", "Only dispatch and validation callbacks are supported.")
            replay = hashlib.sha256(timestamp.encode() + body).digest()
            if not self.replays.contains(replay):
                async with asyncio.timeout(5):
                    await self.ingress.accept(envelope)
                self.guard()
                self.replays.add(replay)
            self.last_authenticated = self.clock()
            # HTTP receipt only; no interaction acknowledgement or command execution.
            return {"op": 12}, 200
        except asyncio.CancelledError:
            if self.stopped:
                return {"code": "service_stopped"}, 503
            raise
        except V2Error as exc:
            return {"code": exc.code}, exc.status
        except TimeoutError:
            return {"code": "callback_timeout"}, 503
        except (ValueError, TypeError):
            return {"code": "invalid_callback"}, 400
        finally:
            self.active -= 1
            self.ingress.inbox.callback_active -= 1
            self.tasks.discard(task)

    def close(self):
        self.stopped = True
        self._key = None
        self.replays.items.clear()
        for task in self.tasks - {asyncio.current_task()}:
            task.cancel()

    async def aclose(self):
        self.close()
        tasks = self.tasks - {asyncio.current_task()}
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
