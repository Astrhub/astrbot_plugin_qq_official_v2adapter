<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { changed, connectionFields } from "./api";

const props = defineProps({ api: { type: Object, required: true }, state: { type: Object, required: true }, busy: Boolean });
const emit = defineEmits(["status", "run", "reload"]);
const canvas = ref(null);
let timer = 0;
const active = computed(() => ["creating", "pending", "ready_to_commit"].includes(props.state.scan?.state));
const ready = computed(() => props.state.scan?.state === "ready_to_commit");
const runtime = computed(() => props.state.view?.runtime || {});
const shardStatus = computed(() => {
  const group = runtime.value.gateway_group;
  if (!group) return "当前没有 WebSocket 网关组状态。";
  const shards = (group.shards || []).map(item => `[${item.index},${item.count}] ${item.state}${item.failure ? ` / ${item.failure.code}` : ""}`).join("；");
  return `QQ 建议 ${group.recommended || group.planned || "?"} / 已计划 ${group.planned || "?"} / 已连接 ${group.connected || 0} · ${group.state || "unknown"}${shards ? ` · ${shards}` : ""}`;
});

function paint(matrix) {
  const node = canvas.value;
  if (!node || !matrix) return;
  const size = matrix.length;
  node.width = node.height = size * 4;
  const context = node.getContext("2d");
  context.fillStyle = "#fff"; context.fillRect(0, 0, node.width, node.height);
  context.fillStyle = "#202124";
  matrix.forEach((row, y) => row.forEach((dark, x) => { if (dark) context.fillRect(x * 4, y * 4, 4, 4); }));
}
function show(value) {
  props.state.scan = value;
  emit("status", value?.hint || "扫码状态更新了。");
  requestAnimationFrame(() => paint(value?.qr_matrix));
}
function later() {
  window.clearTimeout(timer);
  if (!active.value) return;
  const delay = props.state.scan.state === "creating" || !props.state.scan.qr_matrix ? 400 : 5000;
  timer = window.setTimeout(() => emit("run", async () => {
    try { await props.api.refreshScan(); show(props.state.scan); later(); }
    catch (error) { props.state.scan = null; emit("status", error?.message || "扫码状态已失效，请重新生成。"); }
  }), delay);
}
async function start() { await props.api.startScan(); show(props.state.scan); later(); }
async function cancel() { window.clearTimeout(timer); await props.api.cancelScan(); show(props.state.scan); }
async function saveScan() { await props.api.commitScan(); emit("reload"); }
function save() {
  const form = props.state.form;
  const next = connectionFields(form);
  const secret = props.state.secret;
  if (secret && props.state.secretAction !== "replace") throw new Error("输入了新的 AppSecret 后请选择替换。");
  if (props.state.networkToken && props.state.networkTokenAction !== "replace") throw new Error("输入了新的网络 token 后请选择替换。");
  if (!window.confirm("保存连接配置。保存后不会自动重载实例，是否继续？")) return;
  emit("run", async () => {
    await props.api.saveConnection({
      patch: changed(props.state.view.fields, next),
      secret_action: props.state.secretAction,
      ...(secret ? { secret } : {}),
      confirm: true,
      confirm_secret: props.state.confirmSecret,
      confirm_identity: props.state.confirmIdentity,
      network_token_action: props.state.networkTokenAction,
      ...(props.state.networkToken ? { network_token: props.state.networkToken } : {}),
      confirm_network_token: props.state.confirmNetworkToken,
      confirm_network_writes: props.state.confirmNetworkWrites,
    });
    props.state.secret = ""; props.state.networkToken = "";
    emit("status", "连接已保存。请重新读取或重载实例确认运行状态。");
    emit("reload");
  });
}
async function reloadConnection() {
  if (!window.confirm("按已保存的配置重载实例？启用的平台会开始连接 QQ。")) return;
  await props.api.reloadConnection();
  emit("status", "已提交重载请求。");
  emit("reload");
}
async function toggleNetworkGate() {
  const enabled = !props.state.boot.flags.onebot_network_enabled;
  const message = enabled ? "打开迁移期 OneBot 网络总闸？" : "关闭网络总闸？这会撤销所有实例的监听。";
  if (!window.confirm(message)) return;
  await props.api.setFlag("onebot_network_enabled", enabled);
  emit("status", enabled ? "网络总闸已打开，实例重载后生效。" : "网络总闸已关闭，现有监听已撤销。");
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
      <h2>连接与 SDK 状态</h2>
      <p class="muted">页面只调用经过认证的 ControlAPI；QQ 消息、媒体和管理写入由当前 SDK 与统一操作账本处理。</p>
      <dl class="facts">
        <div><dt>平台类型</dt><dd>{{ state.form.type === 'qq_official_v2_webhook' ? 'Webhook' : 'WebSocket' }}</dd></div>
        <div><dt>凭据</dt><dd>{{ state.view?.credentials_configured ? '已配置' : '未配置' }}</dd></div>
        <div><dt>运行状态</dt><dd>{{ runtime.state || state.view?.runtime_state || 'not_loaded' }}</dd></div>
        <div><dt>消息路径</dt><dd>{{ runtime.message_ready ? 'message_ready' : '未就绪' }}</dd></div>
      </dl>
      <pre class="status-box">{{ shardStatus }}</pre>
      <details><summary>SDK 能力</summary><pre class="status-box">{{ JSON.stringify(state.view?.capabilities || {}, null, 2) }}</pre></details>
      <div class="scan-box">
        <h3>QQ 扫码接入</h3>
        <p class="muted">二维码凭据保存后仍保持平台关闭，需要显式启用并重载。</p>
        <canvas ref="canvas" v-show="state.scan?.qr_matrix" class="qr"></canvas>
        <p v-if="!state.scan?.qr_matrix" class="muted">尚未生成二维码。</p>
        <div class="actions">
          <button :disabled="busy || !state.view" @click="emit('run', start)">生成二维码</button>
          <button v-if="active" class="secondary" :disabled="busy" @click="emit('run', cancel)">取消</button>
          <button v-if="ready" class="success" :disabled="busy" @click="emit('run', saveScan)">保存扫码凭据</button>
        </div>
      </div>
    </article>

    <form class="card" @submit.prevent="save">
      <h2>连接配置</h2>
      <div class="form-grid">
        <label>AppID<input v-model="state.form.appid" maxlength="128"></label>
        <label>平台类型<select v-model="state.form.type"><option value="qq_official_v2">WebSocket</option><option value="qq_official_v2_webhook">Webhook</option></select></label>
        <label class="wide"><input v-model="state.form.use_markdown" type="checkbox"> 默认使用 Markdown（未显式指定格式的文本）</label>
        <label class="wide"><input v-model="state.form.enable" type="checkbox"> 保存后启用平台（重载后连接 QQ）</label>
        <label>事件意图<input v-model.number="state.form.intents" type="number" min="0" max="4294967295"></label>
      </div>
      <fieldset v-if="state.form.type === 'qq_official_v2'"><legend>WebSocket 分片</legend>
        <label>分片模式<select v-model="state.form.shard_mode"><option value="auto">自动（按 QQ 建议启动）</option><option value="manual">手动</option></select></label>
        <div v-if="state.form.shard_mode === 'manual'" class="form-grid">
          <label>片号<input v-model.number="state.form.shardIndex" type="number" min="0" max="1023"></label>
          <label>总片数<input v-model.number="state.form.shardCount" type="number" min="1" max="1024"></label>
        </div>
      </fieldset>
      <label>AppSecret 操作<select v-model="state.secretAction"><option value="keep">保留现有凭据</option><option value="replace">替换凭据</option><option value="clear">清空凭据（先禁用）</option></select></label>
      <label>新的 AppSecret<input v-model="state.secret" type="password" maxlength="512" autocomplete="new-password" placeholder="未编辑时留空"></label>
      <label><input v-model="state.confirmSecret" type="checkbox"> 明确同意替换或清空 AppSecret</label>
      <label><input v-model="state.confirmIdentity" type="checkbox"> 明确同意改绑 AppID</label>
      <details class="compat-section"><summary>迁移期 OneBot 网络兼容</summary>
        <p class="muted">新插件和新集成应使用进程内 SDK。此处只保留现有外部客户端的迁移配置。</p>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', toggleNetworkGate)">{{ state.boot?.flags?.onebot_network_enabled ? '关闭网络总闸' : '打开网络总闸' }}</button>
        <label><input v-model="state.form.onebot.enable" type="checkbox"> 此实例启用监听</label>
        <div class="form-grid"><label>绑定 IP<input v-model="state.form.onebot.host" maxlength="64"></label><label>端口<input v-model.number="state.form.onebot.port" type="number" min="1" max="65535"></label></div>
        <label><input v-model="state.form.onebot.writes" type="checkbox"> 允许网络写动作</label>
        <label><input v-model="state.confirmNetworkWrites" type="checkbox"> 确认授予网络写入权限</label>
        <label>网络 token 操作<select v-model="state.networkTokenAction"><option value="keep">保留</option><option value="replace">替换</option><option value="clear">清空</option></select></label>
        <label>新的网络 token<input v-model="state.networkToken" type="password" minlength="16" maxlength="512" autocomplete="new-password"></label>
        <label><input v-model="state.confirmNetworkToken" type="checkbox"> 明确确认替换或清空网络 token</label>
      </details>
      <div class="actions"><button :disabled="busy || !state.view">保存配置</button><button type="button" class="secondary" :disabled="busy || !state.view" @click="emit('run', reloadConnection)">重载实例</button></div>
    </form>
  </section>
</template>
