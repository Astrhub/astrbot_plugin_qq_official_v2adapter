<script setup>
import { computed, ref } from "vue";

const props = defineProps({ api: { type: Object, required: true }, state: { type: Object, required: true }, busy: Boolean });
const emit = defineEmits(["status", "run"]);
const output = ref(null);
const plan = ref(null);
const retained = ref(null);
const picked = ref([]);
const remoteState = computed(() => props.state.current?.remote_state?.[props.state.scene] || { state: "not_managed" });
function options() {
  return {
    target_type: props.state.panelTargetType,
    targets: props.state.panelTargets.split(",").map(item => item.trim()).filter(Boolean),
    menu_only: props.state.menuOnly,
  };
}
async function toggleGate() {
  const enabled = !props.state.boot.flags?.remote_menu_sync;
  if (!window.confirm(enabled ? "打开远端同步总闸？后续稳定目录变化会按确认范围同步。" : "关闭远端同步总闸？已有面板保留，在途结果仍需核对。")) return;
  await props.api.setFlag("remote_menu_sync", enabled);
  emit("status", enabled ? "远端同步总闸已打开。" : "远端同步总闸已关闭。");
}
async function makePlan() {
  plan.value = await props.api.planPanels(options());
  output.value = plan.value;
  emit("status", `已生成${plan.value.issues?.length || 0}项面板检查结果。`);
}
async function enable() {
  if (!props.state.boot.flags?.remote_menu_sync) throw new Error("请先打开远端同步总闸。");
  if (!plan.value) throw new Error("请先预览发布范围。");
  if (plan.value.issues?.length) throw new Error("发布预览仍有问题，请先处理。");
  if (!window.confirm(`确认托管 ${props.state.scene} / ${options().target_type} 并向 QQ 发布？`)) return;
  output.value = await props.api.enablePanels({ ...options(), plan_fingerprint: plan.value.fingerprint });
  plan.value = null;
  emit("status", "已提交面板托管。");
}
async function sync() {
  if (!window.confirm("确认核对并同步当前已托管面板？未知结果不会自动重放。")) return;
  output.value = await props.api.syncPanels();
  emit("status", "已完成面板核对请求。");
}
async function disable() {
  if (!window.confirm("停止当前范围托管？远端已有面板不会删除。")) return;
  output.value = await props.api.disablePanels();
  emit("status", "已停止当前范围托管。");
}
async function loadRetained() {
  retained.value = await props.api.retained();
  picked.value = [];
}
async function discard() {
  if (!retained.value) throw new Error("请先读取保留事件。");
  const entries = retained.value.entries.filter(entry => picked.value.includes(entry.receipt)).map(({ receipt, version, confirmation }) => ({ receipt, version, confirmation }));
  if (!entries.length) throw new Error("请先选择要丢弃的事件。");
  if (!window.confirm(`永久丢弃 ${entries.length} 条保留事件？`)) return;
  const result = await props.api.discardRetained({
    platform_id: retained.value.platform_id,
    fingerprint: retained.value.fingerprint,
    expires: retained.value.expires,
    csrf: props.state.boot.csrf,
    entries,
    confirm: true,
  });
  await loadRetained();
  emit("status", `已明确丢弃 ${result.discarded} 条保留事件。`);
}
</script>

<template>
  <section class="panel-grid remote-grid">
    <article class="card">
      <h2>QQ 指令面板托管</h2>
      <p class="muted">页面只提交托管意图。ControlAPI 会交给 PanelService，使用机器人锁、计划指纹、pending 和 reconcile 管理 QQ 写入。</p>
      <div class="state-banner"><strong>当前状态：{{ remoteState.state || 'not_managed' }}</strong><span v-if="remoteState.panel_id">panel_id：{{ remoteState.panel_id }}</span><span v-if="remoteState.pending">待核对：{{ remoteState.pending.kind }}</span></div>
      <button class="secondary" :disabled="busy" @click="emit('run', toggleGate)">{{ state.boot?.flags?.remote_menu_sync ? '关闭远端同步总闸' : '打开远端同步总闸' }}</button>
      <label>范围<select v-model="state.panelTargetType"><option value="all">该场景全部目标</option><option value="specific">指定已观察目标</option></select></label>
      <label>目标 OpenID<input v-model="state.panelTargets" maxlength="10400" placeholder="多个目标用逗号分隔"></label>
      <label><input v-model="state.menuOnly" type="checkbox"> 只发布菜单入口，其余指令进入动态帮助</label>
      <div class="actions"><button :disabled="busy" @click="emit('run', makePlan)">预览发布范围</button><button :disabled="busy" @click="emit('run', enable)">确认发布并托管</button><button class="secondary" :disabled="busy" @click="emit('run', sync)">核对 / 同步</button><button class="secondary" :disabled="busy" @click="emit('run', disable)">停止托管</button></div>
      <pre class="status-box">{{ JSON.stringify(output || remoteState, null, 2) }}</pre>
    </article>
    <article class="card">
      <h2>保留事件恢复</h2>
      <p class="muted">只显示元数据。丢弃操作不可恢复，确认绑定当前实例和 60 秒快照。</p>
      <button class="secondary" :disabled="busy" @click="emit('run', loadRetained)">读取保留事件</button>
      <div v-if="retained" class="retained-list">
        <label v-for="entry in retained.entries" :key="entry.receipt"><input v-model="picked" :value="entry.receipt" type="checkbox"> #{{ entry.receipt }} · {{ entry.event_type }} · {{ entry.state }} · {{ entry.reason }}</label>
      </div>
      <button class="danger" :disabled="busy || !retained" @click="emit('run', discard)">明确丢弃所选事件</button>
    </article>
  </section>
</template>
