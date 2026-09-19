"""One authoritative host platform configuration, with explicit save/reload separation."""

import asyncio
import copy
import json
import secrets
import uuid
from pathlib import Path

from . import PLATFORM_TYPE
from .errors import V2Error
from .models import InstanceKey, text_id

EDITABLE = {"appid", "environment", "transport", "intents", "shard", "enable"}
DEFAULT_CONNECTION = {"type": PLATFORM_TYPE, "enable": False, "appid": "", "secret": "",
                      "environment": "production", "transport": "websocket", "intents": 33554432, "shard": [0, 1]}


class Connections:
    def __init__(self, owner):
        self.owner = owner
        self.lock = asyncio.Lock()
        self.reload_tasks = {}
        self.reload_results = {}

    def _config(self):
        return self.owner.context.get_config()

    def current(self, platform_id):
        text_id(platform_id)
        if any(c in platform_id for c in ":!"):
            raise V2Error("invalid_id", "Platform ID cannot contain ':' or '!'.")
        rows = [p for p in self._config().get("platform", []) if p.get("id") == platform_id]
        if len(rows) > 1 or rows and rows[0].get("type") != PLATFORM_TYPE:
            raise V2Error("instance_not_owned", "Target is not a unique V2 configuration.", status=404)
        return copy.deepcopy(rows[0]) if rows else None

    def fingerprint(self, platform_id, value):
        return self.owner.control.fingerprint([platform_id, value])

    def view(self, platform_id):
        config = self.current(platform_id)
        existing_ids = {p.get("id") for p in self._config().get("platform", [])}
        self.reload_results = {k: v for k, v in self.reload_results.items() if k in existing_ids}
        value = {**DEFAULT_CONNECTION, "id": platform_id, **(config or {})}
        instance = next((i for i in self.owner.instances if i.identity.platform_id == platform_id), None)
        runtime = instance.runtime_status() if instance else {"state": "configured" if config else "not_configured", "online": False}
        reload_state = self.reload_results.get(platform_id, "not_requested")
        if reload_state == "connecting":
            if runtime.get("failure"):
                reload_state = "connection_failed_configuration_retained"
            elif runtime["online"]:
                reload_state = "online"
            elif runtime["state"] == "webhook_ready":
                reload_state = "webhook_ready_unverified"
        return {"platform_id": platform_id, "exists": config is not None,
                "fingerprint": self.fingerprint(platform_id, config),
                "fields": {k: value[k] for k in EDITABLE if k in value},
                "credentials_configured": bool(value.get("secret")),
                "webhook_path": f"/api/platform/webhook/{value['webhook_uuid']}" if value.get("webhook_uuid") else None,
                "runtime": runtime, "reload": reload_state,
                "save_semantics": "host-object revision, no cross-process atomic CAS"}

    def _read_disk_target(self, platform_id):
        config = self._config()
        path = getattr(config, "config_path", None)
        if not path:
            raise V2Error("host_contract_mismatch", "Host configuration persistence is unavailable.", status=503)
        try:
            # Read only at an explicitly authorized runtime write; never duplicate credentials in SQLite.
            with Path(path).open(encoding="utf-8-sig") as stream:
                root = json.load(stream)
            if root != dict(config):
                raise V2Error("config_conflict", "Host disk and runtime configuration differ; reconcile before saving.", status=409)
            matches = [p for p in root.get("platform", []) if p.get("id") == platform_id]
            if len(matches) > 1:
                raise ValueError
            return matches[0] if matches else None
        except (OSError, ValueError, TypeError, AttributeError):
            raise V2Error("host_config_unavailable", "Cannot verify the current host configuration on disk.", status=503) from None

    def checked(self, platform_id, fingerprint):
        current = self.current(platform_id)
        if not isinstance(fingerprint, str) or not secrets.compare_digest(fingerprint, self.fingerprint(platform_id, current)):
            raise V2Error("config_conflict", "Connection configuration changed; read it again.", status=409)
        if self._read_disk_target(platform_id) != current:
            raise V2Error("config_conflict", "Host disk and runtime target differ; reconcile them before writing.", status=409)
        return current

    def _validate(self, old, candidate):
        if type(candidate.get("enable")) is not bool:
            raise V2Error("invalid_config", "enable must be boolean.")
        # Disabled, unbound entries are useful onboarding targets; they must not be started.
        check = {**candidate, "appid": candidate.get("appid") or "unbound-config-validation"}
        InstanceKey.from_config(check)
        appid, secret = candidate.get("appid"), candidate.get("secret")
        if not isinstance(appid, str) or len(appid) > 128 or not isinstance(secret, str) or len(secret) > 512 or any(ord(c) < 33 for c in secret):
            raise V2Error("invalid_credentials", "Credentials have an invalid shape.")
        if secret and (all(c in "*•●…" for c in secret) or secret in {"[REDACTED]", "已配置"}):
            raise V2Error("invalid_credentials", "A credential mask is not a replacement secret.")
        if candidate["enable"] and (not appid or not secret):
            raise V2Error("missing_credentials", "An enabled platform requires AppID and secret.")
        if candidate["transport"] == "webhook":
            if list(candidate.get("shard", [0, 1])) != [0, 1]:
                raise V2Error("invalid_shard", "Webhook does not support WS sharding.")
            candidate["webhook_uuid"] = (old or {}).get("webhook_uuid") or uuid.uuid4().hex
            try:
                uuid.UUID(candidate["webhook_uuid"])
            except (ValueError, AttributeError):
                raise V2Error("invalid_webhook_uuid", "Restore a valid webhook UUID in the host configuration.") from None
            candidate["unified_webhook_mode"] = True
        else:
            candidate["unified_webhook_mode"] = False
        for other in self._config().get("platform", []):
            if other.get("id") == candidate["id"]:
                continue
            if candidate.get("webhook_uuid") and candidate.get("webhook_uuid") == other.get("webhook_uuid"):
                raise V2Error("webhook_uuid_conflict", "Webhook UUID already belongs to another platform.", status=409)
            if other.get("type") == PLATFORM_TYPE and candidate["enable"] and other.get("enable"):
                try:
                    other_identity = InstanceKey.from_config(other)
                except V2Error:
                    continue  # An invalid unrelated configuration cannot own an active receiver.
                identity = InstanceKey.from_config(candidate)
                if other_identity.robot == identity.robot and (other_identity.shard == identity.shard or
                        other_identity.shard[1] != identity.shard[1] or "webhook" in (identity.transport, other_identity.transport)):
                    raise V2Error("duplicate_receiver", "Another enabled V2 configuration conflicts with this robot's receiving mode or shard.", status=409)

    async def save(self, platform_id, fingerprint, patch, *, secret_action="keep", secret=None, confirm=False,
                   confirm_identity=False, confirm_secret=False, guard=lambda: None):
        async with self.lock:
            if self.owner.stopping:
                raise V2Error("service_stopped", "Plugin is stopped.", status=503)
            guard()
            old = self.checked(platform_id, fingerprint)
            if old is None and sum(p.get("type") == PLATFORM_TYPE for p in self._config().get("platform", [])) >= 256:
                raise V2Error("instance_capacity", "At most 256 V2 configurations can be managed.", status=409)
            if confirm is not True:
                raise V2Error("confirmation_required", "Confirm saving connection settings; this does not reload.")
            if not isinstance(patch, dict) or patch.keys() - EDITABLE:
                raise V2Error("invalid_config", "Only documented connection fields may be edited.")
            candidate = {**copy.deepcopy(DEFAULT_CONNECTION), **copy.deepcopy(old or {}), "id": platform_id, **copy.deepcopy(patch)}
            if old and old.get("appid") and any(candidate.get(k) != old.get(k, DEFAULT_CONNECTION[k]) for k in ("appid", "environment")) and confirm_identity is not True:
                raise V2Error("identity_confirmation_required", "Confirm rebinding the robot identity.")
            if secret_action not in {"keep", "replace", "clear"}:
                raise V2Error("invalid_config", "Unknown credential edit action.")
            if secret_action == "keep":
                if secret not in (None, ""):
                    raise V2Error("secret_confirmation_required", "Select replace before supplying a credential.")
            else:
                if confirm_secret is not True:
                    raise V2Error("secret_confirmation_required", "Explicitly confirm credential replacement or clearing.")
                if secret_action == "replace" and (not isinstance(secret, str) or not secret):
                    raise V2Error("invalid_credentials", "A replacement secret must be nonempty.")
                candidate["secret"] = secret if secret_action == "replace" else ""
            self._validate(old, candidate)
            # Synchronous revalidation + the public host save method: no await in this critical section.
            self.checked(platform_id, fingerprint)
            guard()
            config = self._config()
            platforms = config["platform"]
            previous = list(platforms)
            platforms[:] = [candidate if p.get("id") == platform_id else p for p in platforms]
            if old is None:
                platforms.append(candidate)
            try:
                config.save_config()
            except Exception:
                platforms[:] = previous
                raise V2Error("config_save_failed", "Host save failed; original runtime configuration retained. Reload disk state before retrying.", status=503) from None
            for instance in list(self.owner.instances):
                if instance.identity.platform_id == platform_id and instance.config_fingerprint != self.owner.control.fingerprint(candidate):
                    instance.revoke()
            self.reload_results[platform_id] = "saved_requires_reload"
            return self.view(platform_id)

    async def reload(self, platform_id, fingerprint, *, confirm=False):
        async with self.lock:
            if self.owner.stopping:
                raise V2Error("service_stopped", "Plugin is stopped.", status=503)
            if confirm is not True:
                raise V2Error("confirmation_required", "Confirm stopping the old generation and reloading.")
            config = self.checked(platform_id, fingerprint)
            if config is None:
                raise V2Error("instance_not_owned", "Create the target configuration first.", status=404)
            if platform_id in self.reload_tasks:
                raise V2Error("reload_in_progress", "This instance is already reloading.", status=409)
            if len(self.reload_tasks) >= 8:
                raise V2Error("reload_capacity", "Too many platform reloads are in progress.", status=429)
            self._validate(config, {**DEFAULT_CONNECTION, **copy.deepcopy(config)})
            self.reload_tasks[platform_id] = asyncio.current_task()
        try:
            async with asyncio.timeout(10):
                await self.owner.context.platform_manager.reload(config)
            if config["enable"] and not any(i.identity.platform_id == platform_id for i in self.owner.instances):
                raise V2Error("reload_failed", "Host did not load the saved configuration.", status=503)
            self.reload_results[platform_id] = "connecting" if config["enable"] else "disabled"
            return self.view(platform_id)
        except Exception:
            self.reload_results[platform_id] = "reload_failed_configuration_retained"
            raise V2Error("reload_failed", "Saved configuration is retained; inspect connection status before retrying.", status=503) from None
        finally:
            self.reload_tasks.pop(platform_id, None)

    async def close(self):
        tasks = set(self.reload_tasks.values()) - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def prepare_webhooks(self):
        config = self._config()
        before = copy.deepcopy(config.get("platform", []))
        updated = copy.deepcopy(before)
        changed = False
        seen = {}
        for p in updated:
            if p.get("type") == PLATFORM_TYPE and p.get("transport") == "webhook":
                if not p.get("webhook_uuid"):
                    p["webhook_uuid"] = uuid.uuid4().hex
                    changed = True
                if p.get("unified_webhook_mode") is not True:
                    p["unified_webhook_mode"] = True
                    changed = True
            identifier = p.get("webhook_uuid")
            if identifier:
                if identifier in seen and (p.get("type") == PLATFORM_TYPE or seen[identifier].get("type") == PLATFORM_TYPE):
                    raise V2Error("webhook_uuid_conflict", "Resolve duplicate V2 webhook UUIDs before enabling V2.", status=409)
                seen[identifier] = p
        if changed:
            config["platform"][:] = updated
            try:
                config.save_config()
            except Exception:
                config["platform"][:] = before
                raise V2Error("config_save_failed", "Unable to persist V2 webhook UUIDs.", status=503) from None
