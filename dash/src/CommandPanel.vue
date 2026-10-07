<script setup>
import { computed, nextTick, ref } from "vue";
import { changed } from "./api";

const props = defineProps({ api: { type: Object, required: true }, state: { type: Object, required: true }, busy: Boolean });
const emit = defineEmits(["status", "run", "reload"]);
const query = ref("");
const help = ref(null);
const commandText = ref("");
const commandRef = ref(null);
const preview = ref(null);
const previewPages = ref(1);
const visible = computed(() => (props.state.catalog?.nodes || []).filter(node =>
  `${node.name} ${node.plugin} ${node.description}`.toLowerCase().includes(query.value.toLowerCase())));
const panel = computed(() => props.state.draft?.panels?.[props.state.scene]);
const selected = computed(() => {
  if (!panel.value || !props.state.catalog) return [];
  return panel.value.mode === "default" ? props.state.catalog.nodes.filter(node => node.system && node.enabled).map(node => node.id) : panel.value.selected;
});
const plugins = computed(() => [...new Set(visible.value.map(node => node.plugin))]);
const layers = ["home", "plugin", "group", "detail"];
const layerLabels = { home: "首页", plugin: "插件页", group: "指令组", detail: "详情页" };

function customize() {
  if (panel.value.mode === "default") panel.value.selected = [...selected.value];
  panel.value.mode = "custom";
}
function toggle(node, checked) {
  customize();
  panel.value.selected = panel.value.selected.filter(id => id !== node.id);
  if (checked) panel.value.selected.push(node.id);
}
function move(index, delta) {
  customize();
  const target = index + delta;
  if (target >= 0 && target < panel.value.selected.length) {
    [panel.value.selected[index], panel.value.selected[target]] = [panel.value.selected[target], panel.value.selected[index]];
  }
}
function readPatch() {
  props.state.draft.scene_overrides = JSON.parse(props.state.sceneOverrides);
  props.state.draft.node_overrides = JSON.parse(props.state.nodeOverrides);
  props.state.draft.extensions = { ...props.state.draft.extensions, ...props.state.extensions };
  return changed(props.state.current.draft, props.state.draft);
}
async function mutate(operation) {
  const patch = readPatch();
  if (operation === "apply" && Object.keys(patch).length) throw new Error("还有未保存的修改，请先保存草稿。");
  if (operation !== "save" && !window.confirm("确认执行本地配置操作？此操作不会直接发布到 QQ。")) return;
  await props.api.mutate(operation, patch, Number(props.state.restoreRevision));
  emit("reload");
}
async function showPreview() {
  preview.value = await props.api.preview({ patch: readPatch(), layer: props.state.layer, node: props.state.previewNode || null, page: props.state.previewPage });
  previewPages.value = preview.value.card.pages || 1;
  emit("status", `已预览${props.state.previewPage + 1}/${previewPages.value}页，不会发送 QQ 请求。`);
}
async function changePage(delta) {
  const next = props.state.previewPage + delta;
  if (next < 0 || next >= previewPages.value) return;
  props.state.previewPage = next;
  await showPreview();
}
async function selectText() {
  await nextTick();
  commandRef.value?.focus();
  commandRef.value?.select();
  emit("status", "命令文本已选中，没有执行命令。");
}
function inputNumber(key, value) { props.state.draft.layout[key]["page_size"] = Number(value); }
</script>

<template>
  <section v-if="state.current && state.draft" class="panel-grid command-grid">
    <article class="card command-catalog">
      <h2>真实指令目录</h2>
      <input v-model="query" placeholder="搜索名称、插件或说明">
      <div class="catalog-scroll">
        <details v-for="plugin in plugins" :key="plugin" open>
          <summary>{{ plugin }}</summary>
          <div v-for="node in visible.filter(item => item.plugin === plugin)" :key="node.id" class="command-row">
            <input type="checkbox" :checked="node.menu_entry || selected.includes(node.id)" :disabled="node.menu_entry || !node.enabled" @change="toggle(node, $event.target.checked)">
            <button type="button" class="link-button" @click="help = node; commandText = node.usage">{{ node.usage }}{{ node.enabled ? '' : '（不可用）' }}</button>
          </div>
        </details>
      </div>
    </article>

    <article class="card">
      <h2>本地菜单配置</h2>
      <label>菜单标题<input v-model="state.draft.title" maxlength="80"></label>
      <label>选择方式<select v-model="panel.mode"><option value="default">全部适用系统指令 + 菜单</option><option value="custom">自选指令</option></select></label>
      <ol class="selected-list">
        <li v-for="(id, index) in selected" :key="id">
          <span>{{ state.catalog.nodes.find(node => node.id === id)?.command || `缺失：${id}` }}</span>
          <span><button type="button" class="small-button" @click="move(index, -1)">上移</button><button type="button" class="small-button" @click="move(index, 1)">下移</button></span>
        </li>
      </ol>
      <label><input v-model="state.confirmBindings" type="checkbox"> 确认当前所选 handler 来源及参数</label>

      <h3>四层布局</h3>
      <div class="layout-grid">
        <fieldset v-for="layer in layers" :key="layer"><legend>{{ layerLabels[layer] }}</legend>
          <label>每页<input v-model.number="state.draft.layout[layer].page_size" type="number" min="1" max="21"></label>
          <label>每行<input v-model.number="state.draft.layout[layer].columns" type="number" min="1" max="5"></label>
          <label>样式<select v-model="state.draft.layout[layer].style"><option value="plain">普通</option><option value="heading">标题</option><option value="quote">引用</option></select></label>
          <label><input v-model="state.draft.layout[layer].show_description" type="checkbox"> 显示摘要</label>
        </fieldset>
      </div>

      <details><summary>场景和节点覆盖</summary>
        <label>scene_overrides<textarea v-model="state.sceneOverrides" rows="5"></textarea></label>
        <label>node_overrides<textarea v-model="state.nodeOverrides" rows="5"></textarea></label>
      </details>
      <details><summary>扩展与媒体策略</summary>
        <div class="form-grid">
          <label>本地媒体上限<input v-model.number="state.extensions.media_max_bytes" type="number" min="1" max="200000000"></label>
          <label>流式回退<select v-model="state.extensions.stream_fallback"><option value="aggregate">有界聚合</option><option value="reject">明确拒绝</option></select></label>
          <label>流式字符上限<input v-model.number="state.extensions.stream_max_chars" type="number" min="1" max="4096"></label>
          <label>流式秒数<input v-model.number="state.extensions.stream_timeout" type="number" min="1" max="180"></label>
          <label>票据有效秒数<input v-model.number="state.extensions.ticket_ttl" type="number" min="30" max="300"></label>
        </div>
        <label><input v-model="state.extensions.typing_enabled" type="checkbox"> 允许 C2C 输入中状态</label>
        <label><input v-model="state.extensions.keyboard_enabled" type="checkbox"> 允许回调键盘</label>
        <label><input v-model="state.extensions.keyboard_execute" type="checkbox"> 允许无参数指令二次确认执行</label>
        <label><input v-model="state.extensions.management_writes" type="checkbox"> 允许具名管理和撤回写入</label>
      </details>

      <div class="actions">
        <button :disabled="busy" @click="emit('run', () => mutate('save'))">保存草稿</button>
        <button class="secondary" :disabled="busy" @click="emit('run', () => mutate('apply'))">应用到本地</button>
        <button class="secondary" :disabled="busy" @click="emit('run', () => mutate('discard'))">撤销草稿</button>
        <button class="secondary" :disabled="busy" @click="emit('run', () => mutate('defaults'))">恢复默认</button>
      </div>
      <label>保留版本<select v-model="state.restoreRevision"><option v-for="version in state.current.versions" :key="version" :value="version">版本 {{ version }}</option></select></label>
      <button class="secondary" :disabled="busy" @click="emit('run', () => mutate('restore'))">恢复为草稿</button>
    </article>

    <article class="card preview-card">
      <h2>本地预览</h2>
      <label>预览层级<select v-model="state.layer" @change="state.previewPage = 0"><option v-for="layer in layers" :key="layer" :value="layer">{{ layerLabels[layer] }}</option></select></label>
      <label>预览节点<select v-model="state.previewNode" @change="state.previewPage = 0"><option value="">根节点</option><option v-for="node in state.catalog.nodes" :key="node.id" :value="node.id">{{ node.name }}</option></select></label>
      <button :disabled="busy" @click="emit('run', showPreview)">校验 / 预览</button>
      <div v-if="preview" class="preview-output">
        <strong>{{ state.draft.title }}</strong>
        <p v-for="item in preview.card.items" :key="item.usage">{{ item.usage }}</p>
        <small>第 {{ state.previewPage + 1 }} / {{ previewPages }} 页 · 本地预览</small>
      </div>
      <div class="actions"><button class="secondary" :disabled="busy || !preview || state.previewPage === 0" @click="emit('run', () => changePage(-1))">上一页</button><button class="secondary" :disabled="busy || !preview || state.previewPage + 1 >= previewPages" @click="emit('run', () => changePage(1))">下一页</button></div>
      <div v-if="help" class="help-box">
        <strong>{{ help.name }}</strong><p>{{ help.description }}</p>
        <textarea ref="commandRef" v-model="commandText" readonly rows="3"></textarea>
        <button class="secondary" type="button" @click="selectText">选择命令文本</button>
      </div>
    </article>
  </section>
  <section v-else class="empty-state"><h2>还没有已加载的目标</h2><p>先在连接页保存一个 QQ V2 目标，再编辑本地菜单。</p></section>
</template>
