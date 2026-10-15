<script setup>
import { computed, nextTick, onMounted, ref } from "vue";
import AbSelect from "./AbSelect.vue";
import { changed } from "./api.js";
import { t } from "./i18n.js";

const props = defineProps({
  api: { type: Object, required: true },
  state: { type: Object, required: true },
  busy: Boolean,
  confirm: { type: Function, required: true },
});
const emit = defineEmits(["status", "run", "reload"]);
const query = ref("");
const help = ref(null);
const commandText = ref("");
const commandRef = ref(null);
const preview = ref(null);
const previewPages = ref(1);
const newSceneKey = ref("group");
const newSceneLayer = ref("home");
const newNodeId = ref("");

const layers = ["home", "plugin", "group", "detail"];
const scenes = ["group", "c2c", "channel", "dm"];

const allNodes = computed(() => props.state.catalog?.nodes || []);
const allPlugins = computed(() => [...new Set(allNodes.value.map(node => node.plugin))]);
const visible = computed(() => allNodes.value.filter(node =>
  `${node.name} ${node.plugin} ${node.description}`.toLowerCase().includes(query.value.toLowerCase())));
const panel = computed(() => props.state.draft?.panels?.[props.state.scene]);
const selected = computed(() => {
  if (!panel.value || !props.state.catalog) return [];
  return panel.value.mode === "default" ? allNodes.value.filter(node => node.system && node.enabled).map(node => node.id) : panel.value.selected;
});
const plugins = computed(() => [...new Set(visible.value.map(node => node.plugin))]);

const modeOptions = computed(() => [
  { value: "default", label: t("cmd.modeDefault") },
  { value: "custom", label: t("cmd.modeCustom") },
]);
const styleOptions = computed(() => [
  { value: "plain", label: t("cmd.stylePlain") },
  { value: "heading", label: t("cmd.styleHeading") },
  { value: "quote", label: t("cmd.styleQuote") },
]);
const streamFallbackOptions = computed(() => [
  { value: "aggregate", label: t("cmd.streamAggregate") },
  { value: "reject", label: t("cmd.streamReject") },
]);
const layerOptions = computed(() => layers.map(l => ({ value: l, label: t(`cmd.layer.${l}`) })));
const sceneSelectOptions = computed(() => scenes.map(s => ({ value: s, label: t(`scene.${s}`) })));
const nodeOptions = computed(() => {
  if (props.state.layer === "home") {
    return [{ value: "", label: t("cmd.rootNode") }];
  }
  if (props.state.layer === "plugin") {
    return allPlugins.value.map(p => ({ value: p, label: p }));
  }
  if (props.state.layer === "group") {
    const parentIds = new Set(allNodes.value.map(n => n.parent).filter(Boolean));
    const groups = allNodes.value.filter(n => n.kind === "group" || parentIds.has(n.id));
    const list = groups.length ? groups : allNodes.value;
    return list.map(n => ({ value: n.id, label: n.name, sublabel: n.plugin }));
  }
  return allNodes.value.map(n => ({ value: n.id, label: n.name, sublabel: n.plugin }));
});
const overrideNodeOptions = computed(() =>
  (props.state.catalog?.nodes || []).map(n => ({ value: n.id, label: `${n.name} (${n.plugin})` }))
);
const versionOptions = computed(() =>
  (props.state.current?.versions || []).map(v => ({ value: v, label: t("cmd.versionItem", { v }) }))
);

const sceneOverrideRows = computed(() => {
  const rows = [];
  const obj = props.state.draft?.scene_overrides || {};
  for (const [sc, lMap] of Object.entries(obj)) {
    for (const [ly, cfg] of Object.entries(lMap || {})) {
      rows.push({ scene: sc, layer: ly, cfg });
    }
  }
  return rows;
});

const nodeOverrideRows = computed(() => {
  const obj = props.state.draft?.node_overrides || {};
  return Object.entries(obj).map(([nodeId, cfg]) => ({
    nodeId,
    label: props.state.catalog?.nodes?.find(n => n.id === nodeId)?.name || nodeId,
    cfg,
  }));
});

function addSceneOverride() {
  const sc = newSceneKey.value;
  const ly = newSceneLayer.value;
  if (!props.state.draft.scene_overrides[sc]) props.state.draft.scene_overrides[sc] = {};
  if (!props.state.draft.scene_overrides[sc][ly]) {
    props.state.draft.scene_overrides[sc][ly] = { ...props.state.draft.layout[ly] };
  }
}
function removeSceneOverride(sc, ly) {
  if (!props.state.draft.scene_overrides[sc]) return;
  delete props.state.draft.scene_overrides[sc][ly];
  if (!Object.keys(props.state.draft.scene_overrides[sc]).length) {
    delete props.state.draft.scene_overrides[sc];
  }
}
function addNodeOverride() {
  const id = (newNodeId.value || overrideNodeOptions.value[0]?.value || "").trim();
  if (!id) return;
  if (!props.state.draft.node_overrides[id]) {
    props.state.draft.node_overrides[id] = { ...props.state.draft.layout.home };
  }
}
function removeNodeOverride(id) {
  delete props.state.draft.node_overrides[id];
}

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
  props.state.draft.extensions = { ...props.state.draft.extensions, ...props.state.extensions };
  return changed(props.state.current.draft, props.state.draft);
}
async function mutate(operation) {
  const patch = readPatch();
  if (operation === "apply" && Object.keys(patch).length) throw new Error(t("cmd.errSaveBeforeApply"));
  if (operation !== "save" && !(await props.confirm(t("cmd.confirmMutate")))) return;
  await props.api.mutate(operation, patch, Number(props.state.restoreRevision));
  emit("reload");
}
function onLayerChange(nextLayer) {
  props.state.layer = nextLayer;
  props.state.previewPage = 0;
  const opts = nodeOptions.value;
  props.state.previewNode = opts[0]?.value ?? "";
  emit("run", showPreview);
}
function onNodeChange(nextNode) {
  props.state.previewNode = nextNode;
  props.state.previewPage = 0;
  emit("run", showPreview);
}
async function showPreview() {
  preview.value = await props.api.preview({ patch: readPatch(), layer: props.state.layer, node: props.state.previewNode || null, page: props.state.previewPage });
  previewPages.value = preview.value.card.pages || 1;
  emit("status", t("cmd.previewedStatus", { page: props.state.previewPage + 1, total: previewPages.value }));
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
  emit("status", t("cmd.selectedTextStatus"));
}
onMounted(() => {
  if (props.state.current) emit("run", showPreview);
});
</script>

<template>
  <section v-if="state.current && state.draft" class="panel-grid command-grid">
    <article class="card command-catalog">
      <h2>{{ t("cmd.catalogTitle") }}</h2>
      <input v-model="query" :placeholder="t('cmd.catalogSearch')">
      <div class="catalog-scroll">
        <details v-for="plugin in plugins" :key="plugin" open>
          <summary>{{ plugin }}</summary>
          <div v-for="node in visible.filter(item => item.plugin === plugin)" :key="node.id" class="command-row">
            <input type="checkbox" :checked="node.menu_entry || selected.includes(node.id)" :disabled="node.menu_entry || !node.enabled" @change="toggle(node, $event.target.checked)">
            <button type="button" class="link-button" @click="help = node; commandText = node.usage">{{ node.usage }}{{ node.enabled ? '' : t("cmd.disabledSuffix") }}</button>
          </div>
        </details>
      </div>
    </article>

    <article class="card">
      <h2>{{ t("cmd.configTitle") }}</h2>
      <div class="form-grid">
        <label>{{ t("cmd.menuTitle") }}<input v-model="state.draft.title" maxlength="80"></label>
        <label>{{ t("cmd.selectMode") }}<AbSelect v-model="panel.mode" :options="modeOptions" /></label>
      </div>
      <ol class="selected-list">
        <li v-for="(id, index) in selected" :key="id">
          <span>{{ state.catalog.nodes.find(node => node.id === id)?.command || t("cmd.missingNode", { id }) }}</span>
          <span>
            <button type="button" class="small-button" @click="move(index, -1)">{{ t("cmd.moveUp") }}</button>
            <button type="button" class="small-button" @click="move(index, 1)">{{ t("cmd.moveDown") }}</button>
          </span>
        </li>
      </ol>
      <label class="check-row"><input v-model="state.confirmBindings" type="checkbox"> {{ t("cmd.confirmBindings") }}</label>

      <h3>{{ t("cmd.layoutTitle") }}</h3>
      <div class="layout-grid">
        <fieldset v-for="layer in layers" :key="layer">
          <legend>{{ t(`cmd.layer.${layer}`) }}</legend>
          <label>{{ t("cmd.pageSize") }}<input v-model.number="state.draft.layout[layer].page_size" type="number" min="1" max="21"></label>
          <label>{{ t("cmd.columns") }}<input v-model.number="state.draft.layout[layer].columns" type="number" min="1" max="5"></label>
          <label>{{ t("cmd.style") }}<AbSelect v-model="state.draft.layout[layer].style" :options="styleOptions" /></label>
          <label class="check-row"><input v-model="state.draft.layout[layer].show_description" type="checkbox"> {{ t("cmd.showDesc") }}</label>
        </fieldset>
      </div>

      <details>
        <summary>{{ t("cmd.overridesSummary") }}</summary>
        <div class="override-block">
          <h4>{{ t("cmd.sceneOverridesTitle") }}</h4>
          <div class="override-add-row">
            <AbSelect v-model="newSceneKey" :options="sceneSelectOptions" />
            <AbSelect v-model="newSceneLayer" :options="layerOptions" />
            <button type="button" class="secondary" @click="addSceneOverride">{{ t("cmd.addOverride") }}</button>
          </div>
          <div v-if="sceneOverrideRows.length" class="override-list">
            <div v-for="row in sceneOverrideRows" :key="`${row.scene}-${row.layer}`" class="override-card">
              <div class="override-card-head">
                <span class="badge badge-primary">{{ t(`scene.${row.scene}`) }} · {{ t(`cmd.layer.${row.layer}`) }}</span>
                <button type="button" class="small-button" @click="removeSceneOverride(row.scene, row.layer)">{{ t("cmd.removeOverride") }}</button>
              </div>
              <div class="form-grid">
                <label>{{ t("cmd.pageSize") }}<input v-model.number="row.cfg.page_size" type="number" min="1" max="21"></label>
                <label>{{ t("cmd.columns") }}<input v-model.number="row.cfg.columns" type="number" min="1" max="5"></label>
                <label>{{ t("cmd.style") }}<AbSelect v-model="row.cfg.style" :options="styleOptions" /></label>
                <label class="check-row"><input v-model="row.cfg.show_description" type="checkbox"> {{ t("cmd.showDesc") }}</label>
              </div>
            </div>
          </div>
          <p v-else class="muted">{{ t("cmd.noOverrides") }}</p>
        </div>

        <div class="override-block">
          <h4>{{ t("cmd.nodeOverridesTitle") }}</h4>
          <div class="override-add-row">
            <AbSelect v-model="newNodeId" :options="overrideNodeOptions" editable :placeholder="t('cmd.previewNode')" />
            <button type="button" class="secondary" @click="addNodeOverride">{{ t("cmd.addOverride") }}</button>
          </div>
          <div v-if="nodeOverrideRows.length" class="override-list">
            <div v-for="row in nodeOverrideRows" :key="row.nodeId" class="override-card">
              <div class="override-card-head">
                <code>{{ row.label }}</code>
                <button type="button" class="small-button" @click="removeNodeOverride(row.nodeId)">{{ t("cmd.removeOverride") }}</button>
              </div>
              <div class="form-grid">
                <label>{{ t("cmd.pageSize") }}<input v-model.number="row.cfg.page_size" type="number" min="1" max="21"></label>
                <label>{{ t("cmd.columns") }}<input v-model.number="row.cfg.columns" type="number" min="1" max="5"></label>
                <label>{{ t("cmd.style") }}<AbSelect v-model="row.cfg.style" :options="styleOptions" /></label>
                <label class="check-row"><input v-model="row.cfg.show_description" type="checkbox"> {{ t("cmd.showDesc") }}</label>
              </div>
            </div>
          </div>
          <p v-else class="muted">{{ t("cmd.noOverrides") }}</p>
        </div>
      </details>

      <details>
        <summary>{{ t("cmd.extSummary") }}</summary>
        <div class="form-grid">
          <label>{{ t("cmd.mediaMaxBytes") }}<input v-model.number="state.extensions.media_max_bytes" type="number" min="1" max="200000000"></label>
          <label>{{ t("cmd.streamFallback") }}<AbSelect v-model="state.extensions.stream_fallback" :options="streamFallbackOptions" /></label>
          <label>{{ t("cmd.streamMaxChars") }}<input v-model.number="state.extensions.stream_max_chars" type="number" min="1" max="4096"></label>
          <label>{{ t("cmd.streamTimeout") }}<input v-model.number="state.extensions.stream_timeout" type="number" min="1" max="180"></label>
          <label>{{ t("cmd.ticketTtl") }}<input v-model.number="state.extensions.ticket_ttl" type="number" min="30" max="300"></label>
        </div>
        <label class="check-row"><input v-model="state.extensions.typing_enabled" type="checkbox"> {{ t("cmd.typingEnabled") }}</label>
        <label class="check-row"><input v-model="state.extensions.keyboard_enabled" type="checkbox"> {{ t("cmd.keyboardEnabled") }}</label>
        <label class="check-row"><input v-model="state.extensions.keyboard_execute" type="checkbox"> {{ t("cmd.keyboardExecute") }}</label>
        <label class="check-row"><input v-model="state.extensions.management_writes" type="checkbox"> {{ t("cmd.managementWrites") }}</label>
      </details>

      <div class="actions" style="margin-top: 14px;">
        <button type="button" :disabled="busy" @click="emit('run', () => mutate('save'))">{{ t("cmd.saveDraft") }}</button>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', () => mutate('apply'))">{{ t("cmd.applyLocal") }}</button>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', () => mutate('discard'))">{{ t("cmd.discardDraft") }}</button>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', () => mutate('defaults'))">{{ t("cmd.restoreDefaults") }}</button>
      </div>
      <div v-if="versionOptions.length" class="form-grid" style="align-items: end; margin-top: 12px;">
        <label>{{ t("cmd.retainedVersions") }}<AbSelect v-model="state.restoreRevision" :options="versionOptions" /></label>
        <button type="button" class="secondary" :disabled="busy" @click="emit('run', () => mutate('restore'))">{{ t("cmd.restoreBtn") }}</button>
      </div>
    </article>

    <article class="card preview-card">
      <div class="card-head">
        <h2>{{ t("cmd.previewTitle") }}</h2>
        <div class="actions">
          <button type="button" :disabled="busy" @click="emit('run', showPreview)">{{ t("cmd.previewBtn") }}</button>
          <button type="button" class="secondary" :disabled="busy || !preview || state.previewPage === 0" @click="emit('run', () => changePage(-1))">{{ t("cmd.prevPage") }}</button>
          <span v-if="preview" class="runtime-pill">{{ t("cmd.previewPageInfo", { page: state.previewPage + 1, total: previewPages }) }}</span>
          <button type="button" class="secondary" :disabled="busy || !preview || state.previewPage + 1 >= previewPages" @click="emit('run', () => changePage(1))">{{ t("cmd.nextPage") }}</button>
        </div>
      </div>

      <div class="form-grid preview-controls">
        <label>{{ t("cmd.previewLayer") }}<AbSelect :model-value="state.layer" :options="layerOptions" @change="onLayerChange" /></label>
        <label>{{ t("cmd.previewNode") }}<AbSelect :model-value="state.previewNode" :options="nodeOptions" :disabled="state.layer === 'home'" @change="onNodeChange" /></label>
      </div>

      <div v-if="preview" class="preview-stage">
        <div class="qq-preview-pane">
          <div class="qq-pane-head">
            <strong>{{ state.draft.title }}</strong>
            <span class="badge badge-primary">{{ t(`cmd.layer.${state.layer}`) }} · {{ t(`cmd.style${(preview.card.layout?.style || 'plain').replace(/^./, c => c.toUpperCase())}`) }}</span>
          </div>
          <div v-if="preview.card.items?.length" class="qq-msg-list" :class="`style-${preview.card.layout?.style || 'plain'}`">
            <div v-for="item in preview.card.items" :key="item.id || item.usage" class="qq-msg-item">
              <code>{{ item.usage }}</code>
              <span v-if="preview.card.layout?.show_description && item.description" class="muted">{{ item.description }}</span>
            </div>
          </div>
          <p v-else class="muted" style="padding: 20px 0; text-align: center;">—</p>
        </div>

        <div class="qq-preview-pane">
          <div class="qq-pane-head">
            <strong>{{ t("cmd.columns") }}: {{ preview.card.layout?.columns || 2 }}</strong>
            <span class="badge badge-muted">{{ preview.card.items?.length || 0 }} / {{ preview.card.page_size || 0 }}</span>
          </div>
          <div
            class="qq-kb-grid"
            :style="{ gridTemplateColumns: `repeat(${Math.max(1, Math.min(5, preview.card.layout?.columns || 2))}, minmax(0, 1fr))` }"
          >
            <div v-for="item in preview.card.items" :key="item.id || item.usage" class="qq-kb-btn" @click="help = item; commandText = item.usage">
              <span>{{ item.command || item.name }}</span>
            </div>
          </div>
          <div class="qq-kb-nav">
            <span class="qq-kb-nav-btn">{{ t("cmd.prevPage") }}</span>
            <span class="qq-kb-nav-btn">{{ t("cmd.previewPageInfo", { page: state.previewPage + 1, total: previewPages }) }}</span>
            <span class="qq-kb-nav-btn">{{ t("cmd.nextPage") }}</span>
            <span class="qq-kb-nav-btn">{{ t("cmd.layer.home") }}</span>
          </div>
        </div>
      </div>

      <div v-if="help" class="help-box">
        <div class="override-card-head">
          <strong>{{ help.name }}</strong>
          <button class="secondary" type="button" @click="selectText">{{ t("cmd.selectCommandText") }}</button>
        </div>
        <p class="muted">{{ help.description }}</p>
        <textarea ref="commandRef" v-model="commandText" readonly rows="2"></textarea>
      </div>
    </article>
  </section>
  <section v-else class="empty-state"><h2>{{ t("cmd.emptyTitle") }}</h2><p class="muted">{{ t("cmd.emptyDesc") }}</p></section>
</template>
