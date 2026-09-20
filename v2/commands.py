"""Read effective host command metadata without executing filters or handlers."""

import hashlib
import inspect
import json
import marshal
import types
import typing
from collections import Counter

from astrbot.core.star.filter.command import CommandFilter, GreedyStr
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot.core.star.filter.permission import PermissionTypeFilter
from astrbot.core.star.filter.platform_adapter_type import (
    PlatformAdapterType,
    PlatformAdapterTypeFilter,
)
from astrbot.core.star.star import star_map
from astrbot.core.star.star_handler import EventType, star_handlers_registry

from . import PLUGIN_NAME
from .errors import V2Error
from .models import SCENES
from .settings import effective_layout


def binding_fingerprint(plugin, handler, ancestry, params):
    function = handler.handler.__func__ if inspect.ismethod(handler.handler) else handler.handler
    if not inspect.isfunction(function):
        return None
    source = hashlib.sha256(marshal.dumps(function.__code__)).hexdigest()
    value = [plugin.name, handler.handler_module_path, handler.handler_full_name, source,
             [parent.handler_full_name for parent, _ in ancestry], params]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parameter_help(command):
    result = []
    for name, value in getattr(command, "handler_params", {}).items():
        required = (isinstance(value, (type, types.UnionType))
                    or typing.get_origin(value) is typing.Union or value is inspect.Parameter.empty)
        greedy = value is GreedyStr
        simple_type = value if required else type(value)
        type_name = {str: "str", int: "int", float: "float", bool: "bool", GreedyStr: "GreedyStr"}.get(simple_type, "unknown") if isinstance(simple_type, type) else "unknown"
        if typing.get_origin(value) in (typing.Union, types.UnionType):
            type_name = "union (host parser)"
        sensitive = any(word in name.lower() for word in ("secret", "token", "password", "key", "credential"))
        safe_default = not required and not sensitive and (value is None or type(value) in (bool, int, float))
        # Strings are hidden: even an innocent-looking parameter may default to a credential.
        result.append({"name": name, "required": required and not greedy, "greedy": greedy,
                       "type": type_name, "default": value if safe_default else None,
                       "default_visible": safe_default, "description": "未提供参数说明"})
    return result


def collect_catalog(config, scene, *, handlers=None, plugins=None):
    if scene not in SCENES:
        raise V2Error("invalid_scene", "Unknown scene.")
    handlers = list(star_handlers_registry.star_handlers_map.values()) if handlers is None else list(handlers)
    plugins = star_map if plugins is None else plugins
    command_filters = {}
    for handler in handlers:
        for item in handler.event_filters:
            if isinstance(item, (CommandFilter, CommandGroupFilter)):
                command_filters[id(item)] = (handler, item)
    parents = {}
    for handler, item in command_filters.values():
        if isinstance(item, CommandGroupFilter):
            for sub in item.sub_command_filters:
                parents[id(sub)] = id(item)
    prefixes = config.get("wake_prefix", [])
    prefix = next((p for p in prefixes if isinstance(p, str)), "")
    nodes = []
    for filter_id, (handler, command) in command_filters.items():
        if handler.event_type != EventType.AdapterMessageEvent:
            continue
        plugin = plugins.get(handler.handler_module_path)
        if plugin is None:
            continue
        ancestry = []
        parent_id = parents.get(filter_id)
        visited = {filter_id}
        while parent_id in command_filters and parent_id not in visited:
            visited.add(parent_id)
            ancestry.insert(0, command_filters[parent_id])
            parent_id = parents.get(parent_id)
        chain = ancestry + [(handler, command)]
        names = [item.group_name if isinstance(item, CommandGroupFilter) else item.command_name for _, item in chain]
        full_name = " ".join(names)
        reasons, permissions, conditions = [], [], []
        if not plugin.activated or plugin.star_cls is None:
            reasons.append("plugin_disabled")
        if not all(h.enabled for h, _ in chain):
            reasons.append("handler_disabled")
        if config.get("disable_builtin_commands") and handler.handler_module_path == "astrbot.builtin_stars.builtin_commands.main":
            reasons.append("builtin_commands_disabled")
        plugin_set = config.get("plugin_set", ["*"])
        if plugin_set != ["*"] and plugin.name not in plugin_set and not plugin.reserved:
            reasons.append("plugin_set_excluded")
        for h, item in chain:
            filters = list(h.event_filters) + list(item.custom_filter_list)
            for f in filters:
                if isinstance(f, PermissionTypeFilter):
                    permissions.append(f.permission_type.name.lower())
                elif isinstance(f, PlatformAdapterTypeFilter):
                    if f.platform_type is None or not f.platform_type & PlatformAdapterType.ALL:
                        reasons.append("platform_filter_mismatch")
                elif isinstance(f, EventMessageTypeFilter):
                    flag = EventMessageType.GROUP_MESSAGE if scene in ("group", "channel") else EventMessageType.PRIVATE_MESSAGE
                    if not f.event_message_type & flag:
                        reasons.append("scene_filter_mismatch")
                elif not isinstance(f, (CommandFilter, CommandGroupFilter)):
                    conditions.append("custom_filter_runtime_check")
        params = parameter_help(command) if isinstance(command, CommandFilter) else []
        usage = prefix + full_name
        for param in params:
            usage += " " + (f"<{param['name']}>" if param["required"] else f"[{param['name']}]")
        nodes.append({"id": handler.handler_full_name, "parent": ancestry[-1][0].handler_full_name if ancestry else None,
                      "binding": binding_fingerprint(plugin, handler, ancestry, params),
                      "plugin": plugin.name, "system": bool(plugin.reserved and handler.handler_module_path.startswith("astrbot.builtin_stars.")),
                      "menu_entry": plugin.name == PLUGIN_NAME and handler.handler_name == "menu",
                      "name": full_name, "command": prefix + full_name, "usage": usage,
                      "aliases": sorted(command.get_complete_command_names()),
                      "description": handler.desc or "未提供说明", "parameters": params,
                      "permission": sorted(set(permissions)) or ["member"],
                      "conditions": sorted(set(conditions)), "reasons": sorted(set(reasons)),
                      "enabled": not reasons, "group": isinstance(command, CommandGroupFilter),
                      "compatibility": "依赖旧事件类型或数字 ID 的插件可能需要适配"})
    # Parent/child command prefixes are intentional; exact competing invocations are not.
    counts = Counter(alias for n in nodes if n["enabled"] for alias in set(n["aliases"]))
    for node in nodes:
        if node["enabled"] and any(counts[a] > 1 for a in node["aliases"]):
            node["enabled"] = False
            node["reasons"].append("command_conflict")
    data = {"nodes": nodes, "scene": scene, "prefix": prefix,
            "scope": "host_default_preview", "warning": "默认配置预览，不代表所有会话的权限/前缀；不会执行指令。"}
    data["version"] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return data


def official_length(text):
    return sum(1 if ord(c) < 128 else 2 for c in text)


def panel_preview(catalog, settings):
    selection = settings["panels"][catalog["scene"]]
    nodes = {node["id"]: node for node in catalog["nodes"]}
    entries = [node for node in nodes.values() if node["menu_entry"] and node["enabled"]]
    selected = ([n["id"] for n in nodes.values() if n["system"] and n["enabled"]]
                if selection["mode"] == "default" else selection["selected"])
    issues = []
    if len(entries) != 1:
        issues.append("menu_entry_unavailable_or_ambiguous")
    ids = [i for i in selected if not nodes.get(i, {}).get("menu_entry")] + [n["id"] for n in entries]
    items = []
    for identity in ids:
        node = nodes.get(identity)
        if not node or not node["enabled"]:
            issues.append(f"unavailable:{identity}")
            continue
        if official_length(node["command"]) > 14:
            issues.append(f"name_too_long:{identity}")
        description = node["description"].splitlines()[0]
        while official_length(description) > 30:
            description = description[:-1]
        items.append({"id": identity, "type": "command", "name": node["command"],
                      "desc": description, "only_admin": False})
    if len(ids) > 20:
        issues.append("panel_items_over_20")
    return {"scope": catalog["scene"], "target_type": "all", "items": items,
            "slots": len(ids), "limit": 20, "issues": issues,
            "publishable": False, "remote_state": "not_implemented",
            "note": "名称不截断；描述可缩短；QQ only_admin 不等于 AstrBot 管理员权限。"}


def validate_panel_payload(payload, *, existing_panels=0):
    if not isinstance(payload, dict) or payload.get("scope") not in SCENES:
        raise V2Error("invalid_panel", "Unknown panel scope.")
    if type(existing_panels) is not int or not 0 <= existing_panels < 20:
        raise V2Error("panel_limit", "A robot can have at most 20 panels.")
    scene, target = payload["scope"], payload.get("target_type", "all")
    if target not in ("all", "specific") or (scene in ("channel", "dm") and target != "all"):
        raise V2Error("invalid_panel", "Invalid target type for scene.")
    for field, expected in (("user_openids", "c2c"), ("group_openids", "group")):
        ids = payload.get(field, [])
        if not isinstance(ids, list) or len(ids) > 20 or any(not isinstance(i, str) or not i for i in ids):
            raise V2Error("invalid_panel", "Invalid panel targets.")
        if ids and (scene != expected or target != "specific"):
            raise V2Error("invalid_panel", "Target IDs do not match the scene.")
    panel = payload.get("panel", {})
    if not isinstance(panel, dict) or not isinstance(panel.get("items", []), list):
        raise V2Error("invalid_panel", "Invalid panel object.")
    items = panel.get("items", [])
    if len(items) > 20 or not isinstance(panel.get("remark", ""), str) or official_length(panel.get("remark", "")) > 255:
        raise V2Error("panel_limit", "Panel item or remark limit exceeded.")
    for item in items:
        if not isinstance(item, dict) or item.get("type") not in ("command", "link"):
            raise V2Error("invalid_panel", "Unknown panel item type.")
        for key, limit in (("name", 14), ("desc", 30)):
            if not isinstance(item.get(key, ""), str) or official_length(item.get(key, "")) > limit:
                raise V2Error("invalid_panel", "Panel text exceeds the documented limit.")
        if type(item.get("only_admin", False)) is not bool:
            raise V2Error("invalid_panel", "only_admin must be boolean.")
        if item["type"] == "link" and not str(item.get("link", "")).startswith("https://"):
            raise V2Error("invalid_panel", "Panel links must use HTTPS.")
    return payload


def layout_preview(catalog, settings, *, layer="home", node=None, page=0):
    if layer not in ("home", "plugin", "group", "detail") or type(page) is not int or page < 0:
        raise V2Error("invalid_preview", "Invalid preview layer or page.")
    if node is not None and (not isinstance(node, str) or len(node) > 512):
        raise V2Error("invalid_preview", "Invalid preview node.")
    layout, sources = effective_layout(settings, catalog["scene"], layer, node)
    candidates = [n for n in catalog["nodes"] if n["enabled"]]
    if layer == "home":
        candidates = [n for n in candidates if n["parent"] is None]
    elif layer == "plugin":
        candidates = [n for n in candidates if n["plugin"] == node and n["parent"] is None]
    elif layer == "group":
        candidates = [n for n in candidates if n["parent"] == node]
    else:
        candidates = [n for n in candidates if n["id"] == node]
    count = min(layout["page_size"], 5 * layout["columns"] - 4)
    pages = max(1, (len(candidates) + count - 1) // count)
    if page >= pages:
        raise V2Error("invalid_preview", "Page is outside the result.")
    return {"layout": layout, "inheritance": sources, "page": page, "pages": pages,
            "page_size": count, "total": len(candidates), "navigation_slots": 4,
            "items": candidates[page * count:(page + 1) * count],
            "note": "本地文本预览；导航预留4格，未发送 Markdown/键盘，非 QQ 客户端截图。"}
