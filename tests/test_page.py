"""Behavioral and artifact verification for the single-file Vue control view."""

import json
import os
import subprocess
from pathlib import Path

from v2.connection_config import DEFAULT_INTENTS
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
    assert "配置事件订阅" in html and "Configure Event Subscriptions" in html
    assert "消息与存储" in html and "Messages & Storage" in html
    assert "保存并应用存储策略" in html and "Save and apply storage policy" in html
    assert "全文检索" in html and "Full-text search" in html
    assert "LIKE 降级" in html and "LIKE fallback" in html
    assert "资料库" in html and "Profile store" in html
    assert "保存并应用资料库设置" in html and "Save and apply profile store settings" in html
    assert "管理确认已过期，已刷新，请再点击一次。" in html
    assert "Management confirmation expired and was refreshed. Click again to continue." in html
    assert "管理确认已过期且刷新失败，请点击「刷新」后重试。" in html
    assert "Management confirmation expired and refresh failed. Click Refresh, then retry." in html
    for src_file in (ROOT / "dash" / "src").glob("*"):
        src_text = src_file.read_text(encoding="utf-8")
        assert "fetch(" not in src_text and "localStorage" not in src_text and "window.confirm" not in src_text
        if src_file.suffix == ".vue":
            template_part = src_text.split("<template>", 1)[-1]
            assert "JSON.stringify" not in template_part

    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    assert "onebot_network_enabled" not in schema
    for key in ("profile_max_records", "profile_max_bytes", "profile_stale_seconds", "profile_cooldown_seconds"):
        assert key not in schema

    for lang, expected_title in (("zh-CN", "QQ 官方 V2 控制台"), ("en-US", "QQ Official V2 Console")):
        locale_path = ROOT / ".astrbot-plugin" / "i18n" / f"{lang}.json"
        data = json.loads(locale_path.read_text(encoding="utf-8"))
        assert data["views"]["control"]["title"] == expected_title
        assert data["pages"]["control"]["title"] == expected_title
        assert "webui_enabled" in data["config"] and "onebot_network_enabled" not in data["config"]
        for key in ("profile_max_records", "profile_max_bytes", "profile_stale_seconds", "profile_cooldown_seconds"):
            assert key not in data["config"]
        assert "display_name" in data["metadata"]

def test_connection_panel_intents_dialog_cancel_and_apply_contract():
    """The dialog drafts locally: canceling must not touch the form, applying must write an integer mask."""
    import re

    src = (ROOT / "dash" / "src" / "ConnectionPanel.vue").read_text(encoding="utf-8")
    cancel_body = re.search(r"function cancelIntents\(\) \{(.*?)\n\}", src, re.S).group(1)
    assert "form.intents" not in cancel_body
    apply_body = re.search(r"function applyIntents\(\) \{(.*?)\n\}", src, re.S).group(1)
    assert "form.intents" in apply_body
    assert 'role="dialog"' in src and 'aria-modal="true"' in src and "aria-labelledby" in src
    assert '@keydown.esc.prevent="cancelIntents"' in src
    assert re.search(r"watch\(\s*\(\) => props\.state\.view", src)


def test_runtime_policy_placement_and_shared_draft_actions():
    connection = (ROOT / "dash" / "src" / "ConnectionPanel.vue").read_text(encoding="utf-8")
    command = (ROOT / "dash" / "src" / "CommandPanel.vue").read_text(encoding="utf-8")

    for field in (
        "media_max_bytes",
        "stream_fallback",
        "stream_max_chars",
        "stream_timeout",
        "typing_enabled",
        "management_writes",
    ):
        assert f"state.extensions.{field}" in connection
        assert f"state.extensions.{field}" not in command

    for field in ("ticket_ttl", "keyboard_enabled", "keyboard_execute"):
        assert f"state.extensions.{field}" in command
    assert "<DraftActions" not in connection and "<DraftActions" in command
    assert "runtimePolicyFields" in connection and "runtime_policy" in connection
    assert "draftPatch(props.state)" in (ROOT / "dash" / "src" / "DraftActions.vue").read_text(encoding="utf-8")
    storage = (ROOT / "dash" / "src" / "MessageStoragePanel.vue").read_text(encoding="utf-8")
    assert "savePolicy" in storage and "DraftActions" not in storage
    assert "connection/save" not in storage and "saveConnection" not in storage
    assert "canDeleteRange" in storage and "deleteNeedsFilter" in storage
    assert "rangeParams" in storage
    i18n_src = (ROOT / "dash" / "src" / "i18n.js").read_text(encoding="utf-8")
    assert "storage.savePolicy" in i18n_src and "nav.storage" in i18n_src
    app = (ROOT / "dash" / "src" / "App.vue").read_text(encoding="utf-8")
    assert "storageDirty" in app and "tab === 'storage'" in app
    assert "defaultStoragePolicy" in app and "MessageStoragePanel" in app
    assert 't("nav.storage")' in app
    api_src = (ROOT / "dash" / "src" / "api.js").read_text(encoding="utf-8")
    assert "saveHistoryPolicy" in api_src
    assert "max_age_seconds: null" in api_src
    assert "86400" not in api_src and "86400" not in app and "86400" not in storage
    assert "saveProfile" in storage and "saveProfilePolicy" in api_src
    assert "profileDirty" in app and "defaultProfilePolicy" in app
    assert "storage.saveProfile" in i18n_src and "storage.profileTitle" in i18n_src
    assert 'v-model="query.q"' in storage
    assert "storage.search" in i18n_src and "storage.searchModeLike" in i18n_src
    assert "profile/status" in api_src and "profile/policy" in api_src
    assert "state/status" in api_src and "state/policy" in api_src
    assert "saveStatePolicy" in api_src and "defaultStatePolicy" in app and "stateDirty" in app
    assert "storage.stateTitle" in i18n_src and "storage.saveState" in i18n_src
    assert "statePolicy" in storage and "stateBounds" in storage


def test_control_view_api_state_machine_and_i18n_theme_sync():
    node = os.environ.get("V2_NODE")
    assert node, "Node is required for the Vue control view state-machine test."
    script = r"""
import assert from "node:assert/strict";
import fs from "node:fs";
import { applyHostContext, isDark, locale, t, tError, tScanHint, tState } from "/plugin/dash/src/i18n.js";
import { changed, connectionFields, createIntentsEditor, createApi, defaultStoragePolicy, draftPatch, KNOWN_INTENT_BITS, normalizeStoragePolicy, toUint32, unknownIntentBits, withIntentBit } from "/plugin/dash/src/api.js";

const payload = JSON.parse(fs.readFileSync(0, "utf8"));
const defaults = payload.defaults;
const defaultIntents = payload.default_intents;
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
assert.equal(t("nav.storage"), "Messages & Storage");
assert.equal(t("storage.deleteNeedsFilter"), "Range delete needs a scene, target, or time filter. Use the confirmation phrase to purge everything.");
assert.equal(t("nav.sdk"), "SDK Capabilities");
assert.equal(tState("waiting_identify"), "Waiting Identify");
assert.equal(tState("online"), "Online");
assert.equal(tError({ code: "csrf_expired" }), "Management confirmation expired and was refreshed. Click again to continue.");
assert.equal(tError({ code: "csrf_refresh_failed" }), "Management confirmation expired and refresh failed. Click Refresh, then retry.");

applyHostContext({ locale: "zh-CN", isDark: false }, bridge);
assert.equal(locale.value, "zh-CN");
assert.equal(isDark.value, false);
assert.equal(attrs.get("data-theme"), "light");
assert.equal(t("nav.connection"), "连接与接入");
assert.equal(t("nav.storage"), "消息与存储");
assert.equal(t("storage.deleteNeedsFilter"), "删除当前筛选需要先指定场景、目标或时间范围。全量清空请使用确认词。");
const unlimited = defaultStoragePolicy();
assert.equal(unlimited.mode, "unlimited");
assert.equal(unlimited.max_age_seconds, null);
assert.equal(unlimited.max_count, null);
assert.equal(unlimited.max_bytes, null);
assert.deepEqual(normalizeStoragePolicy({ mode: "unlimited", max_age_seconds: 86400, max_count: 1 }), unlimited);
assert.equal(normalizeStoragePolicy({ mode: "max_count", max_count: 1 }).max_count, 1);
assert.equal(normalizeStoragePolicy({ mode: "max_count", max_count: 1 }).max_age_seconds, null);
assert.equal(t("nav.sdk"), "SDK 能力清单");
assert.equal(tState("waiting_identify"), "等待鉴权握手");
assert.equal(tState("online"), "在线");
assert.equal(tError(new Error("Confirm creating a QQ binding task.")), "请确认创建 QQ 扫码绑定任务。");
assert.equal(tError({ code: "csrf_expired" }), "管理确认已过期，已刷新，请再点击一次。");
assert.equal(tError(new Error("Reload the page to obtain a management confirmation token.")), "管理确认已过期，请再点击一次。");
assert.equal(tError({ response: { data: { code: "csrf_expired" } } }), "管理确认已过期，已刷新，请再点击一次。");
assert.equal(tScanHint({ state: "pending" }), "请使用手机 QQ 扫描二维码。");

assert.deepEqual(
  changed({ a: 1, nested: { keep: true, drop: "x" } }, { a: 1, nested: { keep: true } }),
  { nested: { drop: null } }
);

const draftState = {
  current: { draft: { title: "Menu", extensions: { media_max_bytes: 100, keyboard_enabled: false } } },
  draft: { title: "Menu", extensions: { media_max_bytes: 100, keyboard_enabled: false } },
  extensions: { media_max_bytes: 200, keyboard_enabled: true },
};
assert.deepEqual(draftPatch(draftState), { extensions: { keyboard_enabled: true } });

assert.throws(() => connectionFields({ type: "qq_official_v2", shard_mode: "manual", shardIndex: 2, shardCount: 2 }), /分片/);
assert.deepEqual(connectionFields({ type: "qq_official_v2_webhook", shard_mode: "manual", shardIndex: 1, shardCount: 2 }).shard, [0, 1]);

assert.deepEqual(KNOWN_INTENT_BITS.map(e => e.name), ["PUBLIC_GUILD_MESSAGES", "INTERACTION", "GROUP_AND_C2C_EVENT", "DIRECT_MESSAGE"]);
const EXPECTED_BITS = { PUBLIC_GUILD_MESSAGES: 30, INTERACTION: 26, GROUP_AND_C2C_EVENT: 25, DIRECT_MESSAGE: 12 };
for (const { name, bit } of KNOWN_INTENT_BITS) assert.equal(bit, EXPECTED_BITS[name]);
const knownSum = KNOWN_INTENT_BITS.reduce((sum, { bit }) => sum + 2 ** bit, 0);
assert.equal(knownSum, defaultIntents);
assert.equal((2 ** 30) + (2 ** 26) + (2 ** 25) + (2 ** 12), defaultIntents);
assert.equal(toUint32(-1), 4294967295);
assert.equal(unknownIntentBits(4294967295), 4294967295 - defaultIntents);
assert.equal(withIntentBit(4294967295, 30, true), 4294967295);
assert.equal(withIntentBit(4294967295, 30, false), 4294967295 - 2 ** 30);

// A saved legacy mask with unlisted bits must keep them across toggles.
const legacyMask = defaultIntents + (1 << 9) + 3;
assert.equal(unknownIntentBits(legacyMask), (1 << 9) + 3);
assert.equal(t("conn.intentsUnknownRetained", { mask: 515 }), "另有未列出的订阅掩码 515，将原样保留。");
const editor = createIntentsEditor(legacyMask);
assert.equal(editor.value, legacyMask);
assert.equal(editor.has(30) && editor.has(26) && editor.has(25) && editor.has(12), true);
editor.toggle(25, false);
assert.equal(editor.value, legacyMask - 2 ** 25);
assert.equal(editor.unknownBits(), (1 << 9) + 3);
editor.toggle(12, false);
assert.equal(editor.value, legacyMask - 2 ** 25 - 2 ** 12);
editor.setDecimal("0");
assert.equal(editor.valid, true);
editor.toggle(26, true);
const applied = editor.apply();
assert.equal(applied, 2 ** 26);
assert.equal(Number.isInteger(applied), true);
const afterApply = connectionFields({ appid: "102030", use_markdown: true, type: "qq_official_v2", intents: applied, shard_mode: "auto", enable: false });
assert.equal(afterApply.intents, 2 ** 26);
assert.equal(Number.isInteger(afterApply.intents), true);

// Invalid decimal input blocks apply and toggles instead of corrupting the draft.
const guarded = createIntentsEditor(defaultIntents);
guarded.setDecimal("3.5");
assert.equal(guarded.valid, false);
guarded.toggle(30, false);
assert.equal(Number.isNaN(guarded.value), true);
assert.equal(guarded.apply(), null);

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
assert.equal(typeof api.historyStatus, "function");
assert.equal(typeof api.saveHistoryPolicy, "function");
assert.equal(typeof api.profileStatus, "function");
assert.equal(typeof api.saveProfilePolicy, "function");
assert.equal(typeof api.stateStatus, "function");
assert.equal(typeof api.saveStatePolicy, "function");
assert.equal(t("storage.stateTitle"), "运行时状态库");
assert.equal(t("storage.saveState"), "保存并应用状态库设置");
assert.equal(t("storage.profileTitle"), "资料库");
assert.equal(t("storage.saveProfile"), "保存并应用资料库设置");
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

function csrfState(nextBridge, csrf) {
  return {
    bridge: nextBridge,
    boot: { csrf, flags: { remote_menu_sync: false }, flags_revision: "flags-1" },
    view: { platform_id: "qq_v2", fingerprint: "conn-fp" },
    current: { platform_id: "qq_v2", revision: 3, fingerprint: "cfg-fp", draft: structuredClone(defaults) },
    scene: "group",
    platformId: "qq_v2",
    confirmBindings: false,
    scan: null,
  };
}

const expiredCalls = [];
const expiredBridge = {
  async apiGet(endpoint) {
    expiredCalls.push(["GET", endpoint]);
    assert.equal(endpoint, "bootstrap");
    return { csrf: "fresh-csrf", flags: {}, flags_revision: "flags-1" };
  },
  async apiPost(endpoint, body = {}) {
    expiredCalls.push(["POST", endpoint, body.csrf]);
    if (body.csrf !== "fresh-csrf") {
      throw {
        code: "csrf_expired",
        csrf: "attacker-csrf",
        token: "attacker-token",
        secret: "attacker-secret",
        message: "Reload the page to obtain a management confirmation token.",
      };
    }
    return { ok: true, endpoint };
  },
};
const expiredState = csrfState(expiredBridge, "stale-csrf");
const expiredApi = createApi(expiredState);
await assert.rejects(() => expiredApi.mutate("save", { title: "once" }), (err) => {
  assert.equal(err.code, "csrf_expired");
  assert.equal(err.message, t("msg.csrfExpiredRetry"));
  return true;
});
assert.equal(expiredState.boot.csrf, "fresh-csrf");
assert.deepEqual(expiredCalls, [["POST", "config/mutate", "stale-csrf"], ["GET", "bootstrap"]]);
expiredCalls.length = 0;
const saved = await expiredApi.mutate("save", { title: "once" });
assert.equal(saved.ok, true);
assert.deepEqual(expiredCalls, [["POST", "config/mutate", "fresh-csrf"]]);

const otherCalls = [];
const otherBridge = {
  async apiGet(endpoint) { otherCalls.push(["GET", endpoint]); return {}; },
  async apiPost() { otherCalls.push(["POST"]); throw new Error("Confirm creating a QQ binding task."); },
};
await assert.rejects(() => createApi(csrfState(otherBridge, "csrf-1")).mutate("save", { title: "x" }), /Confirm creating/);
assert.deepEqual(otherCalls, [["POST"]]);

const failCalls = [];
const failState = csrfState({
  async apiGet() { failCalls.push("GET"); throw new Error("bootstrap down"); },
  async apiPost() {
    failCalls.push("POST");
    const err = new Error("Reload the page to obtain a management confirmation token.");
    err.code = "csrf_expired";
    throw err;
  },
}, "stale-csrf");
await assert.rejects(() => createApi(failState).saveConnection({ confirm: true }), (err) => {
  assert.equal(err.code, "csrf_refresh_failed");
  assert.equal(err.message, t("msg.csrfRefreshFailed"));
  return true;
});
assert.equal(failState.boot.csrf, "stale-csrf");
assert.deepEqual(failCalls, ["POST", "GET"]);
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", script],
        cwd="/plugin/dash",
        input=json.dumps({"defaults": DEFAULTS, "default_intents": DEFAULT_INTENTS}),
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_storage_panels_shrink_below_viewport_with_long_paths():
    """Absolute SQLite paths must wrap, and grid cards must be allowed to shrink."""
    css = (ROOT / "dash" / "src" / "styles.css").read_text(encoding="utf-8")
    assert "grid-template-columns: repeat(2, minmax(0, 1fr));" in css
    assert "overflow-wrap: anywhere;" in css
    storage = (ROOT / "dash" / "src" / "MessageStoragePanel.vue").read_text(encoding="utf-8")
    assert 't("storage.path")' in storage and "stateUsage" in storage
