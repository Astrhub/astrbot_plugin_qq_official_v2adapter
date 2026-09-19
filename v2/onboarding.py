"""Server-owned QR leases; credentials are committed once through Connections."""

import asyncio
import base64
import json
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import aiohttp
import qrcode
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import V2Error
from .models import InstanceKey
from .protocol import openapi_base
from .transport.http import HTTPTransport, retry_delay

PORTAL = "https://q.qq.com"
ACTIVE = {"creating", "pending", "ready_to_commit"}


@dataclass
class Binding:
    ticket: str
    user: str
    platform_id: str
    fingerprint: str
    deadline: float
    lease: float
    state: str = "creating"
    key: bytes = field(default=b"", repr=False)
    credential: str = field(default="", repr=False)
    appid: str = ""
    remote_id: str = field(default="", repr=False)
    handle: str = field(default="", repr=False)
    qr: list | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    error: str | None = None
    committed: dict | None = None

    def erase(self):
        self.key = b""
        self.credential = self.remote_id = ""
        self.qr = None


class Onboarding:
    def __init__(self, owner, *, clock=time.monotonic, sleep=asyncio.sleep, session_factory=None, validator=None):
        self.owner, self.clock, self.sleep = owner, clock, sleep
        self.factory = session_factory or self._make_session
        self.validator = validator or self._validate_credentials
        self.session = None
        self.bindings = {}
        self.lock = asyncio.Lock()
        self.stopped = False

    def _make_session(self):
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), connector=aiohttp.TCPConnector(limit=4),
                                     trust_env=False, cookie_jar=aiohttp.DummyCookieJar(), auto_decompress=False)

    def _check(self, binding):
        if self.stopped or self.owner.stopping or binding.state not in ACTIVE:
            raise V2Error("binding_inactive", "Binding is no longer active.", status=409)
        if self.clock() >= min(binding.deadline, binding.lease):
            raise V2Error("binding_expired", "Binding lease expired; start again.", status=410)
        current = self.owner.connections.current(binding.platform_id)
        if self.owner.connections.fingerprint(binding.platform_id, current) != binding.fingerprint:
            raise V2Error("config_conflict", "Target connection changed during binding.", status=409)

    def _owned(self, user, platform_id, ticket):
        binding = self.bindings.get(ticket) if isinstance(ticket, str) else None
        if binding is None or binding.user != user or binding.platform_id != platform_id:
            raise V2Error("binding_not_owned", "Binding does not belong to this administrator and instance.", status=404)
        if binding.state in ACTIVE and self.clock() >= min(binding.deadline, binding.lease):
            binding.state = "expired"
            binding.erase()
            if binding.task:
                binding.task.cancel()
        return binding

    def view(self, binding):
        return {"ticket": binding.ticket, "platform_id": binding.platform_id, "state": binding.state,
                "appid": binding.appid or None, "expires_in": max(0, int(binding.deadline - self.clock())),
                "lease_seconds": max(0, int(binding.lease - self.clock())), "qr_matrix": binding.qr,
                "commit_handle": binding.handle if binding.state == "ready_to_commit" else None,
                "error": binding.error, "result": binding.committed}

    async def start(self, user, platform_id, fingerprint, *, confirm=False):
        async with self.lock:
            if self.stopped or self.owner.stopping:
                raise V2Error("service_stopped", "Onboarding is stopped.", status=503)
            if confirm is not True:
                raise V2Error("confirmation_required", "Confirm creating a QQ binding task.")
            current = self.owner.connections.checked(platform_id, fingerprint)
            openapi_base((current or {}).get("environment", "production"))
            for key, item in list(self.bindings.items()):
                if item.state in ACTIVE and self.clock() >= min(item.deadline, item.lease):
                    item.state = "expired"
                    item.erase()
                    if item.task:
                        item.task.cancel()
                if self.clock() >= item.deadline + 60 and (not item.task or item.task.done()):
                    item.erase()
                    del self.bindings[key]
            for item in self.bindings.values():
                if item.platform_id == platform_id and item.state in ACTIVE:
                    self._check(item)
                    if item.user == user and item.fingerprint == fingerprint:
                        return self.view(item)
                    raise V2Error("binding_in_progress", "Target already has an active binding lease.", status=409)
            if len(self.bindings) >= 16 or sum(b.state in ACTIVE for b in self.bindings.values()) >= 4:
                raise V2Error("binding_capacity", "Binding capacity reached; cancel or wait for lease expiry.", status=429)
            now = self.clock()
            binding = Binding(secrets.token_urlsafe(24), user, platform_id, fingerprint, now + 180, now + 30,
                              key=secrets.token_bytes(32))
            self.bindings[binding.ticket] = binding
            binding.task = asyncio.create_task(self._run(binding), name="qq-v2-binding")
            return self.view(binding)

    async def status(self, user, platform_id, ticket, *, renew=False):
        binding = self._owned(user, platform_id, ticket)
        if binding.state in ACTIVE:
            self._check(binding)
            if renew:
                binding.lease = min(binding.deadline, self.clock() + 30)
        return self.view(binding)

    async def cancel(self, user, platform_id, ticket):
        binding = self._owned(user, platform_id, ticket)
        async with self.lock:
            if binding.state == "configured":
                return self.view(binding)
            binding.state = "cancelled"
            binding.erase()
            if binding.task:
                binding.task.cancel()
        if binding.task:
            await asyncio.gather(binding.task, return_exceptions=True)
        return self.view(binding)

    async def _post(self, binding, action, data):
        self._check(binding)
        if action not in {"create_bind_task", "poll_bind_result"}:
            raise V2Error("invalid_binding_action", "Unsupported portal action.")
        if self.session is None:
            self.session = self.factory()
        try:
            async with asyncio.timeout(10):
                async with self.session.post(f"{PORTAL}/lite/{action}", json=data, allow_redirects=False,
                                             headers={"User-Agent": "AstrBot-QQ-V2", "Accept-Encoding": "identity"}) as response:
                    raw = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise V2Error("binding_response_too_large", "Portal response exceeds 64 KiB.", status=502)
                    self._check(binding)
                    if response.status != 200:
                        if response.status in {429, 500, 502, 503, 504}:
                            raise V2Error("binding_transient_error", "QQ portal is temporarily unavailable.", status=503,
                                          retry_after=response.headers.get("Retry-After"))
                        raise V2Error("binding_http_error", "QQ portal rejected the request.", status=502)
                    value = json.loads(raw)
                    if not isinstance(value, dict) or type(value.get("retcode")) is not int or value["retcode"] != 0 or not isinstance(value.get("data"), dict):
                        raise V2Error("binding_api_error", "QQ portal returned an invalid or rejected result.", status=502)
                    return value["data"]
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise V2Error("binding_network_error", "QQ binding request failed.", status=503) from None
        except (ValueError, UnicodeError, RecursionError):
            raise V2Error("binding_response_invalid", "QQ portal returned invalid JSON.", status=502) from None

    async def _validate_credentials(self, binding):
        current = self.owner.connections.current(binding.platform_id) or {}
        identity = InstanceKey.from_config({"id": binding.platform_id, **current, "appid": binding.appid})
        http = HTTPTransport(identity, binding.credential, guard=lambda: self._check(binding))
        try:
            await http.token()
        finally:
            await http.close()

    async def _run(self, binding):
        try:
            created = await self._post(binding, "create_bind_task", {"key": base64.b64encode(binding.key).decode()})
            remote_id = created.get("task_id")
            if not isinstance(remote_id, str) or not 1 <= len(remote_id) <= 256 or any(ord(c) < 32 for c in remote_id):
                raise V2Error("binding_response_invalid", "QQ portal omitted its task identifier.", status=502)
            binding.remote_id = remote_id
            url = "https://q.qq.com/qqbot/openclaw/connect.html?task_id=" + quote(remote_id, safe="") + "&_wv=2"
            qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, border=4)
            qr.add_data(url)
            qr.make(fit=True)
            binding.qr = qr.get_matrix()
            binding.state = "pending"
            failures = 0
            while binding.state == "pending":
                self._check(binding)
                await self.sleep(min(2, binding.lease - self.clock(), binding.deadline - self.clock()))
                try:
                    data = await self._post(binding, "poll_bind_result", {"task_id": binding.remote_id})
                    failures = 0
                except V2Error as exc:
                    if exc.code not in {"binding_network_error", "binding_transient_error"} or failures >= 2:
                        raise
                    delay = retry_delay(exc.retry_after, failures)
                    if delay is None:
                        raise
                    failures += 1
                    await self.sleep(delay)
                    continue
                status = data.get("status")
                if type(status) is not int or status not in {0, 1, 2, 3}:
                    raise V2Error("binding_response_invalid", "QQ returned an unknown binding status.", status=502)
                if status == 3:
                    raise V2Error("binding_expired", "QQ binding task expired.", status=410)
                if status != 2:
                    continue
                appid = data.get("bot_appid")
                if type(appid) is int:
                    appid = str(appid)
                encrypted = data.get("bot_encrypt_secret")
                try:
                    if not isinstance(appid, str) or not 1 <= len(appid) <= 128 or not isinstance(encrypted, str) or len(encrypted) > 2048:
                        raise ValueError
                    cipher = base64.b64decode(encrypted, validate=True)
                    if not 29 <= len(cipher) <= 1024:
                        raise ValueError
                    credential = AESGCM(binding.key).decrypt(cipher[:12], cipher[12:], None).decode("utf-8")
                    if not 1 <= len(credential) <= 512 or any(ord(c) < 33 for c in credential):
                        raise ValueError
                except Exception:
                    raise V2Error("binding_decryption_failed", "Binding credential failed authenticated decryption or validation.", status=502) from None
                binding.key = b""
                binding.appid, binding.credential = appid, credential
                data.clear()
                self._check(binding)
                await self.validator(binding)
                self._check(binding)
                binding.handle = secrets.token_urlsafe(32)
                binding.state = "ready_to_commit"
                binding.qr = None
            while binding.state == "ready_to_commit":
                self._check(binding)
                await self.sleep(min(2, binding.lease - self.clock(), binding.deadline - self.clock()))
        except asyncio.CancelledError:
            if binding.state in ACTIVE:
                binding.state = "cancelled"
        except V2Error as exc:
            binding.state = "expired" if exc.code == "binding_expired" else "failed"
            binding.error = exc.code
        except Exception:
            binding.state, binding.error = "failed", "binding_failed"
        finally:
            if binding.state not in ACTIVE:
                binding.erase()

    async def commit(self, user, platform_id, ticket, handle, *, confirm=False, confirm_identity=False, confirm_secret=False):
        async with self.lock:
            binding = self._owned(user, platform_id, ticket)
            if not isinstance(handle, str) or not binding.handle or not secrets.compare_digest(handle, binding.handle):
                raise V2Error("invalid_commit_handle", "Invalid one-time binding commit handle.", status=403)
            if binding.committed is not None:
                return self.view(binding)
            self._check(binding)
            if binding.state != "ready_to_commit":
                raise V2Error("binding_not_ready", "Binding has no verified credential to commit.", status=409)
            result = await self.owner.connections.save(platform_id, binding.fingerprint, {"appid": binding.appid, "enable": False},
                secret_action="replace", secret=binding.credential, confirm=confirm,
                confirm_identity=confirm_identity, confirm_secret=confirm_secret, guard=lambda: self._check(binding))
            binding.committed = result
            binding.state = "configured"
            binding.erase()
            if binding.task:
                binding.task.cancel()
            return self.view(binding)

    async def close(self):
        self.stopped = True
        tasks = []
        for binding in self.bindings.values():
            if binding.state in ACTIVE:
                binding.state = "cancelled"
            binding.erase()
            if binding.task:
                binding.task.cancel()
                tasks.append(binding.task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.session:
            await self.session.close()
        self.bindings.clear()
