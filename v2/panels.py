"""Opt-in robot-wide panel ownership; unknown writes reconcile without recreation."""
import asyncio
import copy
import hashlib
import json
import math
import sqlite3
import time
from email.utils import parsedate_to_datetime
from urllib.parse import quote
from uuid import uuid4

from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .commands import collect_catalog, panel_preview, validate_panel_payload
from .errors import V2Error
from .messaging.store import robot_key
from .models import SCENES, SessionRoute, text_id
from .protocol import RequestSpec


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def retry_time(error, now):
    transient = error.business_code is None and (
        error.code in {"connect_failed", "network_failure", "request_deadline", "request_capacity", "panel_rate_limited"}
        or error.code == "qq_api_error" and error.http_status in {408, 429, 500, 502, 503, 504}
        or error.code == "token_refresh_failed" and (error.http_status or error.status) in {408, 429, 500, 502, 503, 504})
    if not transient:
        return None
    try:
        delay = float(error.retry_after)
    except (TypeError, ValueError):
        try:
            delay = parsedate_to_datetime(error.retry_after).timestamp() - now
        except (TypeError, ValueError, AttributeError, OverflowError):
            delay = 5
    return now + max(5, delay) if math.isfinite(delay) and math.isfinite(now + delay) else now + 60


def canonical(value):
    if not isinstance(value, dict) or value.get("scope") not in SCENES or value.get("target_type", "all") not in {"all", "specific"}:
        raise V2Error("invalid_panel_response", "Remote panel scope is invalid.", status=502)
    panel = value.get("panel")
    if not isinstance(panel, dict) or panel.keys() - {"items", "remark", "version"}:
        raise V2Error("invalid_panel_response", "Remote panel fields cannot be safely normalized.", status=502)
    items = panel.get("items", [])
    if not isinstance(items, list) or len(items) > 20:
        raise V2Error("invalid_panel_response", "Remote panel item count is invalid.", status=502)
    normalized = []
    for item in items:
        if not isinstance(item, dict) or item.keys() - {"type", "name", "desc", "only_admin", "link"}:
            raise V2Error("invalid_panel_response", "Remote panel has unknown item fields.", status=502)
        normalized.append({"type": item.get("type", "command"), "name": item.get("name", ""), "desc": item.get("desc", ""),
                           "only_admin": item.get("only_admin", False), **({"link": item["link"]} if "link" in item else {})})
    result = {"scope": value["scope"], "target_type": value.get("target_type", "all"),
              "panel": {"items": normalized, "remark": panel.get("remark", "")}}
    for field in ("user_openids", "group_openids"):
        ids = value.get(field, [])
        if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(i, str) for i in ids):
            raise V2Error("invalid_panel_response", "Remote panel targets are invalid.", status=502)
        if ids:
            result[field] = sorted(set(ids))
    return result


class PanelService:
    def __init__(self, owner, *, clock=time.time, sleep=asyncio.sleep, stability_seconds=1, catalog_provider=None):
        self.owner, self.store, self.clock, self.sleep = owner, owner.messages, clock, sleep
        self.last_error = None
        self.db = self.store.db
        self.stability_seconds = stability_seconds
        self.catalog_provider = catalog_provider or self._catalog
        self.locks, self.stable = {}, {}
        self.tasks = set()
        self.worker = None
        self.closed = False
        manager = getattr(owner.context, "_star_manager", None)
        lock = getattr(manager, "_pm_lock", None)
        self.hot_loading = bool(lock and lock.locked())
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS panels (robot TEXT, scene TEXT, body TEXT NOT NULL, PRIMARY KEY(robot,scene));
            CREATE TABLE IF NOT EXISTS panel_rates (robot TEXT, kind TEXT, stamp REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS panel_rate_bucket ON panel_rates(robot,kind,stamp);
        """)
        for robot, scene, body in self.db.execute("SELECT robot,scene,body FROM panels").fetchall():
            value = json.loads(body)
            if value["state"] == "writing":
                value["state"] = "unknown"
                self._save(robot, scene, value)

    def _save(self, robot, scene, value, *, control=False):
        if not control:
            row = self.db.execute("SELECT body FROM panels WHERE robot=? AND scene=?", (robot, scene)).fetchone()
            if row:
                previous = json.loads(row[0])
                for key in ("enabled", "platform_id", "intent"):
                    if key in previous:
                        value[key] = previous[key]
        if value["state"] == "synced":
            value.pop("failed_fingerprint", None)
            value.pop("failed_generation", None)
        if value["state"] in {"writing", "synced"}:
            value.pop("retry_at", None)
        if len(json.dumps(value).encode()) > 256 * 1024:
            raise V2Error("panel_state_full", "Panel state exceeds its bounded record size.", status=503)
        with self.store.transaction():
            if not self.db.execute("SELECT 1 FROM panels WHERE robot=? AND scene=?", (robot, scene)).fetchone():
                if self.db.execute("SELECT count(*) FROM panels").fetchone()[0] >= 1024:
                    raise V2Error("panel_state_full", "Panel ownership capacity reached.", status=503)
            self.db.execute("INSERT OR REPLACE INTO panels VALUES(?,?,?)", (robot, scene, json.dumps(value)))

    def state(self, instance, scene):
        row = self.db.execute("SELECT body FROM panels WHERE robot=? AND scene=?", (robot_key(instance.identity.robot), scene)).fetchone()
        return json.loads(row[0]) if row else {"state": "not_managed", "enabled": False, "panel_id": None, "previous": None, "pending": None, "error": None}

    def gate(self, instance):
        if self.closed or self.owner.stopping:
            raise V2Error("service_stopped", "Panel synchronization is stopped.", status=503)
        instance.check_generation()
        if self.owner.config.get("remote_menu_sync") is not True:
            raise V2Error("remote_sync_disabled", "Enable the independent remote-menu switch before publishing.", status=403)

    def _host_ready(self):
        manager = getattr(self.owner.context, "_star_manager", None)
        lock = getattr(manager, "_pm_lock", None)
        if lock and lock.locked():
            return False
        return bool(getattr(self.owner, "catalog_ready", False) or self.hot_loading)

    def _catalog(self, instance, scene, target_type, targets):
        context = self.owner.context
        if target_type == "specific" and scene in {"group", "c2c"}:
            configs = []
            message_type = MessageType.GROUP_MESSAGE if scene == "group" else MessageType.FRIEND_MESSAGE
            for target in targets:
                route = SessionRoute(instance.identity.robot, scene, target)
                umo = str(MessageSession(instance.identity.platform_id, message_type, route.encode()))
                configs.append(context.get_config(umo))
        else:
            manager = getattr(context, "astrbot_config_mgr", None)
            configs = list(getattr(manager, "confs", {}).values()) or [context.get_config()]
        catalogs = [collect_catalog(config, scene) for config in configs]
        first = copy.deepcopy(catalogs[0])
        first["scope_variants"] = catalogs
        return first

    def capture_bindings(self, instance, settings, *, confirm=False):
        selected = {n for p in settings["panels"].values() if p["mode"] == "custom" for n in p["selected"]}
        nodes = {}
        for scene in SCENES:
            nodes.update({n["id"]: n for n in collect_catalog(self.owner.context.get_config(), scene)["nodes"]})
        key = instance.identity.settings_key
        current = dict(self.owner.store.db.execute("SELECT handler,binding FROM command_bindings WHERE settings_key=?", (key,)))
        changes = [identity for identity in selected if identity not in nodes or current.get(identity) != nodes[identity]["binding"]]
        if changes and confirm is not True:
            raise V2Error("command_binding_confirmation", "Confirm the current handler sources and parameters before binding selected shortcuts.", status=409)
        if any(identity not in nodes or not nodes[identity]["binding"] for identity in selected):
            raise V2Error("command_unavailable", "A selected command source is unavailable.", status=409)
        return {identity: nodes[identity]["binding"] for identity in selected}

    def filter_pins(self, instance, settings, catalog):
        value = copy.deepcopy(settings)
        selected = value["panels"][catalog["scene"]]
        if selected["mode"] == "custom":
            bindings = dict(self.owner.store.db.execute("SELECT handler,binding FROM command_bindings WHERE settings_key=?", (instance.identity.settings_key,)))
            nodes = {node["id"]: node for node in catalog["nodes"]}
            selected["selected"] = [identity for identity in selected["selected"] if identity in nodes and nodes[identity]["binding"] and bindings.get(identity) == nodes[identity]["binding"]]
        return value

    def plan(self, instance, scene, *, target_type="all", targets=None, menu_only=False):
        if scene not in SCENES or type(menu_only) is not bool or target_type not in {"all", "specific"}:
            raise V2Error("invalid_panel", "Invalid panel scope or mode.")
        targets = [] if targets is None else targets
        if not isinstance(targets, list) or len(targets) > 20 or any(not isinstance(t, str) for t in targets) or len(set(targets)) != len(targets):
            raise V2Error("invalid_panel", "Use at most 20 distinct observed targets.")
        targets = sorted(text_id(t) for t in targets)
        if target_type == "all" and targets or target_type == "specific" and (not targets or scene not in {"group", "c2c"}):
            raise V2Error("invalid_panel", "Targets are not supported by this scope.")
        state = self.state(instance, scene)
        intent = state.get("intent", {})
        confirmed_scope = (state.get("platform_id") == instance.identity.platform_id
            and intent.get("target_type") == target_type and intent.get("targets") == targets)
        # Persisted operator consent survives chat TTL, but never authorizes another scope.
        if not confirmed_scope:
            for target in targets:
                self.store.target(SessionRoute(instance.identity.robot, scene, target))
        settings = self.owner.store.get(instance.identity.settings_key)
        catalog = self.catalog_provider(instance, scene, target_type, targets)
        variants = catalog.get("scope_variants", [catalog])
        applied = copy.deepcopy(settings["applied"])
        if menu_only:
            applied["panels"][scene] = {"mode": "custom", "selected": []}
        preview = panel_preview(catalog, applied)
        issues = list(preview["issues"])
        selected = applied["panels"][scene]
        if selected["mode"] == "custom" and selected["selected"]:
            safe = self.filter_pins(instance, applied, catalog)["panels"][scene]["selected"]
            if safe != selected["selected"]:
                issues.append("command_binding_confirmation")
        def items(value):
            return [{key: item[key] for key in ("type", "name", "desc", "only_admin")} for item in value["items"]]
        planned_items = items(preview)
        for variant in variants:
            other = panel_preview(variant, applied)
            if other["issues"] or items(other) != planned_items:
                issues.append("scope_config_conflict")
                break
        payload = {"scope": scene, "target_type": target_type, "panel": {"items": planned_items}}
        if targets:
            payload["user_openids" if scene == "c2c" else "group_openids"] = targets
        try:
            validate_panel_payload(payload)
        except V2Error as exc:
            issues.append(exc.code)
        intent = {"target_type": target_type, "targets": targets, "menu_only": menu_only}
        fingerprint = digest([instance.identity.settings_key, intent, settings["applied_revision"], payload, catalog["version"], issues])
        key = (robot_key(instance.identity.robot), scene)
        if self.stable.get(key, (None,))[0] != fingerprint:
            if key not in self.stable and len(self.stable) >= 1024:
                self.stable.pop(next(iter(self.stable)))
            self.stable[key] = (fingerprint, self.clock())
        return {"payload": payload, "issues": sorted(set(issues)), "intent": intent, "applied_revision": settings["applied_revision"],
                "fingerprint": fingerprint, "catalog_version": catalog["version"], "state": state, "permission": "unknown"}

    def _stable_plan(self, instance, scene, intent):
        value = self.plan(instance, scene, **intent)
        key = (robot_key(instance.identity.robot), scene)
        old = self.stable.get(key)
        if not old or old[0] != value["fingerprint"]:
            self.stable[key] = (value["fingerprint"], self.clock())
        if not self._host_ready() or self.clock() - self.stable[key][1] < self.stability_seconds:
            raise V2Error("catalog_not_stable", "Wait for host loading and a stable applied command snapshot.", status=409)
        if value["issues"]:
            raise V2Error("panel_invalid", "Panel blocked: " + ", ".join(value["issues"][:8]), status=409)
        return value

    async def enable(self, instance, scene, fingerprint, *, confirm, target_type="all", targets=None, menu_only=False):
        self.gate(instance)
        lock = self.locks.get(robot_key(instance.identity.robot))
        if lock and lock.locked():
            raise V2Error("panel_busy", "A robot-wide panel operation is already in progress.", status=409)
        if confirm is not True:
            raise V2Error("confirmation_required", "Confirm the displayed application, scope and panel items.")
        plan = self.plan(instance, scene, target_type=target_type, targets=targets, menu_only=menu_only)
        if fingerprint != plan["fingerprint"]:
            raise V2Error("config_conflict", "Applied settings or command catalog changed; preview again.", status=409)
        self._stable_plan(instance, scene, plan["intent"])
        value = self.state(instance, scene)
        if value.get("platform_id") not in (None, instance.identity.platform_id):
            raise V2Error("panel_owner_conflict", "Another instance already coordinates this robot and scene.", status=409)
        if value.get("pending"):
            raise V2Error("panel_result_unknown", "Reconcile the previous operation before changing its intent.", status=409)
        if value.get("panel_id") and any(value["intent"][k] != plan["intent"][k] for k in ("target_type", "targets")):
            raise V2Error("panel_scope_locked", "This owned panel's scope is immutable here; no implicit delete/rebind is allowed.", status=409)
        value.update(enabled=True, platform_id=instance.identity.platform_id, intent=plan["intent"])
        self._save(robot_key(instance.identity.robot), scene, value, control=True)
        return await self.sync(instance, scene)

    def disable(self, instance, scene, *, confirm):
        instance.check_generation()
        if confirm is not True:
            raise V2Error("confirmation_required", "Confirm stopping synchronization; remote resources are preserved.")
        value = self.state(instance, scene)
        if value.get("platform_id") not in (None, instance.identity.platform_id):
            raise V2Error("panel_owner_conflict", "Only the coordinating instance can disable this scope.", status=409)
        value["enabled"] = False
        self._save(robot_key(instance.identity.robot), scene, value, control=True)
        return value

    def _wire_gate(self, instance, kind):
        self.gate(instance)
        robot = robot_key(instance.identity.robot)
        with self.store.transaction():
            now = self.store.now()
            self.db.execute("DELETE FROM panel_rates WHERE stamp<=?", (now - 60,))
            count = self.db.execute("SELECT count(*) FROM panel_rates WHERE robot=? AND kind=?", (robot, kind)).fetchone()[0]
            if count >= (30 if kind == "read" else 10):
                raise V2Error("panel_rate_limited", "Conservative robot-wide panel rate limit reached.", status=429)
            self.db.execute("INSERT INTO panel_rates VALUES(?,?,?)", (robot, kind, now))

    async def _request(self, instance, method, path, *, params=None, body=None, check=None):
        def before_send():
            if check:
                check()
            self._wire_gate(instance, "read" if method == "GET" else "write")
        return await instance.http.request(RequestSpec(instance.identity.robot.environment, method, path, params=params, json_body=body), before_send=before_send)

    async def _list(self, instance):
        all_records = {}
        for scene in SCENES:
            cursor, seen = None, set()
            for _ in range(8):
                response = await self._request(instance, "GET", "/v2/panels", params={"scope": scene, "limit": 50, **({"cursor": cursor} if cursor else {})})
                data = response.data
                if not isinstance(data, dict) or not isinstance(data.get("records"), list) or type(data.get("is_end")) is not bool:
                    raise V2Error("invalid_panel_response", "Panel list is incomplete.", status=502)
                for record in data["records"]:
                    if not isinstance(record, dict) or record.get("scope") != scene:
                        raise V2Error("invalid_panel_response", "Panel list has a mismatched scope.", status=502)
                    panel_id = text_id(record.get("panel_id"))
                    if panel_id in all_records or len(all_records) >= 200:
                        raise V2Error("invalid_panel_response", "Panel listing is duplicate or exceeds the safety bound.", status=502)
                    all_records[panel_id] = record
                if data["is_end"]:
                    break
                cursor = data.get("next_cursor")
                if not isinstance(cursor, str) or not cursor or len(cursor) > 2048 or cursor in seen:
                    raise V2Error("invalid_panel_response", "Panel pagination did not advance.", status=502)
                seen.add(cursor)
            else:
                raise V2Error("invalid_panel_response", "Panel pagination exceeds the safety bound.", status=502)
        return all_records

    async def _detail(self, instance, panel_id):
        response = await self._request(instance, "GET", "/v2/panels/" + quote(panel_id, safe=""))
        if not isinstance(response.data, dict) or response.data.get("panel_id") != panel_id:
            raise V2Error("invalid_panel_response", "Panel details belong to a different resource.", status=502)
        return canonical(response.data)

    async def _reconcile(self, instance, scene, value):
        pending = value["pending"]
        if pending["kind"] == "create":
            records = await self._list(instance)
            matches = [key for key, r in records.items() if key not in pending["baseline"] and r.get("scope") == scene
                       and isinstance(r.get("panel"), dict) and r["panel"].get("remark") == pending["payload"]["panel"]["remark"]]
            if len(matches) != 1:
                raise V2Error("panel_result_unknown", "Unknown create has no unique new candidate; it will not be recreated.", status=409, phase="result_unknown")
            panel_id = matches[0]
        else:
            panel_id = value["panel_id"]
        actual = await self._detail(instance, panel_id)
        if actual != canonical(pending["payload"]):
            raise V2Error("panel_result_unknown", "Remote state does not prove the pending write; automatic replay is forbidden.", status=409, phase="result_unknown")
        value.update(panel_id=panel_id, previous=actual, pending=None, state="synced", error=None, checked=self.clock())
        self._save(robot_key(instance.identity.robot), scene, value)
        return value

    async def sync(self, instance, scene):
        self.gate(instance)
        if len(self.tasks) >= 8:
            raise V2Error("panel_capacity", "Too many panel operations are outstanding.", status=429)
        robot = robot_key(instance.identity.robot)
        if robot not in self.locks:
            if len(self.locks) >= 256:
                raise V2Error("panel_capacity", "Too many robot coordinators.", status=429)
            self.locks[robot] = asyncio.Lock()
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            async with self.locks[robot]:
                self.gate(instance)
                value = self.state(instance, scene)
                if not value["enabled"] or value.get("platform_id") != instance.identity.platform_id:
                    raise V2Error("panel_not_managed", "Confirm management of this instance and scope first.", status=403)
                response_received = False
                attempt_plan = None
                write_attempt = False
                try:
                    if value["pending"]:
                        return await self._reconcile(instance, scene, value)
                    plan = self._stable_plan(instance, scene, value["intent"])
                    attempt_plan = plan["fingerprint"]
                    payload = copy.deepcopy(plan["payload"])
                    if value["panel_id"]:
                        actual = await self._detail(instance, value["panel_id"])
                        if actual != value["previous"]:
                            raise V2Error("panel_drift", "Remote panel changed outside this adapter; synchronization is paused.", status=409)
                        payload["panel"]["remark"] = value["previous"]["panel"]["remark"]
                        if canonical(payload) == actual:
                            value.update(state="synced", error=None, checked=self.clock(), fingerprint=plan["fingerprint"])
                            self._save(robot, scene, value)
                            return value
                        pending = {"kind": "update", "payload": payload}
                        method, path, body = "PUT", "/v2/panels/" + quote(value["panel_id"], safe=""), {"panel": payload["panel"]}
                    else:
                        records = await self._list(instance)
                        if len(records) >= 20:
                            raise V2Error("panel_limit", "All external panels count toward the 20-panel limit; none were removed.", status=409)
                        payload["panel"]["remark"] = "astrbot-v2:" + uuid4().hex
                        pending = {"kind": "create", "payload": payload, "baseline": list(records)}
                        method, path, body = "POST", "/v2/panels", payload
                    write_attempt = True
                    value.update(state="writing", pending=pending, error=None)
                    self._save(robot, scene, value)
                    def recheck():
                        current = self.state(instance, scene)
                        if not current["enabled"] or self._stable_plan(instance, scene, value["intent"])["fingerprint"] != plan["fingerprint"]:
                            raise V2Error("config_conflict", "The confirmed panel snapshot changed before sending.", status=409)
                    response = await self._request(instance, method, path, body=body, check=recheck)
                    response_received = True
                    data = response.data
                    if method == "POST":
                        if not isinstance(data, dict) or not isinstance(data.get("panel_id"), str) or not data["panel_id"]:
                            raise V2Error("invalid_panel_response", "Create did not return a real panel ID.", status=502, phase="result_unknown")
                        value["panel_id"] = text_id(data["panel_id"])
                    elif not isinstance(data, dict) or type(data.get("version")) is not int:
                        raise V2Error("invalid_panel_response", "Update did not return a version.", status=502, phase="result_unknown")
                    value.update(state="synced", previous=canonical(payload), pending=None, error=None, checked=self.clock(), fingerprint=plan["fingerprint"])
                    self._save(robot, scene, value)
                    return value
                except asyncio.CancelledError as exc:
                    if value.get("pending"):
                        unknown = getattr(exc, "phase", "result_unknown") == "result_unknown"
                        value.update(state="unknown" if unknown else "failed", error="cancelled")
                        if not unknown:
                            value["pending"] = None
                        self._save(robot, scene, value)
                    raise
                except V2Error as exc:
                    if response_received or value.get("pending") and exc.code != "token_refresh_failed" and exc.http_status is not None and exc.http_status >= 500:
                        exc = V2Error(exc.code, str(exc), retcode=exc.retcode, status=exc.status,
                            business_code=exc.business_code, trace_id=exc.trace_id, retry_after=exc.retry_after,
                            http_status=exc.http_status, phase="result_unknown")
                    if value.get("pending") and exc.phase not in {"not_sent", "rejected"}:
                        value["state"] = "unknown"
                    elif value.get("pending") and value["state"] == "writing":
                        value.update(state="failed", pending=None)
                    elif value.get("pending"):
                        value["state"] = "unknown"
                    else:
                        value["state"] = "paused" if exc.code == "panel_drift" else "failed"
                    if attempt_plan and not value.get("pending"):
                        retry_at = retry_time(exc, self.clock()) if not write_attempt or exc.phase == "not_sent" else None
                        if retry_at is not None:
                            value.pop("failed_fingerprint", None)
                            value.pop("failed_generation", None)
                            value["retry_at"] = retry_at
                        else:
                            value.pop("retry_at", None)
                            value["failed_fingerprint"] = attempt_plan
                            value["failed_generation"] = instance.identity.generation
                    value["checked"] = self.clock()
                    value["error"] = exc.as_dict()
                    self._save(robot, scene, value)
                    raise exc from None
        finally:
            self.tasks.discard(task)

    def start(self):
        if self.worker is None:
            self.worker = asyncio.create_task(self._run(), name="qq-v2-panel-sync")

    async def _run(self):
        while not self.closed:
            await self.sleep(5)
            if self.owner.config.get("remote_menu_sync") is not True or not self._host_ready():
                continue
            try:
                records = self.db.execute("SELECT robot,scene,body FROM panels").fetchall()
            except sqlite3.Error:
                self.last_error = "panel_storage_unavailable"
                continue
            if self.last_error == "panel_storage_unavailable":
                self.last_error = None
            for robot, scene, body in records:
                value = json.loads(body)
                if not value["enabled"] or value["state"] == "paused":
                    continue
                instance = next((i for i in self.owner.instances if i.identity.platform_id == value.get("platform_id") and robot_key(i.identity.robot) == robot), None)
                if instance is None:
                    continue
                try:
                    if self.clock() < value.get("retry_at", 0):
                        continue
                    if value.get("pending"):
                        if self.clock() - value.get("checked", 0) < 60:
                            continue
                    else:
                        plan = self._stable_plan(instance, scene, value["intent"])
                        if value.get("failed_fingerprint") == plan["fingerprint"] and value.get("failed_generation") == instance.identity.generation:
                            continue
                        if value.get("fingerprint") == plan["fingerprint"] and self.clock() - value.get("checked", 0) < 60:
                            continue
                    await self.sync(instance, scene)
                    self.last_error = None
                except V2Error as exc:
                    self.last_error = exc.code
                    current = self.state(instance, scene)
                    if not current.get("pending") and exc.code == "panel_invalid":
                        current.update(state="blocked", error=exc.as_dict(), checked=self.clock())
                        self._save(robot, scene, current)
                except Exception:
                    self.last_error = "panel_coordinator_failure"

    async def close(self):
        self.closed = True
        tasks = self.tasks | ({self.worker} if self.worker else set())
        tasks.discard(asyncio.current_task())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
