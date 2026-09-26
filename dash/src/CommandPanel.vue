<script setup>
import { computed, ref } from "vue";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
});
const emit = defineEmits(["status", "run", "reload"]);
const query = ref("");
const help = ref(null);
const commandText = ref("");
const preview = ref(null);
const previewPage = ref(0);
const selected = computed(() => {
  const panel = props.state.draft?.panels[props.state.scene];
  if (!panel || !props.state.catalog) return [];
  return panel.mode === "default" ? props.state.catalog.nodes.filter(node => node.system && node.enabled).map(node => node.id) : panel.selected;
});
const visible = computed(() => (props.state.catalog?.nodes || []).filter(node => `${node.name} ${node.plugin} ${node.description}`.toLowerCase().includes(query.value.toLowerCase())));
function panel() { return props.state.draft.panels[props.state.scene]; }
function customize() {
  if (panel().mode === "default") panel().selected = [...selected.value];
  panel().mode = "custom";
}
function toggle(node, checked) {
  customize();
  panel().selected = panel().selected.filter(id => id !== node.id);
  if (checked) panel().selected.push(node.id);
}
function move(index, delta) {
  customize();
  const list = panel().selected;
  const target = index + delta;
  if (target >= 0 && target < list.length) [list[index], list[target]] = [list[target], list[index]];
}
function readPatch() {
  const draft = props.state.draft;
  draft.scene_overrides = JSON.parse(props.state.sceneOverrides);
  draft.node_overrides = JSON.parse(props.state.nodeOverrides);
  draft.extensions = { ...draft.extensions, ...props.state.extensions, media_roots: props.state.mediaRoots.split("\n").map(item => item.trim()).filter(Boolean) };
  return changed(props.state.current.draft, draft);
}
function changed(oldValue, next) {
  const patch = {};
  for (const key of new Set([...Object.keys(oldValue), ...Object.keys(next)])) {
    if (JSON.stringify(oldValue[key]) !== JSON.stringify(next[key])) patch[key] = next[key];
  }
  return patch;
}
async function mutate(operation) {
  const patch = readPatch();
  if (operation === "apply" && Object.keys(patch).length) throw new Error("还有没保存的修改。先保存，再应用到本地。");
  if (operation !== "save" && !window.confirm("这个操作只改本地菜单，不会发给 QQ。")) return;
  await props.api.mutate(operation, patch, Number(props.state.restoreRevision));
  emit("reload");
}
async function showPreview() {
  preview.value = await props.api.preview({ patch: readPatch(), layer: props.state.layer, node: props.state.previewNode || null, page: previewPage.value });
  emit("status", `看了第 ${previewPage.value + 1} 页，没有真的发出去。`);
}
</script>

<template>
  <section v-if="state.draft" class="grid gap-6 xl:grid-cols-3">
    <div class="rounded-3xl border border-[#DADCE0] bg-white p-5">
      <h2 class="font-semibold">机器人会用到的指令</h2>
      <input v-model="query" placeholder="搜名字、插件或说明" class="mt-3 w-full rounded-xl border px-3 py-2 text-sm">
      <div class="mt-3 max-h-[70vh] space-y-3 overflow-auto">
        <details v-for="plugin in [...new Set(visible.map(node => node.plugin))]" :key="plugin" open>
          <summary>{{ plugin }}</summary>
          <label v-for="node in visible.filter(item => item.plugin === plugin)" :key="node.id" class="mt-2 flex items-start gap-2 text-sm">
            <input type="checkbox" :checked="node.menu_entry || selected.includes(node.id)" :disabled="node.menu_entry || !node.enabled" @change="toggle(node, $event.target.checked)">
            <button type="button" class="text-left text-[#1A73E8]" @click="help = node; commandText = node.usage">{{ node.usage }}{{ node.enabled ? "" : "（现在不能用）" }}</button>
          </label>
        </details>
      </div>
    </div>
    <div class="rounded-3xl border border-[#DADCE0] bg-white p-5">
      <h2 class="font-semibold">菜单里显示什么</h2>
      <label class="mt-3 block text-xs text-[#5F6368]">菜单标题 <input v-model="state.draft.title" maxlength="80" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"></label>
      <label class="mt-3 block text-xs text-[#5F6368]">选择方式 <select v-model="panel().mode" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="default">用系统指令</option><option value="custom">自己选</option></select></label>
      <ol class="mt-3 space-y-2 text-sm">
        <li v-for="(id, index) in selected" :key="id" class="flex items-center justify-between gap-2">
          <span>{{ state.catalog.nodes.find(node => node.id === id)?.command || `找不到：${id}` }}</span>
          <span class="flex gap-1"><button type="button" @click="move(index, -1)">上移</button><button type="button" @click="move(index, 1)">下移</button></span>
        </li>
      </ol>
      <label class="mt-4 flex items-center gap-2 text-sm"><input v-model="state.confirmBindings" type="checkbox">我确认这些指令的来源和参数</label>
      <div class="mt-4 flex flex-wrap gap-2">
        <button class="rounded-full bg-[#1A73E8] px-4 py-2 text-sm text-white" :disabled="busy" @click="emit('run', () => mutate('save'))">保存</button>
        <button class="rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', () => mutate('apply'))">应用到本地</button>
        <button class="rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', () => mutate('discard'))">放弃修改</button>
        <button class="rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', () => mutate('defaults'))">恢复默认</button>
      </div>
      <label class="mt-4 block text-xs text-[#5F6368]">以前的版本 <select v-model="state.restoreRevision" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option v-for="version in state.current.versions" :key="version" :value="version">版本 {{ version }}</option></select></label>
      <button class="mt-2 rounded-full px-4 py-2 text-sm hover:bg-[#F1F3F4]" :disabled="busy" @click="emit('run', () => mutate('restore'))">取回这个版本</button>
    </div>
    <div class="rounded-3xl border border-[#DADCE0] bg-white p-5">
      <h2 class="font-semibold">先看看效果</h2>
      <label class="mt-3 block text-xs text-[#5F6368]">看哪一层 <select v-model="state.layer" class="mt-1 w-full rounded-xl border px-3 py-2 text-sm"><option value="home">首页</option><option value="plugin">插件</option><option value="group">指令分组</option><option value="detail">一条指令</option></select></label>
      <button class="mt-3 rounded-full bg-[#1A73E8] px-4 py-2 text-sm text-white" :disabled="busy" @click="emit('run', showPreview)">看看</button>
      <div v-if="preview" class="mt-4 rounded-2xl bg-[#F8F9FA] p-4 text-sm">
        <p class="font-semibold">{{ state.draft.title }}</p>
        <p v-for="item in preview.card.items" :key="item.usage">{{ item.usage }}</p>
        <p class="text-[#5F6368]">第 {{ previewPage + 1 }} / {{ preview.card.pages }} 页</p>
      </div>
      <div v-if="help" class="mt-4 text-sm">
        <p class="font-semibold">{{ help.name }}</p>
        <p>{{ help.description }}</p>
        <textarea v-model="commandText" readonly rows="3" class="mt-2 w-full rounded-xl border px-3 py-2"></textarea>
        <button type="button" class="mt-2 rounded-full px-4 py-2 hover:bg-[#F1F3F4]" @click="emit('status', '文字已放在框里，还没有执行。')">选中文字</button>
      </div>
    </div>
  </section>
</template>
