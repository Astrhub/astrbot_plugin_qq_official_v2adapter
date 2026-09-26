<script setup>
import { computed, onUnmounted, ref } from "vue";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
});
const emit = defineEmits(["status", "run"]);
const canvas = ref(null);
let timer = 0;
const active = computed(() => ["creating", "pending", "ready_to_commit"].includes(props.state.scan?.state));
const ready = computed(() => props.state.scan?.state === "ready_to_commit");

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
function show(value) {
  props.state.scan = value;
  emit("status", value.hint || "扫码状态更新了。");
  requestAnimationFrame(() => paint(value.qr_matrix));
}
function later() {
  window.clearTimeout(timer);
  if (!active.value) return;
  const delay = props.state.scan.state === "creating" || !props.state.scan.qr_matrix ? 400 : 5000;
  timer = window.setTimeout(() => emit("run", async () => { await props.api.refreshScan(); show(props.state.scan); later(); }), delay);
}
async function start() {
  emit("status", "正在向 QQ 要二维码。");
  await props.api.startScan();
  show(props.state.scan);
  later();
}
async function cancel() {
  window.clearTimeout(timer);
  await props.api.cancelScan();
  show(props.state.scan);
}
async function saveScan() {
  await props.api.commitScan();
  show(props.state.scan);
}
function fields() {
  const form = props.state.form;
  if (!form) throw new Error("请先读取连接。");
  return { ...form, shard: JSON.parse(form.shardText), intents: Number(form.intents) };
}
function changed(oldValue, next) {
  const patch = {};
  for (const key of new Set([...Object.keys(oldValue), ...Object.keys(next)])) {
    if (JSON.stringify(oldValue[key]) !== JSON.stringify(next[key])) patch[key] = next[key];
  }
  return patch;
}
async function save() {
  const next = fields();
  const secret = props.state.secret;
  if (secret && props.state.secretAction !== "replace") throw new Error("写了新的 AppSecret，就要选择更换，或者把输入框清空。");
  if (props.state.networkToken && props.state.networkTokenAction !== "replace") throw new Error("写了新的网络口令，就要选择更换，或者把输入框清空。");
  if (!window.confirm("保存这台机器人的连接。保存后不会自动上线。")) return;
  await props.api.saveConnection({
    patch: changed(props.state.view.fields, next), secret_action: props.state.secretAction, secret,
    confirm: true, confirm_secret: props.state.confirmSecret, confirm_identity: props.state.confirmIdentity,
    network_token_action: props.state.networkTokenAction, network_token: props.state.networkToken,
    confirm_network_token: props.state.confirmNetworkToken, confirm_network_writes: props.state.confirmNetworkWrites,
  });
  props.state.secret = "";
  props.state.networkToken = "";
  emit("status", "连接已保存，机器人还没上线。");
}
function leave() {
  window.clearTimeout(timer);
  if (active.value) props.api.connectionPayload && props.state.bridge.apiPost("onboarding/cancel", { csrf: props.state.boot.csrf, platform_id: props.state.scan.platform_id, ticket: props.state.scan.ticket }).catch(() => {});
}
onUnmounted(leave);
window.addEventListener("pagehide", leave);
</script>

<template>
  <section class="grid gap-6 lg:grid-cols-12">
    <div class="rounded-3xl border border-[#DADCE0] bg-white p-6 lg:col-span-5">
      <h2 class="text-base font-semibold">手机 QQ 扫码</h2>
      <p class="mt-1 text-sm text-[#5F6368]">不用先填 AppSecret。扫完后点保存，机器人仍保持关闭。</p>
      <div class="mx-auto my-4 grid aspect-square w-full max-w-[280px] place-items-center rounded-2xl border border-[#E0E2EC] bg-[#F8F9FA]">
        <canvas v-show="state.scan?.qr_matrix" ref="canvas" class="h-56 w-56 [image-rendering:pixelated]"></canvas>
        <p v-if="!state.scan?.qr_matrix" class="px-6 text-center text-sm text-[#5F6368]">点下面生成二维码</p>
      </div>
      <div class="flex gap-2">
        <button class="flex-1 rounded-full bg-[#1A73E8] py-2.5 text-sm font-medium text-white disabled:bg-[#BDC1C6]" :disabled="busy || !state.view" @click="emit('run', start)">生成二维码</button>
        <button v-if="active" class="rounded-full px-4 text-sm text-[#5F6368] hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', cancel)">取消</button>
        <button v-if="ready" class="rounded-full bg-[#137333] px-4 text-sm font-medium text-white" :disabled="busy" @click="emit('run', saveScan)">保存扫码结果</button>
      </div>
    </div>
    <form class="grid gap-3 rounded-3xl border border-[#DADCE0] bg-white p-6 lg:col-span-7" @submit.prevent="emit('run', save)">
      <h2 class="text-base font-semibold">手动填写</h2>
      <div class="grid gap-3 md:grid-cols-2">
        <label class="text-xs text-[#5F6368]">AppID <input v-model="state.form.appid" maxlength="128" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
        <label class="text-xs text-[#5F6368]">用在哪 <select v-model="state.form.environment" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="production">正式环境</option><option value="sandbox">沙箱，暂时不能联网</option></select></label>
        <label class="text-xs text-[#5F6368]">怎么收消息 <select v-model="state.form.transport" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="websocket">长连接</option><option value="webhook">QQ 回调到本机</option></select></label>
        <label class="text-xs text-[#5F6368]">接收哪些事件 <input v-model="state.form.intents" type="number" min="0" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
        <label class="text-xs text-[#5F6368]">分片，写成 [序号, 总数] <input v-model="state.form.shardText" maxlength="20" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
        <label class="mt-6 flex items-center gap-2 text-sm"><input v-model="state.form.enable" type="checkbox">保存后允许上线</label>
      </div>
      <label class="text-xs text-[#5F6368]">AppSecret 怎么办 <select v-model="state.secretAction" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="keep">保持不变</option><option value="replace">换成新的</option><option value="clear">清掉，要先关闭机器人</option></select></label>
      <label class="text-xs text-[#5F6368]">新的 AppSecret <input v-model="state.secret" type="password" maxlength="512" autocomplete="new-password" placeholder="不改就留空，页面不会显示旧值" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
      <label class="flex items-center gap-2 text-sm"><input v-model="state.confirmSecret" type="checkbox">我确认要更换或清掉 AppSecret</label>
      <label class="flex items-center gap-2 text-sm"><input v-model="state.confirmIdentity" type="checkbox">我确认要改 AppID 或环境</label>
      <details class="rounded-2xl bg-[#F8F9FA] p-4 text-sm">
        <summary>给外部程序接这个机器人</summary>
        <p class="my-2 text-[#5F6368]">默认关闭。这不是完整 OneBot，也不能拿数字 QQ 号当身份。</p>
        <button type="button" class="rounded-full bg-white px-4 py-2" :disabled="busy" @click="emit('run', async () => { await api.setFlag('onebot_network_enabled', !state.boot.flags.onebot_network_enabled); emit('status', state.boot.flags.onebot_network_enabled ? '外部接口已打开，还要重载才生效。' : '外部接口已关闭。'); })">{{ state.boot?.flags?.onebot_network_enabled ? "关闭外部接口" : "打开外部接口" }}</button>
        <label class="mt-3 flex items-center gap-2"><input v-model="state.form.onebot.enable" type="checkbox">这台机器人也打开</label>
        <div class="mt-3 grid gap-3 md:grid-cols-2">
          <label class="text-xs text-[#5F6368]">只监听这个地址 <input v-model="state.form.onebot.host" maxlength="64" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
          <label class="text-xs text-[#5F6368]">端口 <input v-model.number="state.form.onebot.port" type="number" min="1" max="65535" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
        </div>
        <label class="mt-3 flex items-center gap-2"><input v-model="state.form.onebot.writes" type="checkbox">允许外部程序发消息和改设置</label>
        <label class="mt-2 flex items-center gap-2"><input v-model="state.confirmNetworkWrites" type="checkbox">我确认给这个口令写入权限</label>
        <label class="mt-3 block text-xs text-[#5F6368]">网络口令怎么办 <select v-model="state.networkTokenAction" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="keep">保持不变</option><option value="replace">换成新的</option><option value="clear">清掉</option></select></label>
        <label class="mt-3 block text-xs text-[#5F6368]">新的网络口令 <input v-model="state.networkToken" type="password" minlength="16" maxlength="512" autocomplete="new-password" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
        <label class="mt-2 flex items-center gap-2"><input v-model="state.confirmNetworkToken" type="checkbox">我确认要更换或清掉网络口令</label>
      </details>
      <div class="flex gap-2">
        <button class="rounded-full bg-[#1A73E8] px-5 py-2.5 text-sm font-medium text-white disabled:bg-[#BDC1C6]" :disabled="busy || !state.view">保存，先不上线</button>
        <button type="button" class="rounded-full px-5 py-2.5 text-sm hover:bg-[#F1F3F4]" :disabled="busy || !state.view" @click="emit('run', async () => { if (window.confirm('按已保存的配置重新连接。机器人开启时会开始收消息。')) { await api.reloadConnection(); emit('status', '已重新连接。'); } })">重新连接</button>
      </div>
    </form>
  </section>
</template>
