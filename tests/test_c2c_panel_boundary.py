import pytest
from test_messaging_help_panels import panel_env as panel_env
from test_messaging_send import sending as sending

from v2.errors import V2Error
from v2.protocol import RequestSpec


async def test_c2c_all_panel_is_distinct_from_global_custom_menu(panel_env):
    e = panel_env
    plan = e.service.plan(e.instance, "c2c")
    assert not e.calls and not plan["issues"]
    assert plan["payload"]["scope"] == "c2c" and plan["payload"]["target_type"] == "all"
    assert "user_openids" not in plan["payload"]
    with pytest.raises(V2Error) as exc:
        await e.service.enable(e.instance, "c2c", plan["fingerprint"], confirm=True)
    assert exc.value.code == "remote_sync_disabled" and not e.calls
    e.owner.config["remote_menu_sync"] = True
    with pytest.raises(V2Error) as exc:
        await e.service.enable(e.instance, "c2c", plan["fingerprint"], confirm=False)
    assert exc.value.code == "confirmation_required" and not e.calls
    result = await e.service.enable(e.instance, "c2c", plan["fingerprint"], confirm=True)
    assert result["state"] == "synced"
    writes = [(method, path, body) for method, path, body in e.calls if method != "GET"]
    assert len(writes) == 1 and writes[0][:2] == ("POST", "/v2/panels")
    assert writes[0][2]["scope"] == "c2c" and writes[0][2]["target_type"] == "all"
    e.handlers[0].desc = "updated"
    await e.service.sync(e.instance, "c2c")
    assert e.calls[-1][0:2] == ("PUT", "/v2/panels/" + result["panel_id"])
    assert all(path != "/v2/menu" for _, path, _ in e.calls)


async def test_unsupported_menu_write_cannot_bypass_native_surface(sending):
    s = sending
    with pytest.raises(V2Error):
        await s.client.qq.request(RequestSpec("production", "PUT", "/v2/menu", json_body={"menu": {"items": []}}))
    assert not s.calls
