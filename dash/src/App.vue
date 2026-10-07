<script setup>
import { computed, onMounted, reactive, ref } from "vue";
import ConnectionPanel from "./ConnectionPanel.vue";
import CommandPanel from "./CommandPanel.vue";
import RemotePanel from "./RemotePanel.vue";
import { changed, clone, connectionFields, createApi } from "./api";
import "./styles.css";

const bridge = window.AstrBotPluginPage;
const status = ref("等待 AstrBot Plugin Pages bridge。");
const busy = ref(false);
const tab = ref("connection");
const targetInput = ref("qq_v2");
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
  sceneOverrides: "{}",
  nodeOverrides: "{}",
  extensions: {},
  secret: "",
  secretAction: "keep",
  confirmSecret: false,
  confirmIdentity: false,
  networkToken: "",
  networkTokenAction: "keep",
  confirmNetworkToken: false,
  confirmNetworkWrites: false,
  panelTargetType: "all",
  panelTargets: "",
  menuOnly: false,
  form: {
    appid: "", use_markdown: true, type: "qq_official_v2", intents: 1174409216,
    shard_mode: "auto", shardIndex: 0, shardCount: 1, enable: false,
    onebot: { enable: false, host: "127.0.0.1", port: 5700, writes: false },
  },
});
const api = createApi(state);

const connectionDirty = computed(() => {
  if (!state.view) return false;
  try {
    return Object.keys(changed(state.view.fields, connectionFields(state.form))).length > 0
      || Boolean(state.secret) || Boolean(state.networkToken)
      || state.secretAction !== "keep" || state.networkTokenAction !== "keep";
  } catch {
    return true;
  }
});
const draftDirty = computed(() => Boolean(state.current && state.draftSnapshot && JSON.stringify(state.draft) !== state.draftSnapshot));
const dirty = computed(() => connectionDirty.value || draftDirty.value);

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
    onebot: { enable: false, host: "127.0.0.1", port: 5700, writes: false, ...(fields.onebot || {}) },
  };
  state.secret = "";
  state.secretAction = "keep";
  state.confirmSecret = false;
  state.confirmIdentity = false;
  state.networkToken = "";
  state.networkTokenAction = "keep";
  state.confirmNetworkToken = false;
  state.confirmNetworkWrites = false;
  state.scan = null;
}

function fillConfig(value, catalog) {
  state.current = value;
  state.catalog = catalog;
  state.loadedScene = state.scene;
  state.draft = clone(value.draft);
  state.draftSnapshot = JSON.stringify(state.draft);
  state.sceneOverrides = JSON.stringify(state.draft.scene_overrides, null, 2);
  state.nodeOverrides = JSON.stringify(state.draft.node_overrides, null, 2);
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
  if (!id) throw new Error("请输入目标 ID。");
  if (!force && dirty.value && !window.confirm("当前页面有未保存的编辑，确认丢弃并切换目标？")) return;
  const result = await fetchTarget(id);
  fillConnection(result.view);
  if (result.current) fillConfig(result.current, result.catalog);
  else {
    state.current = null;
    state.catalog = null;
    state.draft = null;
    state.draftSnapshot = "";
  }
  status.value = result.view.exists ? "已读取当前实例、SDK 能力和本地配置。" : "目标尚未保存，可以先填写连接并保存。";
}

async function changeScene() {
  if (!state.current) return;
  if (dirty.value && !window.confirm("当前页面有未保存的编辑，确认丢弃并切换场景？")) return;
  const [current, catalog] = await Promise.all([
    bridge.apiGet("config", { platform_id: state.platformId }),
    bridge.apiGet("commands", { platform_id: state.platformId, scene: state.scene }),
  ]);
  fillConfig(current, catalog);
  status.value = `已读取${state.scene}场景。`;
}

async function run(action) {
  if (busy.value) return;
  busy.value = true;
  try { await action(); }
  catch (error) { status.value = error?.message || "操作没有完成。"; }
  finally { busy.value = false; }
}

async function reloadPage() {
  await read(true);
}

onMounted(() => run(async () => {
  if (!bridge) throw new Error("请从 AstrBot Plugin Pages 打开此页。");
  await bridge.ready();
  state.boot = await bridge.apiGet("bootstrap");
  targetInput.value = state.boot.instances[0]?.id || "qq_v2";
  await read(true);
}));
</script>

<template>
  <div class="page-shell">
    <header class="page-header">
      <div>
        <p class="eyebrow">QQ OFFICIAL V2 · SDK CONTROL PLANE</p>
        <h1>机器人设置</h1>
        <p class="status" role="status" aria-live="polite">{{ status }}</p>
      </div>
      <div class="toolbar target-toolbar">
        <label>目标 ID
          <input v-model="targetInput" list="target-options" maxlength="128" autocomplete="off" @keyup.enter="run(reloadPage)">
          <datalist id="target-options">
            <option v-for="item in state.boot?.instances || []" :key="item.id" :value="item.id">{{ item.appid || "未配置" }}</option>
          </datalist>
        </label>
        <label>场景
          <select v-model="state.scene" @change="run(changeScene)">
            <option value="group">群聊</option><option value="c2c">单聊</option><option value="channel">文字子频道</option><option value="dm">频道私信</option>
          </select>
        </label>
        <button :disabled="busy" @click="run(reloadPage)">重新读取</button>
      </div>
    </header>

    <section v-if="state.view" class="runtime-strip">
      <span>{{ state.view.platform_id }} · AppID {{ state.view.identity?.appid || "未配置" }}</span>
      <span>状态：{{ state.view.runtime?.state || state.view.runtime_state || "未加载" }}</span>
      <span>消息：{{ state.view.runtime?.message_ready ? "可发送" : "未就绪" }}</span>
      <span v-if="state.view.runtime?.gateway_group">网关：{{ state.view.runtime.gateway_group.connected }}/{{ state.view.runtime.gateway_group.planned || state.view.runtime.gateway_group.recommended || "?" }}</span>
    </section>

    <nav class="tabs" aria-label="控制面板">
      <button :class="{active: tab === 'connection'}" @click="tab = 'connection'">连接与状态</button>
      <button :class="{active: tab === 'commands'}" :disabled="!state.current" @click="tab = 'commands'">本地菜单</button>
      <button :class="{active: tab === 'remote'}" :disabled="!state.current" @click="tab = 'remote'">QQ 面板</button>
    </nav>

    <ConnectionPanel v-if="tab === 'connection'" :api="api" :state="state" :busy="busy" @status="status = $event" @run="run" @reload="reloadPage" />
    <CommandPanel v-else-if="tab === 'commands'" :api="api" :state="state" :busy="busy" @status="status = $event" @run="run" @reload="reloadPage" />
    <RemotePanel v-else :api="api" :state="state" :busy="busy" @status="status = $event" @run="run" />
  </div>
</template>
