"""Real host managers, dashboard dispatch and Plugin Pages in a disposable root."""

import asyncio
import json
import shutil
import sys
import time
from functools import partial
from pathlib import Path

import httpx
import jwt
import pytest
from inbox_assembly import exercise_inbox_recovery

from v2 import PLATFORM_TYPE, PLUGIN_NAME


@pytest.mark.assembly
async def test_real_astrbot_assembly(config, monkeypatch, qq_reject_server, qq_portal_server):
    plugin_dir = Path("/work/data/plugins") / PLUGIN_NAME
    plugin_dir.parent.mkdir(parents=True, exist_ok=True)
    plugin_dir.mkdir()
    for entry in ("main.py", "metadata.yaml", "_conf_schema.json", "requirements.txt", "v2", "pages"):
        source = Path("/plugin") / entry
        if source.is_dir():
            shutil.copytree(source, plugin_dir / entry)
        else:
            shutil.copy2(source, plugin_dir / entry)
    import importlib

    from test_transport_http import MappedSession
    def map_qq():
        module = importlib.import_module(f"data.plugins.{PLUGIN_NAME}.v2.transport.http")
        monkeypatch.setattr(module.HTTPTransport, "_make_session", lambda self: MappedSession(qq_reject_server[0]))
    map_qq()
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
        assert metadata is not None and metadata.version == "v0.4.0"
        owner = metadata.star_cls
        assert owner and not owner.stopping
        assert PLATFORM_TYPE in platform_cls_map
        assert len(owner.instances) == 1
        instance = next(iter(owner.instances))
        # Await the host wrapper rather than guessing from import or run task creation.
        tasks = lifecycle.platform_manager._platform_tasks[instance.client_self_id]
        await asyncio.wait_for(tasks.wrapper, timeout=5)
        assert instance.status.value == "error"
        assert "QQ API rejected" in instance.last_error.message
        assert instance.failure == "qq_api_error" and instance.http.session.closed
        assert instance.runtime_status()["failure_details"]["business_code"] == 100016
        assert instance.runtime_status()["failure_details"]["http_status"] == 200
        assert qq_reject_server[1] == ["/app/getAppAccessToken"]
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
            await exercise_inbox_recovery(owner, instance, client, prefix, headers, boot, key["api_key"], auth)
            catalog = (await client.get(prefix + "/commands", params={"platform_id": config["id"], "scene": "group"}, headers=headers)).json()
            assert any(n["system"] for n in catalog["nodes"])
            assert len([n for n in catalog["nodes"] if n["menu_entry"]]) == 1
            assert all(n["id"] for n in catalog["nodes"])
            from astrbot.core.star.star_handler import star_handlers_registry
            from test_command_framework_reuse import freeze
            menu_node = next(n for n in catalog["nodes"] if n["menu_entry"])
            menu_handler = star_handlers_registry.get_handler_by_full_name(menu_node["id"])
            assert type(menu_handler.handler) is partial and menu_handler.handler.args == (owner,)
            assert menu_node["binding"]
            runtime = [menu_handler, *menu_handler.event_filters]
            snapshot = [freeze(vars(obj)) for obj in runtime]
            catalog_again = (await client.get(prefix + "/commands", params={"platform_id": config["id"], "scene": "group"}, headers=headers)).json()
            assert catalog_again == catalog and snapshot == [freeze(vars(obj)) for obj in runtime]
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
                                        "patch": {"onebot_network_enabled": True}, "confirm": True}, headers=headers)
            assert response.status_code == 501 and not owner.config.get("onebot_network_enabled")
            response = await client.post(prefix + "/flags", json={"csrf": boot["csrf"], "revision": boot["flags_revision"],
                                        "patch": {"remote_menu_sync": True}, "confirm": True}, headers=headers)
            assert response.status_code == 200 and owner.config["remote_menu_sync"]
            boot["flags_revision"] = response.json()["revision"]
            plan_request = {**payload, "menu_only": True}
            assert (await client.post(prefix + "/panels/plan", json=plan_request, headers={"Authorization": "ApiKey " + key["api_key"]})).status_code == 403
            assert (await client.post(prefix + "/panels/plan", json={**plan_request, "csrf": "bad"}, headers=headers)).status_code == 403
            plan_response = await client.post(prefix + "/panels/plan", json=plan_request, headers=headers)
            assert plan_response.status_code == 200 and not plan_response.json()["issues"]
            assert len(qq_reject_server[1]) == 1  # Opt-in switch and local preview do not publish.
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
            map_qq()
            await lifecycle.platform_manager.load_platform(dict(config))
            new_instance = next(iter(new_owner.instances))
            await lifecycle.platform_manager._platform_tasks[new_instance.client_self_id].wrapper
            assert new_instance.identity.generation != instance.identity.generation
            assert new_instance.local_settings["title"] == "fixture title"
            reloaded_catalog = (await client.get(prefix + "/commands", params={"platform_id": config["id"], "scene": "group"}, headers=headers)).json()
            reloaded_menu = next(n for n in reloaded_catalog["nodes"] if n["menu_entry"])
            assert reloaded_menu["binding"] == menu_node["binding"] and reloaded_menu["enabled"]
            assert star_handlers_registry.get_handler_by_full_name(reloaded_menu["id"]).handler.args == (new_owner,)
            # P2: real host save/reload, QR lease/commit and unified callback dispatch.
            from test_transport_receive import StepClock, event, signed
            clock = StepClock()
            http_module = importlib.import_module(f"data.plugins.{PLUGIN_NAME}.v2.transport.http")
            monkeypatch.setattr(http_module.HTTPTransport, "_make_session", lambda self: MappedSession(qq_portal_server[0]))
            new_owner.onboarding.factory = lambda: MappedSession(qq_portal_server[0])
            new_owner.onboarding.clock = lambda: clock.now
            new_owner.onboarding.sleep = clock.sleep
            boot2 = (await client.get(prefix + "/bootstrap", headers=headers)).json()
            target = "webhook-fixture"
            connection = (await client.get(prefix + "/connection", params={"platform_id": target}, headers=headers)).json()
            save_body = {"csrf": boot2["csrf"], "platform_id": target, "fingerprint": connection["fingerprint"], "patch": {"transport": "webhook"}, "confirm": True}
            key_headers = {"Authorization": "ApiKey " + key["api_key"]}
            assert (await client.post(prefix + "/connection/save", json=save_body, headers=key_headers)).status_code == 403
            response = await client.post(prefix + "/connection/save", json=save_body, headers=headers)
            assert response.status_code == 200, response.text
            connection = response.json()
            assert connection["webhook_path"] and not connection["fields"]["enable"]
            assert len([p for p in astrbot_config["platform"] if p["id"] == target]) == 1
            assert not any(i.identity.platform_id == target for i in new_owner.instances)
            start_body = {"csrf": boot2["csrf"], "platform_id": target, "fingerprint": connection["fingerprint"], "confirm": True}
            assert (await client.post(prefix + "/onboarding/start", json=start_body, headers=key_headers)).status_code == 403
            response = await client.post(prefix + "/onboarding/start", json=start_body, headers=headers)
            assert response.status_code == 200, response.text
            ticket = response.json()["ticket"]
            await clock.tick()
            await asyncio.wait_for(clock.waits.get(), 2)
            status_body = {"csrf": boot2["csrf"], "platform_id": target, "ticket": ticket, "renew": True}
            ready = (await client.post(prefix + "/onboarding/status", json=status_body, headers=headers)).json()
            assert ready["state"] == "ready_to_commit" and "new-fixture-secret" not in json.dumps(ready)
            assert (await client.post(prefix + "/onboarding/status", json={**status_body, "platform_id": config["id"]}, headers=headers)).status_code == 404
            commit_body = {**status_body, "commit_handle": ready["commit_handle"], "confirm": True, "confirm_secret": True, "confirm_identity": True}
            committed = await client.post(prefix + "/onboarding/commit", json=commit_body, headers=headers)
            assert committed.status_code == 200, committed.text
            assert committed.json() == (await client.post(prefix + "/onboarding/commit", json=commit_body, headers=headers)).json()
            saved_target = next(p for p in astrbot_config["platform"] if p["id"] == target)
            assert saved_target["secret"] == "new-fixture-secret" and not saved_target["enable"]
            assert "not-a-chat-observation" not in json.dumps(astrbot_config.get("admins_id", []))
            connection = committed.json()["result"]
            response = await client.post(prefix + "/connection/save", json={**save_body, "fingerprint": connection["fingerprint"], "patch": {"enable": True}}, headers=headers)
            assert response.status_code == 200, response.text
            connection = response.json()
            response = await client.post(prefix + "/connection/reload", json={"csrf": boot2["csrf"], "platform_id": target, "fingerprint": connection["fingerprint"], "confirm": True}, headers=headers)
            assert response.status_code == 200, response.text
            hook_instance = next(i for i in new_owner.instances if i.identity.platform_id == target)
            await asyncio.wait_for(hook_instance.ready.wait(), 2)
            assert hook_instance.state == "webhook_ready" and not hook_instance.runtime_status()["online"]
            callback = connection["webhook_path"]
            challenge = {"op": 13, "d": {"plain_token": "fixture", "event_ts": str(int(time.time()))}}
            response = await client.post(callback, json=challenge, headers={"X-Bot-Appid": "new-fixture-app"})
            assert response.status_code == 200 and response.json()["plain_token"] == "fixture"
            assert not hook_instance.runtime_status()["online"]
            fixture_request = signed(event("assembly-raw"), appid="new-fixture-app", secret="new-fixture-secret", now=int(time.time()))
            response = await client.post(callback, content=fixture_request.raw, headers=dict(fixture_request.headers))
            assert response.status_code == 200 and response.json() == {"op": 12}, response.text
            assert hook_instance.runtime_status()["online"] and not hook_instance.client._state.cache.items
            assert new_owner.inbox.count(hook_instance.identity.settings_key) == 1
            assert (await client.post(callback, content=fixture_request.raw, headers=dict(fixture_request.headers))).status_code == 200
            assert new_owner.inbox.count(hook_instance.identity.settings_key) == 1
            assert (await client.post(callback, content=fixture_request.raw, headers={**dict(fixture_request.headers), "X-Bot-Appid": "wrong"})).status_code == 401
            from messaging_assembly import messaging_roundtrip
            await messaging_roundtrip(lifecycle, client, headers, new_owner, hook_instance, callback, monkeypatch)
            connection = (await client.get(prefix + "/connection", params={"platform_id": target}, headers=headers)).json()
            response = await client.post(prefix + "/connection/save", json={**save_body, "fingerprint": connection["fingerprint"], "patch": {"enable": False}}, headers=headers)
            assert response.status_code == 200, response.text
            await lifecycle.platform_manager._platform_tasks[hook_instance.client_self_id].wrapper
            assert hook_instance.http.session.closed and hook_instance.ingress.worker.done()
            assert (await client.post(callback, content=fixture_request.raw, headers=dict(fixture_request.headers))).status_code == 503
            response = await client.post(prefix + "/connection/reload", json={"csrf": boot2["csrf"], "platform_id": target, "fingerprint": response.json()["fingerprint"], "confirm": True}, headers=headers)
            assert response.status_code == 200, response.text
            assert (await client.post(callback, content=fixture_request.raw, headers=dict(fixture_request.headers))).status_code == 404
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
