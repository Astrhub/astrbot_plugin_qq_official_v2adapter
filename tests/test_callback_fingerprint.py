"""Handler fingerprints must depend on code, not live constant reference counts."""
import functools
import os
import subprocess
import sys
import types
from types import SimpleNamespace

from astrbot.core.message.components import Json
from astrbot.core.message.message_event_result import MessageChain
from test_button_callbacks import callback_case as callback_case
from test_button_callbacks import card
from test_extension_dispatch import dispatch as dispatch
from test_lifecycle import plugin_module as plugin_module
from test_messaging_delivery import receiver as receiver

from v2.commands import binding_fingerprint


def test_handler_fingerprint_is_stable_when_a_return_constant_is_retained():
    namespace = {}
    exec(compile('def callback(self, event):\n    return "callback accepted"\n', "<callback-fixture>", "exec"), namespace)
    function = namespace["callback"]
    instance = object()
    plugin = SimpleNamespace(name="callback-fixture", star_cls=instance)
    handler = SimpleNamespace(handler=functools.partial(function, instance),
                              handler_module_path="fixture.callback", handler_full_name="fixture.callback_confirm")
    constant_index = next(index for index, value in enumerate(function.__code__.co_consts)
                          if isinstance(value, str) and value == "callback accepted")
    before = binding_fingerprint(plugin, handler, [], [])
    retained = function.__code__.co_consts[constant_index]
    assert retained == "callback accepted"
    assert binding_fingerprint(plugin, handler, [], []) == before
    function.__code__ = function.__code__.replace(co_consts=tuple(
        "changed callback result" if index == constant_index else value
        for index, value in enumerate(function.__code__.co_consts)))
    assert binding_fingerprint(plugin, handler, [], []) != before


async def test_retaining_callback_constant_does_not_invalidate_an_issued_ticket(callback_case, monkeypatch):
    _, event, star, _ = callback_case
    namespace = {}
    exec(compile('def make_callback():\n    seen = []\n    async def callback(self, event):\n        seen.append(event)\n        yield event.plain_result("callback constant fixture")\n    return callback\n',
                 "<callback-ticket-fixture>", "exec"), namespace)
    function = star.confirm.__func__
    monkeypatch.setattr(function, "__code__", namespace["make_callback"]().__code__)
    constant_index = next(index for index, value in enumerate(function.__code__.co_consts)
                          if isinstance(value, str) and value == "callback constant fixture")
    button = event.qq.callback_button(star.confirm, label="确认")
    retained = function.__code__.co_consts[constant_index]
    assert retained == "callback constant fixture"
    await event.send(MessageChain([Json(card(button))]))
    assert event.get_extra("qq_send_result")["state"] == "sent"


_NESTED_SOURCE = (
    'async def callback(self, event):\n'
    '    values = ("alpha", "beta", (7, 11))\n'
    '    async def nested(value):\n'
    '        return ("nested constant", values, value in {"plum", "oak", "pear", "melon", "fig", "apple", "mango"})\n'
    '    yield await nested(event)\n'
)


def test_nested_function_tuple_set_code_fingerprint_is_reproducible_across_processes():
    namespace = {}
    exec(compile(_NESTED_SOURCE, "<nested-fingerprint-fixture>", "exec"), namespace)
    function = namespace["callback"]
    instance = object()
    plugin = SimpleNamespace(name="nested-fixture", star_cls=instance)
    handler = SimpleNamespace(handler=functools.partial(function, instance),
                              handler_module_path="fixture.nested", handler_full_name="fixture.nested_callback")
    before = binding_fingerprint(plugin, handler, [], [])
    nested = next(value for value in function.__code__.co_consts if isinstance(value, types.CodeType))
    assert any(isinstance(value, tuple) for value in function.__code__.co_consts)
    assert any(isinstance(value, frozenset) for value in nested.co_consts)
    constant_index = next(index for index, value in enumerate(nested.co_consts)
                          if isinstance(value, str) and value == "nested constant")
    retained = (function.__code__.co_consts, nested.co_consts, nested.co_consts[constant_index])
    assert retained[2] == "nested constant"
    assert binding_fingerprint(plugin, handler, [], []) == before
    compiled = {}
    exec(compile(_NESTED_SOURCE, "<nested-fingerprint-fixture>", "exec"), compiled)
    handler.handler = functools.partial(compiled["callback"], instance)
    assert binding_fingerprint(plugin, handler, [], []) == before

    child = (
        'import functools\n'
        'from types import SimpleNamespace\n'
        'from v2.commands import binding_fingerprint\n'
        f'source = {_NESTED_SOURCE!r}\n'
        'namespace = {}\n'
        'exec(compile(source, "<nested-fingerprint-fixture>", "exec"), namespace)\n'
        'instance = object()\n'
        'plugin = SimpleNamespace(name="nested-fixture", star_cls=instance)\n'
        'handler = SimpleNamespace(handler=functools.partial(namespace["callback"], instance), '
        'handler_module_path="fixture.nested", handler_full_name="fixture.nested_callback")\n'
        'print(binding_fingerprint(plugin, handler, [], []))\n'
    )
    for seed in ("0", "1", "random"):
        result = subprocess.check_output(
            [sys.executable, "-c", child], env={**os.environ, "PYTHONHASHSEED": seed}, text=True, timeout=20,
        )
        assert result.strip().splitlines()[-1] == before

    changed = nested.replace(co_consts=tuple("changed nested result" if value == "nested constant" else value
                                             for value in nested.co_consts))
    function.__code__ = function.__code__.replace(co_consts=tuple(changed if value is nested else value
                                                               for value in function.__code__.co_consts))
    handler.handler = functools.partial(function, instance)
    assert binding_fingerprint(plugin, handler, [], []) != before
