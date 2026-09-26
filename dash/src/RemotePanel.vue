<script setup>
import { ref } from "vue";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
});
const emit = defineEmits(["status", "run"]);
const output = ref("还没有查看 QQ 上的指令面板。");
const plan = ref(null);
const retained = ref(null);
const picked = ref([]);
function options() {
  return { target_type: props.state.panelTargetType, targets: props.state.panelTargets.split(",").map(item => item.trim()).filter(Boolean), menu_only: props.state.menuOnly };
}
async function makePlan() {
  plan.value = await props.api.planPanels(options());
  output.value = JSON.stringify(plan.value, null, 2);
}
async function enable() {
  if (!props.state.boot.flags?.remote_menu_sync) throw new Error("先打开“自动同步到 QQ”。");
  if (!plan.value) throw new Error("先看一下会改哪些面板。");
  if (plan.value.issues.length) throw new Error("还有问题，先处理后再发布。");
  if (!window.confirm("把当前菜单发布到 QQ。之后指令有变化，也会按这个范围更新。")) return;
  output.value = JSON.stringify(await props.api.enablePanels({ ...options(), plan_fingerprint: plan.value.fingerprint }), null, 2);
  plan.value = null;
}
async function loadRetained() {
  retained.value = await props.api.retained();
  picked.value = [];
}
async function discard() {
  const entries = retained.value.entries.filter(entry => picked.value.includes(entry.receipt)).map(({ receipt, version, confirmation }) => ({ receipt, version, confirmation }));
  if (!entries.length) throw new Error("先勾选要丢掉的事件。");
  if (!window.confirm(`丢掉 ${entries.length} 条没处理的事件？丢掉后不会再处理。`)) return;
  const result = await props.api.discardRetained({ platform_id: retained.value.platform_id, fingerprint: retained.value.fingerprint, expires: retained.value.expires, csrf: props.state.boot.csrf, entries, confirm: true });
  await loadRetained();
  emit("status", `已丢掉 ${result.discarded} 条。`);
}
</script>

<template>
  <section class="grid gap-6 lg:grid-cols-2">
    <div class="rounded-3xl border border-[#DADCE0] bg-white p-5">
      <h2 class="font-semibold">QQ 里的指令面板</h2>
      <p class="mt-1 text-sm text-[#5F6368]">这里才会改 QQ。停止同步不会把 QQ 里已经有的面板删掉。</p>
      <button class="mt-3 rounded-full bg-white px-4 py-2 text-sm ring-1 ring-[#DADCE0]" :disabled="busy" @click="emit('run', async () => { await api.setFlag('remote_menu_sync', !state.boot.flags?.remote_menu_sync); emit('status', state.boot.flags.remote_menu_sync ? '之后会自动同步。' : '已停止自动同步。'); })">{{ state.boot?.flags?.remote_menu_sync ? "停止自动同步" : "自动同步到 QQ" }}</button>
      <label class="mt-3 block text-xs text-[#5F6368]">用在哪些聊天 <select v-model="state.panelTargetType" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="all">这个场景里的全部聊天</option><option value="specific">只改下面填的聊天</option></select></label>
      <label class="mt-3 block text-xs text-[#5F6368]">聊天编号，用逗号分开，最多 20 个 <input v-model="state.panelTargets" maxlength="10400" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
      <label class="mt-3 flex items-center gap-2 text-sm"><input v-model="state.menuOnly" type="checkbox">其他指令收进帮助，只发布菜单入口</label>
      <div class="mt-4 flex flex-wrap gap-2">
        <button class="rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', makePlan)">先看看会改什么</button>
        <button class="rounded-full bg-[#1A73E8] px-4 py-2 text-sm text-white" :disabled="busy" @click="emit('run', enable)">发布到 QQ</button>
        <button class="rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', async () => { output = JSON.stringify(await api.syncPanels(), null, 2); })">再同步一次</button>
        <button class="rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', async () => { if (window.confirm('停止继续改 QQ 里的这些面板？已有面板还留着。')) output = JSON.stringify(await api.disablePanels(), null, 2); })">停止同步</button>
      </div>
      <pre class="mt-4 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{{ output }}</pre>
    </div>
    <div class="rounded-3xl border border-[#DADCE0] bg-white p-5">
      <h2 class="font-semibold">没处理完的事件</h2>
      <p class="mt-1 text-sm text-[#5F6368]">这里只显示事件编号和原因，不显示消息内容。丢掉以后找不回来。</p>
      <button class="mt-3 rounded-full bg-[#1A73E8] px-4 py-2 text-sm text-white" :disabled="busy" @click="emit('run', loadRetained)">看看有哪些</button>
      <div class="mt-3 space-y-2 text-sm">
        <label v-for="entry in retained?.entries || []" :key="entry.receipt" class="flex gap-2">
          <input v-model="picked" :value="entry.receipt" type="checkbox">
          <span>#{{ entry.receipt }} · {{ entry.event_type }} · {{ entry.reason }}</span>
        </label>
      </div>
      <button class="mt-4 rounded-full px-4 py-2 text-sm text-[#D93025] ring-1 ring-[#F5C2C0]" :disabled="busy" @click="emit('run', discard)">丢掉勾选的事件</button>
    </div>
  </section>
</template>
