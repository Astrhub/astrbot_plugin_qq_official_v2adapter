"""Real host managers, dashboard dispatch and Plugin Pages in a disposable root."""

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

import httpx
import jwt
import pytest

from v2 import PLATFORM_TYPE, PLUGIN_NAME


@pytest.mark.assembly
async def test_real_astrbot_assembly(config):
    plugin_dir = Path("/work/data/plugins") / PLUGIN_NAME
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir.mkdir()
    for entry in ("main.py", "metadata.yaml", "_conf_schema.json", "v2", "pages"):
        source = Path("/plugin") / entry
        if source.is_dir():
            shutil.copytree(source, plugin_dir / entry)
        else:
            shutil.copy2(source, plugin_dir / entry)
    from astrbot.core import LogBroker, astrbot_config, db_helper, html_renderer
    from astrbot.core.core_lifecycle import AstrBotCoreLifecycle
    from astrbot.core.platform.register import platform_cls_map
    from astrbot.dashboard.server import AstrBotDashboard

    # These are exclusively fresh /work test settings, not operator configuration.
    astrbot_config["platform"] = [dict(config)]
    astrbot_config["provider"] = []
    astrbot_config["dashboard"]["username"] = "fixture-admin"
    astrbot_config["dashboard"]["jwt_secret"] = "fixture-key-not-a-production-secret-0000000000"
    astrbot_config.save_config()
    lifecycle = AstrBotCoreLifecycle(LogBroker(), db_helper)
    baseline_tasks = set(asyncio.all_tasks())
    native_before = {name for name in sys.modules if name.startswith(("astrbot.core.platform.sources.qqofficial.",
                                                                     "astrbot.core.platform.sources.qqofficial_webhook."))}
    initialized = False
    try:
        await asyncio.wait_for(lifecycle.initialize(), timeout=60)
        initialized = True
        assert not lifecycle.plugin_manager.failed_plugin_dict, lifecycle.plugin_manager.failed_plugin_dict.keys()
        metadata = lifecycle.star_context.get_registered_star(PLUGIN_NAME)
        assert metadata is not None and metadata.version == "v0.1.0"
        owner = metadata.star_cls
        assert owner and not owner.stopping
        assert PLATFORM_TYPE in platform_cls_map
        assert len(owner.instances) == 1
        instance = next(iter(owner.instances))
        # Await the host wrapper rather than guessing from import or run task creation.
        tasks = lifecycle.platform_manager._platform_tasks[instance.client_self_id]
        await asyncio.wait_for(tasks.wrapper, timeout=5)
        assert instance.status.value == "error"
        assert "P0/P1 has no QQ transport" in instance.last_error.message
        assert not (await instance.get_client().get_status())["online"]
        native_after = {name for name in sys.modules if name.startswith(("astrbot.core.platform.sources.qqofficial.",
                                                                        "astrbot.core.platform.sources.qqofficial_webhook."))}
        assert native_after == native_before  # Dashboard itself imports native login_registration.
        assert all("qqoffice_expand" not in name for name in sys.modules)
        old_client = instance.client

        dashboard = AstrBotDashboard(lifecycle, db_helper, lifecycle.dashboard_shutdown_event)
        transport = httpx.ASGITransport(app=dashboard.asgi_app)
        secret = dashboard._jwt_secret
        username = "fixture-admin"
        def auth(**extra):
            token = jwt.encode({"username": username, "exp": int(time.time()) + 600, **extra}, secret, algorithm="HS256")
            return {"Authorization": f"Bearer {token}", "Origin": "http://testserver"}
        prefix = f"/api/v1/plugins/extensions/{PLUGIN_NAME}"
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            assert (await client.get(prefix + "/bootstrap")).status_code == 401
            forbidden = [auth(username="other-user"), auth(token_type="plugin_page_asset"),
                         {**auth(), "Origin": "https://evil.invalid"}, auth(exp=1)]
            for headers in forbidden:
                assert (await client.get(prefix + "/bootstrap", headers=headers)).status_code in (401, 403)
            key = await dashboard.asgi_app.state.services.api_keys.create_api_key({"name": "fixture", "scopes": ["plugin"]}, created_by=username)
            response = await client.get(prefix + "/bootstrap", headers={"Authorization": "ApiKey " + key["api_key"]})
            assert response.status_code == 403 and response.json()["code"] == "management_auth_required"
            headers = auth()
            boot = (await client.get(prefix + "/bootstrap", headers=headers)).json()
            assert boot["instances"][0]["id"] == config["id"]
            view = (await client.get(prefix + "/config", params={"platform_id": config["id"]}, headers=headers)).json()
            assert "secret" not in json.dumps(view)
            catalog = (await client.get(prefix + "/commands", params={"platform_id": config["id"], "scene": "group"}, headers=headers)).json()
            assert any(n["system"] for n in catalog["nodes"])
            assert len([n for n in catalog["nodes"] if n["menu_entry"]]) == 1
            assert all(n["id"] for n in catalog["nodes"])
            payload = {"platform_id": config["id"], "scene": "group", "fingerprint": view["fingerprint"],
                       "revision": view["revision"], "csrf": boot["csrf"], "patch": {"title": "fixture title"}}
            assert (await client.post(prefix + "/config/mutate", json={**payload, "csrf": "invalid", "operation": "save"}, headers=headers)).status_code == 403
            assert (await client.post(prefix + "/config/mutate", json={**payload, "platform_id": "another-bot", "operation": "save"}, headers=headers)).status_code == 404
            assert (await client.post(prefix + "/preview", json=payload, headers=headers)).status_code == 200
            assert owner.store.get(instance.identity.settings_key)["revision"] == 0
            malformed = await client.post(prefix + "/preview", json={**payload, "node": {}}, headers=headers)
            assert malformed.status_code == 400 and malformed.json()["code"] == "invalid_preview"
            expired = owner.control.csrf(username, int(time.time()) - 1)
            assert (await client.post(prefix + "/preview", json={**payload, "csrf": expired}, headers=headers)).status_code == 403
            response = await client.post(prefix + "/flags", json={"csrf": boot["csrf"], "revision": boot["flags_revision"],
                                        "patch": {"remote_menu_sync": True}, "confirm": True}, headers=headers)
            assert response.status_code == 501 and not owner.config.get("remote_menu_sync")
            saved = await client.post(prefix + "/config/mutate", json={**payload, "operation": "save"}, headers=headers)
            assert saved.status_code == 200, saved.text
            assert saved.json()["revision"] == 1
            assert (await client.post(prefix + "/config/mutate", json={**payload, "operation": "save"}, headers=headers)).status_code == 409
            payload["revision"] = 1
            assert (await client.post(prefix + "/config/mutate", json={**payload, "operation": "apply"}, headers=headers)).status_code == 400
            response = await client.post(prefix + "/config/mutate", json={**payload, "operation": "apply", "confirm": True}, headers=headers)
            assert response.status_code == 200 and instance.local_settings["title"] == "fixture title"
            astrbot_config["platform"][0]["secret"] = "rotated-fixture"
            assert (await client.post(prefix + "/preview", json=payload, headers=headers)).status_code == 409
            changed_view = (await client.get(prefix + "/config", params={"platform_id": config["id"]}, headers=headers)).json()
            assert changed_view["runtime_state"] == "reload_required"
            assert instance.identity.robot.appid == config["appid"]
            astrbot_config["platform"][0]["secret"] = config["secret"]
            # Real page discovery, injected SDK, assets and traversal rejection.
            page = await client.get("/api/v1/plugins/page", params={"plugin_id": PLUGIN_NAME, "page_name": "control"}, headers=headers)
            assert page.status_code == 200 and page.json()["status"] == "ok", page.text
            asset_params = {"plugin_id": PLUGIN_NAME, "page_name": "control", "asset_path": "index.html"}
            content = await client.get("/api/v1/plugins/page/assets", params=asset_params, headers=headers)
            assert content.status_code == 200 and "bridge-sdk.js" in content.text and "app.js" in content.text
            asset_params["asset_path"] = "app.js"
            js = await client.get("/api/v1/plugins/page/assets", params=asset_params, headers=headers)
            assert js.status_code == 200 and "window.AstrBotPluginPage" in js.text
            asset_params["asset_path"] = "../../main.py"
            response = await client.get("/api/v1/plugins/page/assets", params=asset_params, headers=headers)
            assert response.status_code != 200 or "QQOfficialV2" not in response.text
            # API switch persists in the host plugin config and can be recovered there.
            response = await client.post(prefix + "/flags", json={"csrf": boot["csrf"], "revision": boot["flags_revision"],
                                  "patch": {"webui_enabled": False}, "confirm": True}, headers=headers)
            assert response.status_code == 200, response.text
            assert (await client.get(prefix + "/bootstrap", headers=headers)).status_code == 403
            owner.config["webui_enabled"] = True
            owner.config.save_config()
            old_handler = owner.control.routes[0]
            # Disable through the actual plugin manager; no surviving routes or platform tasks.
            await lifecycle.plugin_manager.turn_off_plugin(PLUGIN_NAME)
            assert owner.stopping and owner.store.closed and not owner.instances
            assert PLATFORM_TYPE not in platform_cls_map
            assert instance.client_self_id not in lifecycle.platform_manager._platform_tasks
            assert not any(api[0].startswith(f"/{PLUGIN_NAME}/") for api in lifecycle.star_context.registered_web_apis)
            with pytest.raises(RuntimeError) as exc:
                await old_client.get_status()
            assert exc.value.code == "stale_generation"
            assert (await old_handler()).status_code == 503
            await owner.terminate()
            await lifecycle.plugin_manager.turn_on_plugin(PLUGIN_NAME)
            new_owner = lifecycle.star_context.get_registered_star(PLUGIN_NAME).star_cls
            assert new_owner is not owner and not new_owner.stopping
            assert not new_owner.instances  # Hot plugin reload does not silently reconnect.
            assert new_owner.store.get(instance.identity.settings_key)["applied"]["title"] == "fixture title"
            await lifecycle.platform_manager.load_platform(dict(config))
            new_instance = next(iter(new_owner.instances))
            await lifecycle.platform_manager._platform_tasks[new_instance.client_self_id].wrapper
            assert new_instance.identity.generation != instance.identity.generation
            assert new_instance.local_settings["title"] == "fixture title"
            await lifecycle.plugin_manager.turn_off_plugin(PLUGIN_NAME)
        print("ASSEMBLY: real core initialize, plugin load/disable/re-enable, platform wrapper, dashboard auth, Pages, restore and cleanup passed")
    finally:
        if initialized:
            await lifecycle.stop()
        # The host starts metadata fetching outside its task registry; sandbox owns cleanup.
        remaining = set(asyncio.all_tasks()) - baseline_tasks
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        session = getattr(html_renderer.network_strategy, "session", None)
        if session and not session.closed:
            await session.close()
