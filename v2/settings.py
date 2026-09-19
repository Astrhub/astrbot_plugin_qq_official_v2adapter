"""Versioned non-secret drafts; applying locally never publishes to QQ."""

import copy
import json
import sqlite3
from pathlib import Path

from .errors import V2Error
from .models import SCENES

LAYERS = ("home", "plugin", "group", "detail")
LAYOUT = {"page_size": 8, "columns": 2, "style": "plain", "show_description": True}
DEFAULTS = {
    "schema_version": 1, "title": "机器人菜单",
    "layout": {layer: dict(LAYOUT) for layer in LAYERS},
    "scene_overrides": {}, "node_overrides": {},
    "panels": {scene: {"mode": "default", "selected": []} for scene in SCENES},
}


def invalid(message):
    raise V2Error("invalid_settings", message)


def layout_check(value, *, partial=False):
    if not isinstance(value, dict) or value.keys() - LAYOUT.keys() or (not partial and value.keys() != LAYOUT.keys()):
        invalid("Unknown or missing layout fields.")
    for key, val in value.items():
        if key == "page_size" and (type(val) is not int or not 1 <= val <= 21):
            invalid("page_size must be 1..21 (four navigation slots reserved).")
        if key == "columns" and (type(val) is not int or not 1 <= val <= 5):
            invalid("columns must be 1..5.")
        if key == "style" and val not in ("plain", "heading", "quote"):
            invalid("Unknown card style.")
        if key == "show_description" and type(val) is not bool:
            invalid("show_description must be boolean.")


def validate_settings(value):
    if not isinstance(value, dict) or value.keys() != DEFAULTS.keys():
        invalid("Only documented non-secret settings may be saved.")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        invalid("Unsupported schema version.")
    if not isinstance(value["title"], str) or not 1 <= len(value["title"]) <= 80:
        invalid("title must contain 1..80 characters.")
    layers = value["layout"]
    if not isinstance(layers, dict) or layers.keys() != set(LAYERS):
        invalid("All four layout layers are required.")
    for layout in layers.values():
        layout_check(layout)
    scenes = value["scene_overrides"]
    if not isinstance(scenes, dict) or scenes.keys() - set(SCENES):
        invalid("Invalid scene override.")
    for overrides in scenes.values():
        if not isinstance(overrides, dict) or overrides.keys() - set(LAYERS):
            invalid("Invalid scene layer.")
        for layout in overrides.values():
            layout_check(layout, partial=True)
    nodes = value["node_overrides"]
    if not isinstance(nodes, dict) or len(nodes) > 500:
        invalid("At most 500 node overrides are allowed.")
    for node, layout in nodes.items():
        if not isinstance(node, str) or not 1 <= len(node) <= 512:
            invalid("Invalid node identity.")
        layout_check(layout, partial=True)
    panels = value["panels"]
    if not isinstance(panels, dict) or panels.keys() != set(SCENES):
        invalid("All four panel selections are required.")
    for selection in panels.values():
        if not isinstance(selection, dict) or selection.keys() != {"mode", "selected"}:
            invalid("Invalid panel selection.")
        if selection["mode"] not in ("default", "custom"):
            invalid("Use default or custom selection.")
        ids = selection["selected"]
        if not isinstance(ids, list) or len(ids) > 500 or any(not isinstance(i, str) or not 1 <= len(i) <= 512 for i in ids):
            invalid("Invalid selected command identities.")
        if len(ids) != len(set(ids)):
            invalid("Duplicate selected commands.")
    if len(json.dumps(value).encode()) > 256 * 1024:
        invalid("Settings exceed 256 KiB.")
    return copy.deepcopy(value)


def merge_patch(current, patch):
    if not isinstance(patch, dict):
        invalid("patch must be an object.")
    result = copy.deepcopy(current)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = merge_patch(result[key], value)
        elif value is None:
            result.pop(key, None)
        else:
            result[key] = copy.deepcopy(value)
    return result


def effective_layout(settings, scene, layer, node=None):
    value = dict(settings["layout"][layer])
    sources = dict.fromkeys(value, "robot")
    for source, override in (("scene", settings["scene_overrides"].get(scene, {}).get(layer, {})),
                             ("node", settings["node_overrides"].get(node, {}))):
        value.update(override)
        sources.update(dict.fromkeys(override, source))
    return value, sources


class SettingsStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=2)
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise V2Error("settings_corrupt", "Unsupported database version; data was not reset.", status=503)
            page_size = self.db.execute("PRAGMA page_size").fetchone()[0]
            self.db.execute(f"PRAGMA max_page_count={128 * 1024 * 1024 // page_size}")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                    draft TEXT NOT NULL, applied_revision INTEGER NOT NULL,
                    applied TEXT NOT NULL, editor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS versions (
                    key TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(key, revision));
                PRAGMA user_version=1;
            """)
        except BaseException:
            self.db.close()
            raise
        self.closed = False

    def close(self):
        if not self.closed:
            self.closed = True
            self.db.close()

    def get(self, key):
        if self.closed:
            raise V2Error("service_stopped", "Settings service is stopped.", status=503)
        row = self.db.execute("SELECT revision, draft, applied_revision, applied, editor FROM settings WHERE key=?", (key,)).fetchone()
        try:
            rev, draft, applied_rev, applied, editor = row or (0, json.dumps(DEFAULTS), 0, json.dumps(DEFAULTS), "")
            return {"revision": rev, "draft": validate_settings(json.loads(draft)),
                    "applied_revision": applied_rev, "applied": validate_settings(json.loads(applied)),
                    "editor": editor, "state": "applied_local" if rev == applied_rev else "draft",
                    "remote_state": "not_implemented"}
        except (ValueError, TypeError, V2Error) as exc:
            raise V2Error("settings_corrupt", "Invalid stored settings; not reset automatically.", status=503) from exc

    def mutate(self, key, revision, editor, *, operation, patch=None, restore_revision=None):
        if type(revision) is not int or revision < 0:
            invalid("revision must be a nonnegative integer.")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            current = self.get(key)
            if current["revision"] != revision:
                raise V2Error("config_conflict", "Draft changed; reload before saving.", status=409)
            exists = self.db.execute("SELECT 1 FROM settings WHERE key=?", (key,)).fetchone()
            if not exists and self.db.execute("SELECT count(*) FROM settings").fetchone()[0] >= 256:
                raise V2Error("settings_capacity", "At most 256 identity-scoped settings records are retained.", status=409)
            if operation == "apply":
                body = current["draft"]
                new_rev = revision
                applied_rev = new_rev
                applied = body
            else:
                if operation == "save":
                    body = validate_settings(merge_patch(current["draft"], patch))
                elif operation == "discard":
                    body = current["applied"]
                elif operation == "defaults":
                    body = copy.deepcopy(DEFAULTS)
                elif operation == "restore":
                    if type(restore_revision) is not int or restore_revision < 0:
                        invalid("restore_revision must be a nonnegative integer.")
                    row = self.db.execute("SELECT body FROM versions WHERE key=? AND revision=?", (key, restore_revision)).fetchone()
                    if not row:
                        raise V2Error("version_not_found", "This retained version does not exist.", status=404)
                    body = validate_settings(json.loads(row[0]))
                else:
                    invalid("Unknown settings operation.")
                new_rev = revision + 1
                applied_rev, applied = current["applied_revision"], current["applied"]
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?, ?, ?, ?, ?)",
                            (key, new_rev, json.dumps(body), applied_rev, json.dumps(applied), editor))
            self.db.execute("INSERT OR REPLACE INTO versions VALUES (?, ?, ?)", (key, new_rev, json.dumps(body)))
            self.db.execute("DELETE FROM versions WHERE key=? AND revision NOT IN (SELECT revision FROM versions WHERE key=? ORDER BY revision DESC LIMIT 20)", (key, key))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return self.get(key)

    def versions(self, key):
        return [row[0] for row in self.db.execute("SELECT revision FROM versions WHERE key=? ORDER BY revision DESC", (key,))]
