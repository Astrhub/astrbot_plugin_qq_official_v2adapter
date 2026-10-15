<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from "vue";
import AbSelect from "./AbSelect.vue";
import { changed, connectionFields } from "./api.js";
import { t, tError, tScanHint, tState } from "./i18n.js";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
  confirm: { type: Function, required: true },
});
const emit = defineEmits(["status", "run", "reload"]);
const canvas = ref(null);
let timer = 0;

const active = computed(() => ["creating", "pending", "ready_to_commit"].includes(props.state.scan?.state));
const ready = computed(() => props.state.scan?.state === "ready_to_commit");
const runtime = computed(() => props.state.view?.runtime || {});
const gatewayGroup = computed(() => runtime.value.gateway_group || null);

const typeOptions = [
  { value: "qq_official_v2", label: "WebSocket" },
  { value: "qq_official_v2_webhook", label: "Webhook" },
];
const shardModeOptions = computed(() => [
  { value: "auto", label: t("conn.shardAuto") },
  { value: "manual", label: t("conn.shardManual") },
]);
const secretActionOptions = computed(() => [
  { value: "keep", label: t("conn.secretKeep") },
  { value: "replace", label: t("conn.secretReplace") },
  { value: "clear", label: t("conn.secretClear") },
]);

function paint(matrix) {
  const node = canvas.value;
  if (!node || !matrix) return;
  const size = matrix.length;
  node.width = node.height = size * 4;
  const context = node.getContext("2d");
  context.fillStyle = "#fff";
  context.fillRect(0, 0, node.width, node.height);
  context.fillStyle = "#202124";
  matrix.forEach((row, y) => row.forEach((dark, x) => { if (dark) context.fillRect(x * 4, y * 4, 4, 4); }));
}
async function show(value) {
  props.state.scan = value;
  emit("status", tScanHint(value));
  await nextTick();
  paint(value?.qr_matrix);
}
function later() {
  window.clearTimeout(timer);
  if (!active.value) return;
  const delay = props.state.scan.state === "creating" || !props.state.scan.qr_matrix ? 400 : 3000;
  timer = window.setTimeout(() => emit("run", async () => {
    try { await props.api.refreshScan(); await show(props.state.scan); later(); }
    catch (error) { props.state.scan = null; emit("status", tError(error)); }
  }), delay);
}
async function start() { await props.api.startScan(); await show(props.state.scan); later(); }
async function cancel() { window.clearTimeout(timer); await props.api.cancelScan(); await show(props.state.scan); }
async function saveScan() { await props.api.commitScan(); emit("reload"); }

function save() {
  emit("run", async () => {
    const form = props.state.form;
    const next = connectionFields(form);
    const secret = props.state.secret;
    if (secret && props.state.secretAction !== "replace") throw new Error(t("conn.errSecretReplace"));
    const base = {
      appid: props.state.view.fields?.appid || "",
      use_markdown: props.state.view.fields?.use_markdown !== false,
      type: props.state.view.fields?.type || "qq_official_v2",
      intents: Number(props.state.view.fields?.intents ?? 1174409216),
      shard_mode: props.state.view.fields?.shard_mode || "auto",
      shard: Array.isArray(props.state.view.fields?.shard) ? props.state.view.fields.shard : [0, 1],
      enable: Boolean(props.state.view.fields?.enable),
    };
    await props.api.saveConnection({
      patch: changed(base, next),
      secret_action: props.state.secretAction,
      ...(secret ? { secret } : {}),
      confirm: true,
      confirm_secret: props.state.confirmSecret,
      confirm_identity: props.state.confirmIdentity,
    });
    props.state.secret = "";
    emit("status", t("conn.savedStatus"));
    emit("reload");
  });
}

async function reloadConnection() {
  if (!(await props.confirm(t("conn.confirmReload")))) return;
  await props.api.reloadConnection();
  emit("status", t("conn.reloadedStatus"));
  emit("reload");
}

function leave() {
  window.clearTimeout(timer);
  if (!active.value || !props.state.scan?.ticket || !props.state.boot) return;
  props.state.bridge.apiPost("onboarding/cancel", {
    csrf: props.state.boot.csrf,
    platform_id: props.state.scan.platform_id,
    ticket: props.state.scan.ticket,
  }).catch(() => {});
}
onMounted(() => window.addEventListener("pagehide", leave));
onBeforeUnmount(() => { window.removeEventListener("pagehide", leave); leave(); });
</script>

<template>
  <section class="panel-grid connection-grid">
    <article class="card">
      <h2>{{ t("conn.statusTitle") }}</h2>
      <dl class="facts">
        <div>
          <dt>{{ t("conn.factType") }}</dt>
          <dd>{{ state.form.type === 'qq_official_v2_webhook' ? 'Webhook' : 'WebSocket' }}</dd>
        </div>
        <div>
          <dt>{{ t("conn.factCredentials") }}</dt>
          <dd>
            <span class="badge" :class="state.view?.credentials_configured ? 'badge-ok' : 'badge-warn'">
              {{ state.view?.credentials_configured ? t("conn.factConfigured") : t("conn.factUnconfigured") }}
            </span>
          </dd>
        </div>
        <div>
          <dt>{{ t("conn.factRuntime") }}</dt>
          <dd>
            <span class="badge" :class="runtime.state === 'online' ? 'badge-ok' : runtime.state === 'degraded' ? 'badge-warn' : 'badge-muted'">
              {{ tState(runtime.state || state.view?.runtime_state) }}
            </span>
          </dd>
        </div>
        <div>
          <dt>{{ t("conn.factMessagePath") }}</dt>
          <dd>
            <span class="badge" :class="runtime.message_ready ? 'badge-ok' : 'badge-muted'">
              {{ runtime.message_ready ? t("page.messageReady") : t("page.messageNotReady") }}
            </span>
          </dd>
        </div>
      </dl>
      <div v-if="state.form.type === 'qq_official_v2_webhook' || state.view?.webhook_path" class="subcard">
        <div class="subcard-head">
          <h3>{{ t("conn.webhookPath") }}</h3>
          <button
            v-if="state.view?.webhook_path"
            type="button"
            class="small-button"
            @click="navigator.clipboard?.writeText?.(state.view.webhook_path)?.catch?.(() => {}); emit('status', t('conn.copiedPath', { path: state.view.webhook_path }))"
          >
            {{ t("conn.copyPath") }}
          </button>
        </div>
        <code v-if="state.view?.webhook_path" class="code-tag">{{ state.view.webhook_path }}</code>
        <p v-else class="muted">{{ t("conn.webhookPending") }}</p>
      </div>

      <div class="subcard">
        <div class="subcard-head">
          <h3>{{ t("conn.gatewayTitle") }}</h3>
          <span v-if="gatewayGroup" class="badge" :class="gatewayGroup.state === 'online' ? 'badge-ok' : 'badge-warn'">
            {{ tState(gatewayGroup.state) }}
          </span>
        </div>
        <div v-if="gatewayGroup" class="gateway-visual">
          <div class="gateway-metrics">
            <span>{{ t("conn.gwRec") }} <strong>{{ gatewayGroup.recommended || gatewayGroup.planned || "?" }}</strong></span>
            <span>{{ t("conn.gwPlanned") }} <strong>{{ gatewayGroup.planned || "?" }}</strong></span>
            <span>{{ t("conn.gwConn") }} <strong>{{ gatewayGroup.connected || 0 }}</strong></span>
          </div>
          <div v-if="gatewayGroup.shards?.length" class="shard-grid">
            <div v-for="sh in gatewayGroup.shards" :key="sh.index" class="shard-item">
              <code>[{{ sh.index }}, {{ sh.count }}]</code>
              <span class="badge" :class="sh.state === 'online' ? 'badge-ok' : 'badge-warn'">{{ tState(sh.state) }}</span>
              <span v-if="sh.failure && sh.state !== 'online'" class="code-tag danger-tag">{{ tState(sh.failure.code) }}</span>
            </div>
          </div>
        </div>
        <p v-else class="muted">{{ t("conn.noGatewayGroup") }}</p>
      </div>

      <div class="scan-box">
        <h3>{{ t("conn.scanTitle") }}</h3>
        <p class="muted">{{ t("conn.scanDesc") }}</p>
        <canvas ref="canvas" v-show="state.scan?.qr_matrix" class="qr"></canvas>
        <p v-if="!state.scan?.qr_matrix" class="muted">{{ t("conn.scanEmpty") }}</p>
        <div class="actions">
          <button type="button" :disabled="busy || !state.view" @click="emit('run', start)">{{ t("conn.scanStart") }}</button>
          <button v-if="active" type="button" class="secondary" :disabled="busy" @click="emit('run', cancel)">{{ t("conn.scanCancel") }}</button>
          <button v-if="ready" type="button" class="success" :disabled="busy" @click="emit('run', saveScan)">{{ t("conn.scanCommit") }}</button>
        </div>
      </div>
    </article>

    <form class="card" @submit.prevent="save">
      <h2>{{ t("conn.formTitle") }}</h2>
      <div class="form-grid">
        <label>
          {{ t("conn.appid") }}
          <input v-model="state.form.appid" maxlength="128">
        </label>
        <label>
          {{ t("conn.type") }}
          <AbSelect v-model="state.form.type" :options="typeOptions" />
        </label>
        <label class="wide check-row">
          <input v-model="state.form.use_markdown" type="checkbox">
          {{ t("conn.useMarkdown") }}
        </label>
        <label class="wide check-row">
          <input v-model="state.form.enable" type="checkbox">
          {{ t("conn.enable") }}
        </label>
        <label class="wide">
          {{ t("conn.intents") }}
          <input v-model.number="state.form.intents" type="number" min="0" max="4294967295">
        </label>
      </div>

      <fieldset v-if="state.form.type === 'qq_official_v2'">
        <legend>{{ t("conn.shardLegend") }}</legend>
        <label>
          {{ t("conn.shardMode") }}
          <AbSelect v-model="state.form.shard_mode" :options="shardModeOptions" />
        </label>
        <div v-if="state.form.shard_mode === 'manual'" class="form-grid">
          <label>{{ t("conn.shardIndex") }}<input v-model.number="state.form.shardIndex" type="number" min="0" max="1023"></label>
          <label>{{ t("conn.shardCount") }}<input v-model.number="state.form.shardCount" type="number" min="1" max="1024"></label>
        </div>
      </fieldset>

      <div class="form-grid">
        <label>
          {{ t("conn.secretAction") }}
          <AbSelect v-model="state.secretAction" :options="secretActionOptions" />
        </label>
        <label>
          {{ t("conn.newSecret") }}
          <input v-model="state.secret" type="password" maxlength="512" autocomplete="new-password" :placeholder="t('conn.newSecretPlaceholder')">
        </label>
      </div>
      <label class="check-row"><input v-model="state.confirmSecret" type="checkbox"> {{ t("conn.confirmSecret") }}</label>
      <label class="check-row"><input v-model="state.confirmIdentity" type="checkbox"> {{ t("conn.confirmIdentity") }}</label>

      <div class="actions" style="margin-top: 16px;">
        <button type="submit" :disabled="busy || !state.view">{{ t("conn.saveBtn") }}</button>
        <button type="button" class="secondary" :disabled="busy || !state.view" @click="emit('run', reloadConnection)">{{ t("conn.reloadInstanceBtn") }}</button>
      </div>
    </form>
  </section>
</template>
