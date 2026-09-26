<script setup>
import { onMounted, reactive, ref } from "vue";
import { createApi } from "./api";
import ConnectionPanel from "./ConnectionPanel.vue";
import CommandPanel from "./CommandPanel.vue";
import RemotePanel from "./RemotePanel.vue";
import "./styles.css";

const bridge = window.AstrBotPluginPage;
const status = ref("正在等 AstrBot 页面接口。");
const busy = ref(false);
const tab = ref("connection");
const state = reactive({
  bridge, boot: null, current: null, catalog: null, draft: null, view: null, scan: null,
  platformId: "qq_v2", scene: "group", loadedScene: "", layer: "home", previewNode: "",
  confirmBindings: false, restoreRevision: 0, sceneOverrides: "{}", nodeOverrides: "{}",
  mediaRoots: "", extensions: {}, secret: "", secretAction: "keep", confirmSecret: false, confirmIdentity: false,
  networkToken: "", networkTokenAction: "keep", confirmNetworkToken: false, confirmNetworkWrites: false,
  panelTargetType: "all", panelTargets: "", menuOnly: false,
  form: { appid: "", environment: "production", transport: "websocket", intents: 0, shardText: "[0,1]", enable: false, onebot: { enable: false, host: "127.0.0.1", port: 8080, writes: false } },
});
const api = createApi(state);

function fill(value) {
  state.view = value;
  state.platformId = value.platform_id;
  state.form = { ...value.fields, shardText: JSON.stringify(value.fields.shard), onebot: { ...value.fields.onebot } };
  state.secret = "";
  state.secretAction = "keep";
  state.networkToken = "";
  state.networkTokenAction = "keep";
}
async function loadConfig() {
  const id = state.platformId;
  state.current = await bridge.apiGet("config", { platform_id: id });
  await api.loadCatalog();
  state.draft = JSON.parse(JSON.stringify(state.current.draft));
  state.sceneOverrides = JSON.stringify(state.draft.scene_overrides, null, 2);
  state.nodeOverrides = JSON.stringify(state.draft.node_overrides, null, 2);
  state.mediaRoots = state.draft.extensions.media_roots.join("\n");
  state.extensions = { ...state.draft.extensions };
  state.restoreRevision = state.current.versions.at(-1) || 0;
}
async function read() {
  fill(await bridge.apiGet("connection", { platform_id: state.platformId.trim() }));
  await loadConfig();
  status.value = "读好了。保存连接不会让机器人上线。";
}
async function run(action) {
  if (busy.value) return;
  busy.value = true;
  try { await action(); }
  catch (error) { status.value = error.message || "没完成。"; }
  finally { busy.value = false; }
}
onMounted(() => run(async () => {
  if (!bridge) throw new Error("请从 AstrBot 插件页面打开。");
  await bridge.ready();
  state.boot = await bridge.apiGet("bootstrap");
  state.platformId = state.boot.instances[0]?.id || "qq_v2";
  await read();
}));
</script>

<template>
  <div class="min-h-screen bg-[#F8F9FA] p-6 text-[#202124] lg:p-10">
    <div class="mx-auto max-w-6xl space-y-6">
      <header class="flex flex-col justify-between gap-4 border-b border-[#E0E2EC] pb-6 md:flex-row md:items-center">
        <div>
          <p class="text-xs font-semibold uppercase tracking-wider text-[#5F6368]">QQ Official V2</p>
          <h1 class="text-2xl font-bold">机器人设置</h1>
          <p class="mt-1 text-sm text-[#5F6368]">{{ status }}</p>
        </div>
        <div class="flex items-center gap-3 rounded-full border border-[#DADCE0] bg-white p-1.5 pl-4">
          <select v-if="state.boot?.instances.length" v-model="state.platformId" class="bg-transparent text-sm outline-none" @change="run(read)">
            <option v-for="item in state.boot.instances" :key="item.id" :value="item.id">{{ item.id }}</option>
          </select>
          <input v-else v-model="state.platformId" maxlength="128" class="w-28 bg-transparent text-sm outline-none">
          <select v-model="state.scene" class="bg-transparent text-sm outline-none" @change="run(loadConfig)">
            <option value="group">群聊</option><option value="c2c">私聊</option><option value="channel">频道</option><option value="dm">频道私信</option>
          </select>
          <button class="rounded-full bg-[#1A73E8] px-4 py-1.5 text-sm text-white" :disabled="busy" @click="run(read)">重新读取</button>
        </div>
      </header>
      <nav class="flex gap-2">
        <button v-for="[id, label] in [['connection', '连接'], ['commands', '指令'], ['remote', 'QQ 面板']]" :key="id" class="rounded-full px-4 py-2 text-sm" :class="tab === id ? 'bg-[#1A73E8] text-white' : 'bg-white'" @click="tab = id">{{ label }}</button>
      </nav>
      <ConnectionPanel v-if="tab === 'connection'" :api="api" :state="state" :busy="busy" @status="status = $event" @run="run" />
      <CommandPanel v-else-if="tab === 'commands'" :api="api" :state="state" :busy="busy" @status="status = $event" @run="run" @reload="run(loadConfig)" />
      <RemotePanel v-else :api="api" :state="state" :busy="busy" @status="status = $event" @run="run" />
    </div>
  </div>
</template>
