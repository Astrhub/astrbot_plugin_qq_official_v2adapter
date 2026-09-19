import copy
import json
from types import SimpleNamespace
from typing import Optional

import pytest

from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.permission import PermissionType, PermissionTypeFilter
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType, PlatformAdapterTypeFilter
from astrbot.core.star.star_handler import EventType, StarHandlerMetadata

from v2 import PLUGIN_NAME
from v2.commands import collect_catalog, layout_preview, panel_preview, parameter_help, validate_panel_payload
from v2.errors import V2Error
from v2.settings import DEFAULTS, SettingsStore, effective_layout, merge_patch, validate_settings


def test_draft_apply_conflict_recovery(tmp_path):
    path = tmp_path / "config.db"
    first, second = SettingsStore(path), SettingsStore(path)
    try:
        before = first.get("a")
        assert before["revision"] == 0
        saved = first.mutate("a", 0, "alice", operation="save", patch={"title": "changed"})
        assert saved["draft"]["title"] == "changed" and saved["applied"]["title"] != "changed"
        with pytest.raises(V2Error) as exc:
            second.mutate("a", 0, "bob", operation="save", patch={"title": "lost"})
        assert exc.value.code == "config_conflict"
        applied = first.mutate("a", 1, "alice", operation="apply")
        assert applied["applied"]["title"] == "changed" and applied["remote_state"] == "not_implemented"
        assert first.get("different-robot")["revision"] == 0
        first.close()
        first = SettingsStore(path)
        assert first.get("a")["applied_revision"] == 1
        current = first.mutate("a", 1, "alice", operation="save", patch={"title": "new"})
        current = first.mutate("a", current["revision"], "alice", operation="discard")
        assert current["draft"]["title"] == "changed"
        current = first.mutate("a", current["revision"], "alice", operation="restore", restore_revision=2)
        assert current["draft"]["title"] == "new"
        for n in range(25):
            current = first.mutate("a", current["revision"], "alice", operation="save", patch={"title": str(n)})
        assert len(first.versions("a")) == 20
        first.db.execute("UPDATE settings SET draft='bad' WHERE key='a'")
        first.db.commit()
        with pytest.raises(V2Error) as exc:
            first.get("a")
        assert exc.value.code == "settings_corrupt"
    finally:
        first.close()
        second.close()


@pytest.mark.parametrize("patch", [{"secret": "not-storable"}, {"schema_version": 2}, {"layout": {"home": {"page_size": 0}}},
                                     {"layout": {"home": {"columns": 6}}}, {"layout": {"home": {"style": "<script>"}}},
                                     {"scene_overrides": {"evil": {}}}, {"title": ""}, {"layout": {"home": {"show_description": 1}}}])
def test_settings_reject_unknown_and_invalid(patch):
    with pytest.raises(V2Error):
        validate_settings(merge_patch(DEFAULTS, patch))


def fixtures():
    handlers, plugins = [], {}

    def add(name, *, module="astrbot.builtin_stars.builtin_commands.main", method=None, parent=None, group=False):
        async def fn(self, event):
            raise AssertionError("metadata reads must never execute a command")
        method = method or name
        md = StarHandlerMetadata(EventType.AdapterMessageEvent, module + "_" + method, method, module, fn, [], "description")
        command = CommandGroupFilter(name, parent_group=parent) if group else CommandFilter(name, alias={name + "alias"}, handler_md=md,
                          parent_command_names=parent.get_complete_command_names() if parent else None)
        if parent:
            parent.add_sub_command_filter(command)
        md.event_filters = [command]
        handlers.append(md)
        plugins[module] = SimpleNamespace(name=PLUGIN_NAME if method == "menu" else module, activated=True,
                                           star_cls=object(), reserved=module.startswith("astrbot.builtin_stars."))
        return md, command

    add("help")
    parent_md, parent = add("admin", group=True)
    parent_md.event_filters.append(PermissionTypeFilter(PermissionType.ADMIN))
    child_md, child = add("child", parent=parent)
    child.handler_params = {"required_optional": Optional[int], "default": 3, "token": "do-not-leak", "rest": GreedyStr}
    add("v2menu", module="data.plugins.v2.main", method="menu")
    add("plugin", module="data.plugins.sample.main")
    return handlers, plugins, add, child_md, child


def test_catalog_groups_permissions_params_and_rename():
    handlers, plugins, add, child_md, child = fixtures()
    catalog = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    node = next(n for n in catalog["nodes"] if n["id"] == child_md.handler_full_name)
    assert node["name"] == "admin child" and node["parent"]
    assert node["permission"] == ["admin"]
    assert node["parameters"][0]["required"] is True
    assert node["parameters"][-1]["greedy"] and not node["parameters"][-1]["required"]
    assert "do-not-leak" not in json.dumps(catalog)
    assert node["parameters"][1]["default"] == 3
    selected = copy.deepcopy(DEFAULTS)
    selected["panels"]["group"] = {"mode": "custom", "selected": [child_md.handler_full_name]}
    child.command_name = "renamed"
    child._cmpl_cmd_names = None
    new_catalog = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    preview = panel_preview(new_catalog, selected)
    assert preview["slots"] == 2 and preview["items"][0]["name"] == "/admin renamed"
    assert not preview["publishable"]
    assert new_catalog["version"] != catalog["version"]
    child_md.enabled = False
    assert panel_preview(collect_catalog({}, "group", handlers=handlers, plugins=plugins), selected)["issues"]


def test_catalog_respects_platform_plugin_builtin_and_conflicts():
    handlers, plugins, add, _, _ = fixtures()
    md, _ = add("restricted", module="data.plugins.restricted.main")
    md.event_filters.append(PlatformAdapterTypeFilter(PlatformAdapterType.QQOFFICIAL))
    add("help", module="data.plugins.conflict.main")
    catalog = collect_catalog({}, "group", handlers=handlers, plugins=plugins)
    assert next(n for n in catalog["nodes"] if n["id"] == md.handler_full_name)["reasons"] == ["platform_filter_mismatch"]
    assert all(not n["enabled"] for n in catalog["nodes"] if n["name"] == "help")
    filtered = collect_catalog({"plugin_set": [], "disable_builtin_commands": True}, "group", handlers=handlers, plugins=plugins)
    assert all(not n["enabled"] for n in filtered["nodes"])


def test_default_selection_counts_pins_and_does_not_truncate():
    handlers, plugins, add, _, _ = fixtures()
    for n in range(22):
        add(f"system{n}")
    for n in range(110):
        add(f"plugin{n}", module=f"data.plugins.p{n}.main")
    add("averylongcommand")
    catalog = collect_catalog({"wake_prefix": ["/"]}, "group", handlers=handlers, plugins=plugins)
    preview = panel_preview(catalog, DEFAULTS)
    assert preview["slots"] == 27
    assert "panel_items_over_20" in preview["issues"]
    assert any(i["name"] == "/averylongcommand" for i in preview["items"])
    assert not any(i["name"] == "/plugin109" for i in preview["items"])
    settings = copy.deepcopy(DEFAULTS)
    settings["layout"]["home"].update(page_size=21, columns=1)
    all_ids = []
    first = layout_preview(catalog, settings)
    for page in range(first["pages"]):
        current = layout_preview(catalog, settings, page=page)
        assert len(current["items"]) + 4 <= 5
        all_ids.extend(n["id"] for n in current["items"])
    assert len(all_ids) == len(set(all_ids)) == first["total"]
    settings["scene_overrides"] = {"group": {"home": {"columns": 5}}}
    settings["node_overrides"] = {"x": {"page_size": 3}}
    layout, sources = effective_layout(settings, "group", "home", "x")
    assert layout["columns"] == 5 and layout["page_size"] == 3
    assert sources["columns"] == "scene" and sources["page_size"] == "node"


@pytest.mark.parametrize("change", ["count", "name", "desc", "targets", "scene", "total"])
def test_official_panel_limits(change):
    payload = {"scope": "group", "panel": {"items": [{"type": "command", "name": "七个汉字正好哦"}]}}
    assert validate_panel_payload(payload)
    existing = 0
    if change == "count": payload["panel"]["items"] *= 21
    if change == "name": payload["panel"]["items"][0]["name"] += "多"
    if change == "desc": payload["panel"]["items"][0]["desc"] = "中" * 16
    if change == "targets": payload.update(target_type="specific", user_openids=["user"])
    if change == "scene": payload.update(scope="channel", target_type="specific")
    if change == "total": existing = 20
    with pytest.raises(V2Error):
        validate_panel_payload(payload, existing_panels=existing)
