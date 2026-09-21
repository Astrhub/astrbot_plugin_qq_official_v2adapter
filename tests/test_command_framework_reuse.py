"""Catalog reads use host metadata APIs without mutating runtime commands."""
import functools
import json
from types import SimpleNamespace

import pytest
from astrbot.core.star import command_management
from astrbot.core.star import context as host_context
from astrbot.core.star.filter.command import CommandFilter
from astrbot.core.star.filter.command_group import CommandGroupFilter
from astrbot.core.star.star import StarMetadata
from astrbot.core.star.star_handler import StarHandlerRegistry
from test_settings_commands import fixtures

from v2 import commands


def freeze(value):
    if isinstance(value, dict):
        return tuple((key, freeze(item)) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(freeze(item) for item in value)
    return value


@pytest.fixture
def host_catalog(monkeypatch):
    handlers, plugins, _, child, command = fixtures()
    stars = [StarMetadata(module_path=module, **vars(plugin)) for module, plugin in plugins.items()]
    by_module = {star.module_path: star for star in stars}
    registry = StarHandlerRegistry()
    for handler in handlers:
        star = by_module[handler.handler_module_path]
        star.star_handler_full_names.append(handler.handler_full_name)
        handler.handler = functools.partial(handler.handler, star.star_cls)
        registry.append(handler)
    lookups = []
    class RegistryAPI:
        def __iter__(self):
            return iter(registry)
        def get_handler_by_full_name(self, full_name):
            lookups.append(full_name)
            return registry.get_handler_by_full_name(full_name)
    monkeypatch.setattr(commands, "star_handlers_registry", RegistryAPI())
    monkeypatch.setattr(host_context, "star_registry", stars)
    def forbidden(*args, **kwargs):
        raise AssertionError("Catalog must not bind command configs or execute runtime filters")
    monkeypatch.setattr(command_management, "list_commands", forbidden)
    monkeypatch.setattr(command_management, "sync_command_configs", forbidden)
    monkeypatch.setattr(command_management, "_apply_config_to_runtime", forbidden)
    monkeypatch.setattr(CommandFilter, "filter", forbidden)
    monkeypatch.setattr(CommandGroupFilter, "filter", forbidden)
    context = object.__new__(host_context.Context)
    return SimpleNamespace(context=context, stars=stars, registry=registry, handlers=handlers,
                           child=child, command=command, lookups=lookups, plugins=by_module)


def readonly_catalog(env, config, scene="group"):
    objects = [env.registry, *env.stars, *env.handlers,
               *(f for h in env.handlers for f in h.event_filters)]
    before = {id(obj): freeze(vars(obj)) for obj in objects}
    config_before = freeze(config)
    result = commands.collect_catalog(config, scene, context=env.context)
    assert before == {id(obj): freeze(vars(obj)) for obj in objects}
    assert freeze(config) == config_before
    assert env.lookups and set(env.lookups) == {h.handler_full_name for h in env.registry}
    return result


def test_actual_host_partial_disabled_entries_aliases_and_pure_preview(host_catalog):
    e = host_catalog
    e.child.enabled = False
    config = {"wake_prefix": ["!"], "plugin_set": ["*"]}
    first = readonly_catalog(e, config)
    child = next(n for n in first["nodes"] if n["id"] == e.child.handler_full_name)
    assert child["binding"] and child["reasons"] == ["handler_disabled"]
    assert child["permission"] == ["admin"] and child["command"] == "!admin child"
    assert child["aliases"] == ["admin child", "admin childalias"]
    assert "do-not-leak" not in json.dumps(first)
    assert all(f._cmpl_cmd_names is None for h in e.handlers for f in h.event_filters if isinstance(f, CommandFilter))
    e.child.enabled = True
    second = readonly_catalog(e, config)
    node = next(n for n in second["nodes"] if n["id"] == child["id"])
    assert node["enabled"] and node["binding"] == child["binding"]
    assert second["version"] != first["version"]
    e.command.command_name = "renamed"
    e.command.alias = {"alias-changed"}
    renamed = readonly_catalog(e, config)
    node = next(n for n in renamed["nodes"] if n["id"] == child["id"])
    assert node["command"] == "!admin renamed" and node["aliases"] == ["admin alias-changed", "admin renamed"]


def test_nested_group_name_cache_is_also_read_only(host_catalog):
    e = host_catalog
    group = next(f for h in e.handlers for f in h.event_filters if isinstance(f, CommandGroupFilter))
    parent = CommandGroupFilter("root", alias={"r"})
    group.parent_group = parent
    before = freeze(vars(parent))
    catalog = readonly_catalog(e, {})
    assert before == freeze(vars(parent))
    assert any(n["aliases"] == ["r admin", "root admin"] for n in catalog["nodes"])
    assert group._cmpl_cmd_names is None and parent._cmpl_cmd_names is None


def test_real_host_snapshot_tracks_disable_unload_and_reload_without_old_handler(host_catalog):
    e = host_catalog
    config = {"wake_prefix": ["/"]}
    original = readonly_catalog(e, config)
    child = next(n for n in original["nodes"] if n["id"] == e.child.handler_full_name)
    star = e.plugins[e.child.handler_module_path]
    star.activated, star.star_cls = False, None
    disabled = readonly_catalog(e, config)
    assert all("plugin_disabled" in n["reasons"] for n in disabled["nodes"] if n["plugin"] == star.name)
    e.stars.remove(star)
    assert not any(n["plugin"] == star.name for n in readonly_catalog(e, config)["nodes"])
    star.activated, star.star_cls = True, object()
    e.stars.append(star)
    for handler in e.handlers:
        if handler.handler_module_path == star.module_path:
            handler.handler = functools.partial(handler.handler.func, star.star_cls)
    reloaded = readonly_catalog(e, config)
    assert next(n for n in reloaded["nodes"] if n["id"] == child["id"])["binding"] == child["binding"]
    old = e.child
    replacement = type(old)(old.event_type, old.handler_full_name, old.handler_name, old.handler_module_path,
                            old.handler, list(old.event_filters), old.desc)
    e.registry.append(replacement)
    e.handlers.append(replacement)
    nodes = readonly_catalog(e, config)["nodes"]
    assert len([n for n in nodes if n["id"] == old.handler_full_name]) == 1
    assert next(n for n in nodes if n["id"] == old.handler_full_name)["enabled"]
    async def changed(self, event):
        raise AssertionError("Reloaded handler must never execute during preview")
    replacement.handler = functools.partial(changed, star.star_cls)
    changed_node = next(n for n in readonly_catalog(e, config)["nodes"] if n["id"] == child["id"])
    assert changed_node["binding"] != child["binding"]


def test_effective_config_and_permissions_remain_projections(host_catalog):
    e = host_catalog
    profile = {"wake_prefix": ["#"], "plugin_set": [], "disable_builtin_commands": True}
    catalog = readonly_catalog(e, profile, "c2c")
    assert all(not n["enabled"] for n in catalog["nodes"])
    assert all(n["command"].startswith("#") for n in catalog["nodes"])
    assert next(n for n in catalog["nodes"] if n["id"] == e.child.handler_full_name)["permission"] == ["admin"]
    assert all(h.enabled for h in e.handlers)


@pytest.mark.parametrize("binding", ["other_instance", "kwargs", "extra_args"])
def test_unrecognized_partial_never_claims_a_verified_binding(host_catalog, binding):
    e = host_catalog
    raw, instance = e.child.handler.func, e.child.handler.args[0]
    if binding == "other_instance":
        e.child.handler = functools.partial(raw, object())
    elif binding == "kwargs":
        e.child.handler = functools.partial(raw, instance, secret="synthetic-hidden")
    else:
        e.child.handler = functools.partial(raw, instance, object())
    catalog = readonly_catalog(e, {})
    node = next(n for n in catalog["nodes"] if n["id"] == e.child.handler_full_name)
    assert node["binding"] is None and "synthetic-hidden" not in json.dumps(catalog)
