"""Behavioral and artifact verification for the single-file Vue control view."""

import json
import os
import subprocess
from pathlib import Path

from v2.settings import DEFAULTS

ROOT = Path(__file__).parents[1]


def test_control_view_singlefile_artifact_and_i18n_metadata():
    pages_dir = ROOT / "pages"
    assert [p.name for p in pages_dir.iterdir() if p.is_dir()] == ["control"]
    control_dir = pages_dir / "control"
    assert [p.name for p in control_dir.iterdir()] == ["index.html"]

    html = (control_dir / "index.html").read_text(encoding="utf-8")
    assert "window.AstrBotPluginView" in html and "window.AstrBotPluginPage" in html
    assert "html[data-theme=dark]" in html and "#3c96ca" in html
    assert 'src="' not in html and 'href="' not in html
    assert "localStorage" not in html and "media_roots" not in html and "onebot_network_enabled" not in html
    for src_file in (ROOT / "dash" / "src").glob("*"):
        src_text = src_file.read_text(encoding="utf-8")
        assert "fetch(" not in src_text and "localStorage" not in src_text and "window.confirm" not in src_text
        if src_file.suffix == ".vue":
            template_part = src_text.split("<template>", 1)[-1]
            assert "JSON.stringify" not in template_part

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "onebot_network_enabled" not in schema

    for lang, expected_title in (("zh-CN", "QQ 官方 V2 控制台"), ("en-US", "QQ Official V2 Console")):
        locale_path = ROOT / ".astrbot-plugin" / "i18n" / f"{lang}.json"
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        assert data["views"]["control"]["title"] == expected_title
        assert data["pages"]["control"]["title"] == expected_title
        assert "webui_enabled" in data["config"] and "onebot_network_enabled" not in data["config"]
        assert "display_name" in data["metadata"]

def test_control_view_api_state_machine_and_i18n_theme_sync():
    node = os.environ.get("V2_NODE")
    assert node, "Node is required for the Vue control view state-machine test."
    script = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import { applyHostContext, isDark, locale, t, tError, tScanHint, tState } from "/plugin/dash/src/i18n.js";
import { changed, connectionFields, createApi } from "/plugin/dash/src/api.js";

const defaults = JSON.parse(fs.readFileSync(0, "utf8"));
const attrs = new Map([["data-theme", "light"]]);
globalThis.document = {
  title: "",
  documentElement: {
    lang: "zh-CN",
    getAttribute: (k) => attrs.get(k) || null,
    setAttribute: (k, v) => attrs.set(k, v),
  },
};

const calls = [];
const bridge = {
  getLocale: () => "zh-CN",
  t: (key, fallback = "") => (key === "views.control.title" && locale.value === "en-US" ? "QQ Official V2 Console" : fallback),
  async apiGet(endpoint, params = {}) {
    calls.push(["GET", endpoint, params]);
    if (endpoint === "commands") return { nodes: [{ id: "cmd1", name: "help" }] };
    if (endpoint === "inbox/retained") return { platform_id: params.platform_id, fingerprint: "fp-1", expires: 999, entries: [] };
    throw new Error(endpoint);
  },
  async apiPost(endpoint, body = {}) {
    calls.push(["POST", endpoint, body]);
    assert.equal(body.csrf, "csrf-1");
    if (endpoint === "flags") return { flags: { ...body.patch }, revision: "flags-2" };
    return { ok: true, endpoint, body };
  },
};

applyHostContext({ locale: "en-US", isDark: true }, bridge);
assert.equal(locale.value, "en-US");
assert.equal(isDark.value, true);
assert.equal(attrs.get("data-theme"), "dark");
assert.equal(document.title, "QQ Official V2 Console");
assert.equal(t("nav.connection"), "Connection");
assert.equal(t("nav.sdk"), "SDK Capabilities");
assert.equal(tState("waiting_identify"), "Waiting Identify");
assert.equal(tState("online"), "Online");

applyHostContext({ locale: "zh-CN", isDark: false }, bridge);
assert.equal(locale.value, "zh-CN");
assert.equal(isDark.value, false);
assert.equal(attrs.get("data-theme"), "light");
assert.equal(t("nav.connection"), "连接与接入");
assert.equal(t("nav.sdk"), "SDK 能力清单");
assert.equal(tState("waiting_identify"), "等待鉴权握手");
assert.equal(tState("online"), "在线");
assert.equal(tError(new Error("Confirm creating a QQ binding task.")), "请确认创建 QQ 扫码绑定任务。");
assert.equal(tScanHint({ state: "pending" }), "请使用手机 QQ 扫描二维码。");

assert.deepEqual(
  changed({ a: 1, nested: { keep: true, drop: "x" } }, { a: 1, nested: { keep: true } }),
  { nested: { drop: null } }
);

assert.throws(() => connectionFields({ type: "qq_official_v2", shard_mode: "manual", shardIndex: 2, shardCount: 2 }), /分片/);
assert.deepEqual(connectionFields({ type: "qq_official_v2_webhook", shard_mode: "manual", shardIndex: 1, shardCount: 2 }).shard, [0, 1]);

const normalized = connectionFields({
  appid: " 102030 ",
  use_markdown: true,
  type: "qq_official_v2",
  intents: 33554432,
  shard_mode: "manual",
  shardIndex: 1,
  shardCount: 3,
  enable: false,
});
assert.deepEqual(normalized.shard, [1, 3]);
assert.equal(normalized.appid, "102030");
assert.equal("onebot" in normalized, false);
const state = {
  bridge,
  boot: { csrf: "csrf-1", flags: { remote_menu_sync: false }, flags_revision: "flags-1" },
  view: { platform_id: "qq_v2", fingerprint: "conn-fp" },
  current: { platform_id: "qq_v2", revision: 3, fingerprint: "cfg-fp", draft: structuredClone(defaults) },
  scene: "group",
  platformId: "qq_v2",
  confirmBindings: true,
  scan: { state: "pending", platform_id: "qq_v2", ticket: "old-ticket", commit_handle: "h1" },
};
const api = createApi(state);

await api.startScan();
assert.equal(calls[0][1], "onboarding/cancel");
assert.equal(calls[0][2].ticket, "old-ticket");
assert.equal(calls[1][1], "onboarding/start");
assert.equal(calls[1][2].confirm, true);

state.scan = { state: "pending", platform_id: "qq_v2", ticket: "new-ticket", commit_handle: "h2" };
await api.refreshScan();
const statusCall = calls.at(-1);
assert.equal(statusCall[0], "POST");
assert.equal(statusCall[1], "onboarding/status");
assert.equal(statusCall[2].renew, true);

state.scan = { state: "ready_to_commit", platform_id: "qq_v2", ticket: "new-ticket", commit_handle: "h2" };
await api.commitScan();
const commitCall = calls.at(-1);
assert.equal(commitCall[1], "onboarding/commit");
assert.equal(commitCall[2].confirm, true);
assert.equal(commitCall[2].confirm_secret, true);
assert.equal(commitCall[2].confirm_identity, true);

await api.mutate("apply", { title: "Updated" }, 2);
const mutateCall = calls.at(-1);
assert.equal(mutateCall[1], "config/mutate");
assert.equal(mutateCall[2].confirm, true);
assert.equal(mutateCall[2].confirm_bindings, true);
assert.equal(mutateCall[2].revision, 3);

await api.enablePanels({ target_type: "all", targets: [], menu_only: true, plan_fingerprint: "plan-fp" });
const enableCall = calls.at(-1);
assert.equal(enableCall[1], "panels/enable");
assert.equal(enableCall[2].fingerprint, "cfg-fp");
assert.equal(enableCall[2].plan_fingerprint, "plan-fp");
assert.equal(enableCall[2].confirm, true);

await api.setFlag("remote_menu_sync", true);
assert.equal(state.boot.flags_revision, "flags-2");
assert.equal(state.boot.flags.remote_menu_sync, true);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd="/plugin/dash",
        input=json.dumps(DEFAULTS),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
