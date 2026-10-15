<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import AbSelect from "./AbSelect.vue";
import ConnectionPanel from "./ConnectionPanel.vue";
import CommandPanel from "./CommandPanel.vue";
import RemotePanel from "./RemotePanel.vue";
import SdkPanel from "./SdkPanel.vue";
import { changed, clone, connectionFields, createApi } from "./api.js";
import { applyHostContext, t, tError, tState } from "./i18n.js";
import "./styles.css";

const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const status = ref(t("page.waitingBridge"));
const busy = ref(false);
const tab = ref("connection");
const targetInput = ref("qq_v2");
let offContext = null;
let runtimePollTimer = 0;

function isTransitionalRuntime(view) {
  const st = view?.runtime?.state || view?.runtime_state;
  if (["connecting", "waiting_identify", "authenticating"].includes(st)) return true;
  const shards = view?.runtime?.gateway_group?.shards || [];
  return shards.some(sh => ["connecting", "waiting_identify", "authenticating"].includes(sh.state));
}

function scheduleRuntimePoll(attempts = 15) {
  window.clearTimeout(runtimePollTimer);
  if (attempts <= 0 || !isTransitionalRuntime(state.view)) return;
  runtimePollTimer = window.setTimeout(async () => {
    try {
      const next = await bridge.apiGet("connection", { platform_id: state.platformId });
      if (state.view && next.platform_id === state.platformId) {
        state.view.runtime = next.runtime;
        state.view.runtime_state = next.runtime_state;
        state.view.capabilities = next.capabilities;
        state.view.reload = next.reload;
        if (!isTransitionalRuntime(next)) return;
      }
      scheduleRuntimePoll(attempts - 1);
    } catch {
      scheduleRuntimePoll(attempts - 1);
    }
  }, 1000);
}

const dialog = reactive({ open: false, message: "", resolve: null });
function askConfirm(message) {
  return new Promise(resolve => {
    dialog.message = message;
    dialog.resolve = resolve;
    dialog.open = true;
  });
}
function closeDialog(result) {
  const fn = dialog.resolve;
  dialog.open = false;
  dialog.resolve = null;
  if (fn) fn(result);
}

const state = reactive({
  bridge,
  boot: null,
  view: null,
  current: null,
  catalog: null,
  draft: null,
  draftSnapshot: "",
  scene: "group",
  loadedScene: "",
  platformId: "qq_v2",
  scan: null,
  layer: "home",
  previewNode: "",
  previewPage: 0,
  confirmBindings: false,
  restoreRevision: 0,
  extensions: {},
  secret: "",
  secretAction: "keep",
  confirmSecret: false,
  confirmIdentity: false,
  panelTargetType: "all",
  panelTargets: "",
  menuOnly: false,
  form: {
    appid: "", use_markdown: true, type: "qq_official_v2", intents: 1174409216,
    shard_mode: "auto", shardIndex: 0, shardCount: 1, enable: false,
  },
});
const api = createApi(state);

const instanceOptions = computed(() =>
  (state.boot?.instances || []).map(item => ({
    value: item.id,
    label: item.id,
    sublabel: item.appid ? `AppID ${item.appid}` : t("page.unconfigured"),
  }))
);
const sceneOptions = computed(() => [
  { value: "group", label: t("scene.group") },
  { value: "c2c", label: t("scene.c2c") },
  { value: "channel", label: t("scene.channel") },
  { value: "dm", label: t("scene.dm") },
]);

const configuredAppId = computed(() => (state.view?.fields?.appid || state.form.appid || "").trim());
const runtimeDotClass = computed(() => {
  const rt = state.view?.runtime;
  if (rt?.state === "online" && rt?.message_ready) return "ok";
  if (state.view?.exists && configuredAppId.value) return "warn";
  return "";
});

const connectionDirty = computed(() => {
  if (!state.view) return false;
  try {
    const base = {
      appid: state.view.fields?.appid || "",
      use_markdown: state.view.fields?.use_markdown !== false,
      type: state.view.fields?.type || "qq_official_v2",
      intents: Number(state.view.fields?.intents ?? 1174409216),
      shard_mode: state.view.fields?.shard_mode || "auto",
      shard: Array.isArray(state.view.fields?.shard) ? state.view.fields.shard : [0, 1],
      enable: Boolean(state.view.fields?.enable),
    };
    return Object.keys(changed(base, connectionFields(state.form))).length > 0
      || Boolean(state.secret) || state.secretAction !== "keep";
  } catch {
    return true;
  }
});
const draftDirty = computed(() => Boolean(state.current && state.draftSnapshot && JSON.stringify(state.draft) !== state.draftSnapshot));
const dirty = computed(() => connectionDirty.value || draftDirty.value);
const remoteAttention = computed(() => {
  const remote = state.current?.remote_state?.[state.scene];
  return Boolean(remote?.pending || remote?.state === "unknown");
});

function fillConnection(value) {
  const fields = value.fields || {};
  const shard = Array.isArray(fields.shard) ? fields.shard : [0, 1];
  state.view = value;
  state.platformId = value.platform_id;
  targetInput.value = value.platform_id;
  state.form = {
    appid: fields.appid || "",
    use_markdown: fields.use_markdown !== false,
    type: fields.type || "qq_official_v2",
    intents: Number(fields.intents ?? 1174409216),
    shard_mode: fields.shard_mode || "auto",
    shardIndex: Number(shard[0] || 0),
    shardCount: Number(shard[1] || 1),
    enable: Boolean(fields.enable),
  };
  state.secret = "";
  state.secretAction = "keep";
  state.confirmSecret = false;
  state.confirmIdentity = false;
  state.scan = null;
}

function fillConfig(value, catalog) {
  state.current = value;
  state.catalog = catalog;
  state.loadedScene = state.scene;
  state.draft = clone(value.draft);
  state.draftSnapshot = JSON.stringify(state.draft);
  state.extensions = { ...state.draft.extensions };
  state.restoreRevision = value.versions.at(-1) || 0;
  state.previewPage = 0;
  state.previewNode = "";
  state.confirmBindings = false;
}

async function fetchTarget(id) {
  const view = await bridge.apiGet("connection", { platform_id: id });
  if (!view.exists) return { view, current: null, catalog: null };
  const current = await bridge.apiGet("config", { platform_id: id });
  const catalog = await bridge.apiGet("commands", { platform_id: id, scene: state.scene });
  return { view, current, catalog };
}

async function read(force = false) {
  const id = (targetInput.value || state.platformId || "").trim();
  if (!id) throw new Error(t("msg.enterTargetId"));
  if (!force && dirty.value && !(await askConfirm(t("msg.confirmDiscardTarget")))) {
    targetInput.value = state.platformId;
    return;
  }
  const result = await fetchTarget(id);
  fillConnection(result.view);
  if (result.current) fillConfig(result.current, result.catalog);
  else {
    state.current = null;
    state.catalog = null;
    state.draft = null;
    state.draftSnapshot = "";
  }
  status.value = result.view.exists ? t("msg.loadedExisting") : t("msg.loadedNewTarget");
  scheduleRuntimePoll();
}

async function changeScene(nextScene) {
  if (!state.current) return;
  if (dirty.value && !(await askConfirm(t("msg.confirmDiscardScene")))) {
    state.scene = state.loadedScene || "group";
    return;
  }
  state.scene = nextScene;
  const [current, catalog] = await Promise.all([
    bridge.apiGet("config", { platform_id: state.platformId }),
    bridge.apiGet("commands", { platform_id: state.platformId, scene: state.scene }),
  ]);
  fillConfig(current, catalog);
  status.value = t("msg.loadedScene", { scene: t(`scene.${state.scene}`) });
}

async function run(action) {
  if (busy.value) return;
  busy.value = true;
  try { await action(); }
  catch (error) { status.value = tError(error); }
  finally { busy.value = false; }
}

async function reload() { await read(true); }

onMounted(() => run(async () => {
  if (!bridge) throw new Error(t("page.openFromHost"));
  const initialCtx = await bridge.ready();
  applyHostContext(initialCtx || bridge.getContext?.(), bridge);
  if (typeof bridge.onContext === "function") {
    offContext = bridge.onContext(ctx => applyHostContext(ctx, bridge));
  }
  state.boot = await bridge.apiGet("bootstrap");
  targetInput.value = state.boot.instances[0]?.id || "qq_v2";
  await read(true);
}));

onBeforeUnmount(() => {
  window.clearTimeout(runtimePollTimer);
  if (typeof offContext === "function") offContext();
});
</script>

<template>
  <div class="ab-app-layout">
    <aside class="ab-sidebar">
      <div class="ab-brand">
        <span class="ab-brand-dot" :class="runtimeDotClass"></span>
        <span class="ab-brand-title">{{ t("page.title") }}</span>
      </div>

      <nav class="tabs" :aria-label="t('nav.aria')">
        <button :class="{ active: tab === 'connection' }" @click="tab = 'connection'">
          <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
            <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
          </svg>
          <span>{{ t("nav.connection") }}</span>
          <span v-if="connectionDirty" class="nav-badge" aria-hidden="true"></span>
        </button>

        <button :class="{ active: tab === 'commands' }" :disabled="!state.current" @click="tab = 'commands'">
          <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <path d="M4 6h16M4 12h16M4 18h10" />
          </svg>
          <span>{{ t("nav.commands") }}</span>
          <span v-if="draftDirty" class="nav-badge" aria-hidden="true"></span>
        </button>

        <button :class="{ active: tab === 'remote' }" :disabled="!state.current" @click="tab = 'remote'">
          <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <rect x="3" y="4" width="18" height="16" rx="2" />
            <path d="M7 8h10M7 12h6M7 16h8" />
          </svg>
          <span>{{ t("nav.remote") }}</span>
          <span v-if="remoteAttention" class="nav-badge" aria-hidden="true"></span>
        </button>

        <button :class="{ active: tab === 'sdk' }" :disabled="!state.view" @click="tab = 'sdk'">
          <svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
            <polyline points="16 18 22 12 16 6" />
            <polyline points="8 6 2 12 8 18" />
          </svg>
          <span>{{ t("nav.sdk") }}</span>
        </button>
      </nav>
    </aside>

    <div class="ab-main-wrap">
      <header class="page-header">
        <div class="header-status-area">
          <div v-if="state.view" class="runtime-strip">
            <span class="runtime-pill">
              <span class="runtime-dot" :class="runtimeDotClass"></span>
              {{ state.view.platform_id }} · AppID {{ configuredAppId || t("page.unconfigured") }}
            </span>
            <span class="runtime-pill">{{ t("page.statusLabel") }}：{{ tState(state.view.runtime?.state || state.view.runtime_state) }}</span>
            <span class="runtime-pill">{{ t("page.messageLabel") }}：{{ state.view.runtime?.message_ready ? t("page.messageReady") : t("page.messageNotReady") }}</span>
            <span v-if="state.view.runtime?.gateway_group" class="runtime-pill">
              {{ t("page.gatewayLabel") }}：{{ state.view.runtime.gateway_group.connected }}/{{ state.view.runtime.gateway_group.planned || state.view.runtime.gateway_group.recommended || "?" }}
            </span>
          </div>
          <p class="status" role="status" aria-live="polite">{{ status }}</p>
        </div>

        <div class="toolbar target-toolbar">
          <div class="toolbar-field">
            <span class="toolbar-label">{{ t("page.targetId") }}</span>
            <AbSelect
              v-model="targetInput"
              :options="instanceOptions"
              editable
              :disabled="busy"
              @change="run(() => read(false))"
              @enter="run(() => read(false))"
            />
          </div>
          <div class="toolbar-field">
            <span class="toolbar-label">{{ t("page.scene") }}</span>
            <AbSelect
              :model-value="state.scene"
              :options="sceneOptions"
              :disabled="busy"
              @change="val => run(() => changeScene(val))"
            />
          </div>
          <button type="button" class="secondary" :disabled="busy" @click="run(reload)">{{ t("page.reloadBtn") }}</button>
        </div>
        <div v-if="busy" class="progress-line" aria-hidden="true"></div>
      </header>

      <main class="workspace-main">
        <ConnectionPanel v-if="tab === 'connection'" :api="api" :state="state" :busy="busy" :confirm="askConfirm" @status="status = $event" @run="run" @reload="reload" />
        <CommandPanel v-else-if="tab === 'commands'" :api="api" :state="state" :busy="busy" :confirm="askConfirm" @status="status = $event" @run="run" @reload="reload" />
        <RemotePanel v-else-if="tab === 'remote'" :api="api" :state="state" :busy="busy" :confirm="askConfirm" @status="status = $event" @run="run" />
        <SdkPanel v-else :state="state" @status="status = $event" />
      </main>
    </div>

    <div v-if="dirty" class="unsaved-floating-pill" role="status">
      <span class="nav-badge" aria-hidden="true"></span>
      <span>{{ t("page.unsavedNotice") }}</span>
    </div>

    <div v-if="dialog.open" class="ab-modal-backdrop" role="dialog" aria-modal="true" @click.self="closeDialog(false)">
      <div class="ab-modal-card">
        <h3>{{ t("dialog.confirmTitle") }}</h3>
        <p>{{ dialog.message }}</p>
        <div class="actions modal-actions">
          <button type="button" class="secondary" @click="closeDialog(false)">{{ t("dialog.cancelBtn") }}</button>
          <button type="button" @click="closeDialog(true)">{{ t("dialog.confirmBtn") }}</button>
        </div>
      </div>
    </div>
  </div>
</template>
