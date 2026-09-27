"""Handler fingerprints must depend on code, not live constant reference counts."""
import functools
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
    before = binding_fingerprint(plugin, handler, [], [])
    retained = function.__code__.co_consts[1]
    assert retained == "callback accepted"
    assert binding_fingerprint(plugin, handler, [], []) == before
    function.__code__ = function.__code__.replace(co_consts=(None, "changed callback result"))
    assert binding_fingerprint(plugin, handler, [], []) != before


async def test_retaining_callback_constant_does_not_invalidate_an_issued_ticket(callback_case, monkeypatch):
    _, event, star, _ = callback_case
    namespace = {}
    exec(compile('def make_callback():\n    seen = []\n    async def callback(self, event):\n        seen.append(event)\n        yield event.plain_result("callback constant fixture")\n    return callback\n',
                 "<callback-ticket-fixture>", "exec"), namespace)
    function = star.confirm.__func__
    monkeypatch.setattr(function, "__code__", namespace["make_callback"]().__code__)
    button = event.qq.callback_button(star.confirm, label="确认")
    retained = function.__code__.co_consts[1]
    assert retained == "callback constant fixture"
    await event.send(MessageChain([Json(card(button))]))
    assert event.get_extra("qq_send_result")["state"] == "sent"
