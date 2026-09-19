"""Authenticated, instance-scoped local configuration and preview API."""

import asyncio
import hashlib
import hmac
import json
import secrets
import sqlite3
import time

from astrbot.api.web import json_response, request

from . import PLATFORM_TYPE, PLUGIN_NAME
from .commands import collect_catalog, layout_preview, panel_preview
from .errors import V2Error, unsupported
from .host_auth import require_admin
from .models import SCENES, InstanceKey
from .settings import DEFAULTS, merge_patch, validate_settings
from .transport.webhook import bounded_body

FLAGS = {"webui_enabled": True, "remote_menu_sync": False, "onebot_network_enabled": False}


class ControlAPI:
    def __init__(self, owner):
        self.owner = owner
        self.routes = []
        self.secret = secrets.token_bytes(32)
        self.lock = asyncio.Lock()
        self.active = 0

    def flags(self):
        return {k: self.owner.config.get(k, default) is True for k, default in FLAGS.items()}

    def fingerprint(self, value):
        return hmac.new(self.secret, json.dumps(value, sort_keys=True).encode(), hashlib.sha256).hexdigest()

    def csrf(self, username, expires=None):
        expires = int(time.time()) + 900 if expires is None else expires
        signature = self.fingerprint([username, expires, "control"])
        return f"{expires}.{signature}"

    def check_csrf(self, username, token):
        try:
            expiry = int(token.split(".", 1)[0])
            if not time.time() < expiry <= time.time() + 901 or not hmac.compare_digest(token, self.csrf(username, expiry)):
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            raise V2Error("csrf_expired", "Reload the page to obtain a management confirmation token.", status=403)

    def register(self):
        for path, method, operation in (("bootstrap", "GET", "bootstrap"), ("config", "GET", "get"),
                                       ("commands", "GET", "commands"), ("config/mutate", "POST", "mutate"),
                                       ("preview", "POST", "preview"), ("flags", "POST", "flags"),
                                       ("connection", "GET", "connection"), ("connection/save", "POST", "connection_save"),
                                       ("connection/reload", "POST", "connection_reload"),
                                       ("onboarding/start", "POST", "onboarding_start"), ("onboarding/status", "POST", "onboarding_status"),
                                       ("onboarding/cancel", "POST", "onboarding_cancel"), ("onboarding/commit", "POST", "onboarding_commit")):
            route = f"/{PLUGIN_NAME}/{path}"
            if any(api[0] == route for api in self.owner.context.registered_web_apis):
                raise V2Error("route_conflict", "Another owner already registered the management route.", status=409)

            async def handler(_operation=operation):
                return await self.handle(_operation)

            self.owner.context.register_web_api(route, handler, [method], "QQ V2 local control")
            self.routes.append(handler)

    def close(self):
        apis = self.owner.context.registered_web_apis
        apis[:] = [api for api in apis if all(api[1] is not h for h in self.routes)]
        self.routes.clear()
        self.secret = b""

    def platform(self, platform_id):
        matches = [c for c in self.owner.context.get_config().get("platform", []) if c.get("id") == platform_id]
        if len(matches) != 1 or matches[0].get("type") != PLATFORM_TYPE:
            raise V2Error("instance_not_owned", "Target is not a unique V2 platform configuration.", status=404)
        config = matches[0]
        identity = InstanceKey.from_config(config)
        return config, identity, self.fingerprint(config)

    def view(self, platform_id):
        config, identity, fingerprint = self.platform(platform_id)
        state = self.owner.store.get(identity.settings_key)
        instance = next((i for i in self.owner.instances if i.identity.platform_id == platform_id), None)
        return {**state, "platform_id": platform_id, "fingerprint": fingerprint,
                "identity": {"appid": identity.robot.appid, "environment": identity.robot.environment},
                "credentials_configured": bool(config.get("secret")), "enabled": bool(config.get("enable")),
                "connection": {"transport": identity.transport, "intents": identity.intents, "shard": identity.shard},
                "runtime_state": instance.runtime_status()["state"] if instance else "not_loaded",
                "versions": self.owner.store.versions(identity.settings_key)}

    async def handle(self, operation):
        if self.active >= 16:
            return json_response({"status": "error", "code": "control_capacity"}, status_code=429)
        self.active += 1
        try:
            if self.owner.stopping:
                raise V2Error("service_stopped", "Plugin is stopped.", status=503)
            username = require_admin(self.owner.context)
            if not self.flags()["webui_enabled"]:
                raise V2Error("webui_disabled", "Enable WebUI from the host plugin settings.", status=403)
            body = {}
            if request.method == "POST":
                raw = await bounded_body(request._get_current(), 256 * 1024)
                body = json.loads(raw)
                if not isinstance(body, dict):
                    raise V2Error("invalid_request", "Expected a JSON object.")
                self.check_csrf(username, body.get("csrf"))
            if operation.startswith(("connection", "onboarding_")):
                result = await self.dispatch_connection(operation, username, body)
            else:
                async with self.lock:
                    if self.owner.stopping:
                        raise V2Error("service_stopped", "Plugin is stopped.", status=503)
                    result = self.dispatch(operation, username, body)
            return json_response(result, headers={"Cache-Control": "no-store"})
        except V2Error as exc:
            return json_response({**exc.as_dict(), "status": "error"}, status_code=exc.status)
        except sqlite3.Error:
            return json_response({"status": "error", "code": "storage_unavailable", "message": "Local storage is unavailable; no remote action occurred."}, status_code=503)
        except (ValueError, TypeError, RecursionError):
            return json_response({"status": "error", "code": "invalid_request", "message": "Invalid request data."}, status_code=400)
        except TimeoutError:
            return json_response({"status": "error", "code": "control_timeout", "message": "Management request timed out; read current state before retrying."}, status_code=503)
        finally:
            self.active -= 1

    async def dispatch_connection(self, operation, username, body):
        platform_id = body.get("platform_id") if request.method == "POST" else request.query.get("platform_id")
        connections, onboarding = self.owner.connections, self.owner.onboarding
        if operation == "connection":
            return connections.view(platform_id)
        if operation == "connection_save":
            return await connections.save(platform_id, body.get("fingerprint"), body.get("patch", {}),
                secret_action=body.get("secret_action", "keep"), secret=body.get("secret"), confirm=body.get("confirm"),
                confirm_identity=body.get("confirm_identity"), confirm_secret=body.get("confirm_secret"))
        if operation == "connection_reload":
            return await connections.reload(platform_id, body.get("fingerprint"), confirm=body.get("confirm"))
        if operation == "onboarding_start":
            return await onboarding.start(username, platform_id, body.get("fingerprint"), confirm=body.get("confirm"))
        if operation == "onboarding_status":
            return await onboarding.status(username, platform_id, body.get("ticket"), renew=body.get("renew") is True)
        if operation == "onboarding_cancel":
            return await onboarding.cancel(username, platform_id, body.get("ticket"))
        if operation == "onboarding_commit":
            return await onboarding.commit(username, platform_id, body.get("ticket"), body.get("commit_handle"),
                confirm=body.get("confirm"), confirm_identity=body.get("confirm_identity"), confirm_secret=body.get("confirm_secret"))
        raise unsupported()

    def dispatch(self, operation, username, body):
        if operation == "bootstrap":
            configs = self.owner.context.get_config().get("platform", [])
            return {"csrf": self.csrf(username), "flags": self.flags(), "flags_revision": self.fingerprint(self.flags()),
                    "instances": [{"id": c.get("id"), "appid": c.get("appid"), "environment": c.get("environment", "production")}
                                  for c in configs if c.get("type") == PLATFORM_TYPE],
                    "defaults": DEFAULTS, "phase": "P2", "remote_state": "not_implemented"}
        if operation == "flags":
            if body.get("confirm") is not True:
                raise V2Error("confirmation_required", "Confirm the basic switch change.")
            if body.get("revision") != self.fingerprint(self.flags()):
                raise V2Error("config_conflict", "Basic switches changed; reload first.", status=409)
            patch = body.get("patch")
            if not isinstance(patch, dict) or patch.keys() - FLAGS.keys() or any(type(v) is not bool for v in patch.values()):
                raise V2Error("invalid_settings", "Only basic boolean switches are allowed.")
            if patch.get("remote_menu_sync") or patch.get("onebot_network_enabled"):
                raise unsupported("Remote synchronization and network listeners are not implemented.")
            old = dict(self.owner.config)
            try:
                self.owner.config.update(patch)
                self.owner.config.save_config()
            except Exception as exc:
                self.owner.config.clear()
                self.owner.config.update(old)
                raise V2Error("config_save_failed", "Host switch save failed; reload to verify disk state.", status=503) from exc
            return {"flags": self.flags(), "revision": self.fingerprint(self.flags())}
        platform_id = body.get("platform_id") if request.method == "POST" else request.query.get("platform_id")
        config, identity, fingerprint = self.platform(platform_id)
        if operation == "get":
            return self.view(platform_id)
        scene = body.get("scene", "group") if request.method == "POST" else request.query.get("scene", "group")
        if scene not in SCENES:
            raise V2Error("invalid_scene", "Unknown scene.")
        catalog = collect_catalog(self.owner.context.get_config(), scene)
        if operation == "commands":
            return catalog
        if body.get("fingerprint") != fingerprint:
            raise V2Error("config_conflict", "Host platform configuration changed; reload first.", status=409)
        current = self.owner.store.get(identity.settings_key)
        if body.get("revision") != current["revision"]:
            raise V2Error("config_conflict", "Draft changed; reload first.", status=409)
        if operation == "preview":
            settings = validate_settings(merge_patch(current["draft"], body.get("patch", {})))
            return {"panel": panel_preview(catalog, settings),
                    "card": layout_preview(catalog, settings, layer=body.get("layer", "home"), node=body.get("node"), page=body.get("page", 0))}
        action = body.get("operation")
        if action == "apply" and body.get("confirm") is not True:
            raise V2Error("confirmation_required", "Confirm local application; this does not publish to QQ.")
        state = self.owner.store.mutate(identity.settings_key, body.get("revision"), username,
                                        operation=action, patch=body.get("patch"), restore_revision=body.get("restore_revision"))
        if action == "apply":
            for instance in self.owner.instances:
                if instance.identity.settings_key == identity.settings_key:
                    instance.local_settings = state["applied"]
        return self.view(platform_id)
