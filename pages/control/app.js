const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
let boot, current, catalog, draft, loadedScene, page = 0, pageCount = 1, loading = false;
let panelPlan, panelPlanScope;
let retainedView;
const retainedSelection = new Set();
const clone = (value) => JSON.parse(JSON.stringify(value));
const scene = () => $("scene").value;
const selection = () => draft.panels[scene()];
function element(tag, text = "") { const node = document.createElement(tag); node.textContent = text; return node; }
function option(value, label) { const node = element("option", label); node.value = value; return node; }
function report(message) { $("status").textContent = message; }
function changed(old, next) {
  const patch = {};
  for (const key of new Set([...Object.keys(old), ...Object.keys(next)])) {
    if (!(key in next)) patch[key] = null;
    else if (JSON.stringify(old[key]) !== JSON.stringify(next[key])) {
      if (old[key] && next[key] && typeof old[key] === "object" && typeof next[key] === "object" && !Array.isArray(next[key])) {
        const nested = changed(old[key], next[key]);
        if (Object.keys(nested).length) patch[key] = nested;
      } else patch[key] = next[key];
    }
  }
  return patch;
}
async function run(action) {
  if (loading) return;
  loading = true;
  const controls = [...document.querySelectorAll("button, select, input, textarea")];
  const before = controls.map((node) => node.disabled);
  controls.forEach((node) => { node.disabled = true; });
  try { await action(); } catch (error) { report(error.message); }
  finally { controls.forEach((node, i) => { node.disabled = before[i]; }); loading = false; }
}
function renderExtensions() {
  const value = draft.extensions;
  $("ext-media-roots").value = value.media_roots.join("\n");
  for (const [id, key] of [["ext-media-max", "media_max_bytes"], ["ext-stream-chars", "stream_max_chars"], ["ext-stream-timeout", "stream_timeout"], ["ext-ticket-ttl", "ticket_ttl"], ["ext-stream-fallback", "stream_fallback"]]) $(id).value = String(value[key]);
  for (const [id, key] of [["ext-typing", "typing_enabled"], ["ext-keyboard", "keyboard_enabled"], ["ext-execute", "keyboard_execute"], ["ext-management", "management_writes"]]) $(id).checked = value[key];
  $("ext-status").textContent = JSON.stringify(current.extension_state || {state: "not_loaded"}, null, 2);
}
function readExtensions() {
  return Object.assign(clone(draft.extensions), {media_roots: $("ext-media-roots").value.split("\n").map(v => v.trim()).filter(Boolean), media_max_bytes: Number($("ext-media-max").value),
    stream_fallback: $("ext-stream-fallback").value, stream_max_chars: Number($("ext-stream-chars").value), stream_timeout: Number($("ext-stream-timeout").value),
    ticket_ttl: Number($("ext-ticket-ttl").value), typing_enabled: $("ext-typing").checked, keyboard_enabled: $("ext-keyboard").checked,
    keyboard_execute: $("ext-execute").checked, management_writes: $("ext-management").checked});
}
function readDraft() {
  draft.title = $("title").value;
  draft.scene_overrides = JSON.parse($("scene-overrides").value);
  draft.node_overrides = JSON.parse($("node-overrides").value);
  draft.extensions = readExtensions();
  return changed(current.draft, draft);
}
function payload(extra = {}) {
  return {platform_id: current.platform_id, scene: scene(), revision: current.revision,
    fingerprint: current.fingerprint, csrf: boot.csrf, ...extra};
}
function selectedIds() {
  return selection().mode === "default" ? catalog.nodes.filter(n => n.system && n.enabled).map(n => n.id) : selection().selected;
}
function customize() {
  if (selection().mode === "default") selection().selected = selectedIds();
  selection().mode = "custom";
  $("mode").value = "custom";
}
function help(node) {
  $("help").replaceChildren();
  for (const text of [node.name, node.description, `来源：${node.plugin} / ${node.id}`, `别名：${node.aliases.join("、")}`,
    `权限：${node.permission.join("、")}`, node.compatibility, ...node.reasons, ...node.conditions]) $("help").append(element("p", text));
  $("command-copy").value = node.usage;
  const fields = [];
  for (const param of node.parameters) {
    const label = element("label", `${param.name} · ${param.required ? "必填" : "可选"} · ${param.type}${param.greedy ? "（剩余文本）" : ""}`);
    const input = element("input");
    input.placeholder = param.default_visible ? `默认 ${JSON.stringify(param.default)}` : "默认值未公开 / 无默认值";
    label.append(input); $("help").append(label); fields.push([param, input]);
  }
  if (fields.length) {
    const button = element("button", "生成预填文本（不执行）");
    button.onclick = () => {
      const parts = []; let omitted = false;
      for (const [param, input] of fields) {
        const value = input.value.trim();
        if (!value) { if (param.required) return report(`请填写 ${param.name}`); omitted = true; continue; }
        if (omitted) return report("不能跳过前面的可选位置参数。");
        if (!param.greedy && /\s/.test(value)) return report("普通位置参数按空白拆分，不支持 Shell 引号。");
        if (/[\r\n]/.test(value)) return report("命令不能包含换行。");
        parts.push(value);
      }
      $("command-copy").value = [node.command, ...parts].join(" "); report("已生成文本，未执行。");
    };
    $("help").append(button);
  }
}
function renderCatalog() {
  $("catalog").replaceChildren();
  const query = $("search").value.toLowerCase();
  const groups = new Map();
  for (const node of catalog.nodes) {
    if (!`${node.name} ${node.plugin} ${node.description}`.toLowerCase().includes(query)) continue;
    if (!groups.has(node.plugin)) {
      const group = element("details"); group.open = true;
      group.append(element("summary", `${node.system ? "系统" : "插件"} · ${node.plugin}`));
      groups.set(node.plugin, group); $("catalog").append(group);
    }
    const row = element("div"); row.className = "command";
    const check = element("input"); check.type = "checkbox";
    check.checked = node.menu_entry || selectedIds().includes(node.id);
    check.disabled = node.menu_entry || !node.enabled;
    check.setAttribute("aria-label", `固定 ${node.name}`);
    check.onchange = () => { customize(); selection().selected = selection().selected.filter(id => id !== node.id); if (check.checked) selection().selected.push(node.id); renderSelected(); };
    const button = element("button", `${node.parent ? "↳ " : ""}${node.usage} [${node.permission.join(", ")}]${node.enabled ? "" : "（不可用）"}`);
    button.onclick = () => help(node); row.append(check, button); groups.get(node.plugin).append(row);
  }
}
function renderSelected() {
  $("selected").replaceChildren();
  const ids = [...selectedIds(), ...catalog.nodes.filter(n => n.menu_entry && n.enabled).map(n => n.id)];
  for (const [index, id] of [...new Set(ids)].entries()) {
    const node = catalog.nodes.find(n => n.id === id);
    const item = element("li", node ? node.command : `缺失指令：${id}`);
    if (!node?.menu_entry) {
      for (const [label, delta] of [["上移", -1], ["下移", 1]]) {
        const button = element("button", label); button.setAttribute("aria-label", `${label} ${node?.name || id}`);
        button.onclick = () => {
          customize(); const list = selection().selected; const target = index + delta;
          if (target >= 0 && target < list.length) [list[index], list[target]] = [list[target], list[index]];
          renderSelected(); renderCatalog();
        }; item.append(button);
      }
      const remove = element("button", "移回菜单");
      remove.onclick = () => { customize(); selection().selected = selection().selected.filter(key => key !== id); renderSelected(); renderCatalog(); };
      item.append(remove);
    }
    $("selected").append(item);
  }
}
function renderLayouts() {
  $("layouts").replaceChildren();
  for (const [layer, labelText] of [["home", "首页"], ["plugin", "插件页"], ["group", "指令组"], ["detail", "详情页"]]) {
    const area = element("div"); area.className = "layer";
    area.append(element("strong", labelText));
    for (const [key, title] of [["page_size", "每页条数"], ["columns", "每行按钮"]]) {
      const label = element("label", title); const input = element("input"); input.type = "number";
      input.min = 1; input.max = key === "columns" ? 5 : 21; input.value = draft.layout[layer][key];
      input.oninput = () => { draft.layout[layer][key] = Number(input.value); }; label.append(input); area.append(label);
    }
    const styleLabel = element("label", "文本样式"); const style = element("select");
    for (const value of ["plain", "heading", "quote"]) style.append(option(value, value));
    style.value = draft.layout[layer].style; style.onchange = () => { draft.layout[layer].style = style.value; };
    styleLabel.append(style); area.append(styleLabel);
    const descLabel = element("label", "显示摘要"); const desc = element("input"); desc.type = "checkbox"; desc.checked = draft.layout[layer].show_description;
    desc.onchange = () => { draft.layout[layer].show_description = desc.checked; }; descLabel.append(desc); area.append(descLabel);
    $("layouts").append(area);
  }
}
function renderNodes() {
  $("preview-node").replaceChildren(option("", "根节点"));
  const layer = $("layer").value;
  if (layer === "plugin") for (const name of new Set(catalog.nodes.map(n => n.plugin))) $("preview-node").append(option(name, name));
  else for (const node of catalog.nodes.filter(n => layer !== "group" || n.group)) $("preview-node").append(option(node.id, node.name));
  page = 0;
}
async function load() {
  const id = $("instance").value;
  $("editor").hidden = true;
  retainedView = null; retainedSelection.clear(); $("retained-list").replaceChildren();
  $("retained-status").textContent = "请读取当前实例的保留事件；不会自动清理。";
  if (!id) return report("请先在接入区或本体平台管理中添加 QQ 官方 V2，再重新读取目录实例。");
  current = await bridge.apiGet("config", {platform_id: id});
  catalog = await bridge.apiGet("commands", {platform_id: id, scene: scene()});
  loadedScene = scene();
  $("card-preview").replaceChildren();
  draft = clone(current.draft);
  panelPlan = null; panelPlanScope = null; $("confirm-bindings").checked = false;
  $("remote-gate-state").textContent = boot.flags?.remote_menu_sync ? "总闸开启" : "总闸关闭";
  $("panel-output").textContent = JSON.stringify(current.remote_state?.[scene()] || {state: "not_managed"}, null, 2);
  $("title").value = draft.title; $("mode").value = selection().mode;
  $("scene-overrides").value = JSON.stringify(draft.scene_overrides, null, 2);
  $("node-overrides").value = JSON.stringify(draft.node_overrides, null, 2);
  $("versions").replaceChildren(...current.versions.map(v => option(v, `版本 ${v}`)));
  $("versions-state").textContent = `草稿 ${current.revision} / 已应用 ${current.applied_revision} / 远端 ${current.remote_state?.[scene()]?.state || "not_managed"}`;
  $("connection").textContent = `${current.platform_id} · AppID ${current.identity.appid} · ${current.identity.environment} · 凭据${current.credentials_configured ? "已配置" : "未配置"} · ${current.runtime_state}。连接配置保存与重载分开。`;
  $("preview-output").textContent = "尚未预览；不发送真实消息。";
  $("help").replaceChildren(); $("command-copy").value = "";
  renderLayouts(); renderCatalog(); renderSelected(); renderNodes();
  renderExtensions();
  $("editor").hidden = false; report("已读取真实目录与本地配置。草稿、本地应用、QQ面板托管分别确认；聊天可使用 v2menu 帮助。");
}
async function preview() {
  const result = await bridge.apiPost("preview", payload({patch: readDraft(), layer: $("layer").value, node: $("preview-node").value || null, page}));
  pageCount = result.card.pages;
  const card = $("card-preview"); card.replaceChildren();
  card.append(element(result.card.layout.style === "heading" ? "h3" : "p", draft.title));
  for (const item of result.card.items) {
    const row = element(result.card.layout.style === "quote" ? "blockquote" : "div");
    const button = element("button", item.usage); button.onclick = () => help(item); row.append(button);
    if (result.card.layout.show_description) row.append(element("p", item.description));
    card.append(row);
  }
  card.append(element("p", `第 ${page + 1}/${pageCount} 页 · 本地预览，不代表 QQ 客户端渲染。`));
  $("preview-output").textContent = JSON.stringify(result, null, 2);
  report(`本地预览 ${page + 1}/${pageCount} · 面板 ${result.panel.slots}/20 · ${result.panel.issues.length} 项面板问题（应用布局不等于发布）。`);
}
async function mutate(operation) {
  const patch = readDraft();
  if (operation === "apply" && Object.keys(patch).length) throw new Error("请先保存未保存的编辑，再应用草稿。");
  if (operation !== "save" && !window.confirm("确认此本地配置操作？不会写入 QQ。")) return;
  await bridge.apiPost("config/mutate", payload({operation, patch, confirm: operation === "apply", confirm_bindings: $("confirm-bindings").checked, restore_revision: Number($("versions").value)}));
  await load();
}
function allowReload() {
  if (!current || $("editor").hidden) return true;
  let dirty = true;
  try { dirty = Object.keys(readDraft()).length > 0; } catch { /* Invalid JSON is also unsaved work. */ }
  if (!dirty || window.confirm("有未保存编辑，确认丢弃并重新读取？")) return true;
  $("instance").value = current.platform_id; $("scene").value = loadedScene;
  return false;
}
$("refresh").onclick = () => run(async () => {
  if (!allowReload()) return;
  boot = await bridge.apiGet("bootstrap");
  const selected = $("instance").value;
  $("instance").replaceChildren(...boot.instances.map(item => option(item.id, `${item.id} · ${item.appid || "未配置"}`)));
  if (boot.instances.some(item => item.id === selected)) $("instance").value = selected;
  await load();
});
$("ext-refresh").onclick = () => run(async () => { if (allowReload()) await load(); });
$("instance").onchange = () => run(async () => { if (allowReload()) await load(); });
$("scene").onchange = () => run(async () => { if (allowReload()) await load(); });
$("search").oninput = () => { if (catalog) renderCatalog(); };
$("mode").onchange = () => { const mode = $("mode").value; if (mode === "custom") customize(); else selection().mode = mode; renderCatalog(); renderSelected(); };
$("layer").onchange = () => renderNodes();
$("preview-node").onchange = () => { page = 0; };
$("preview").onclick = () => run(async () => { page = 0; await preview(); });
$("prev").onclick = () => run(async () => { if (page > 0) { page--; await preview(); } });
$("next").onclick = () => run(async () => { if (page + 1 < pageCount) { page++; await preview(); } });
for (const operation of ["save", "apply", "discard", "defaults", "restore"]) $(operation).onclick = () => run(() => mutate(operation));
$("copy").onclick = () => { $("command-copy").focus(); $("command-copy").select(); report("文本已选中，请复制；未执行指令。"); };
$("disable-ui").onclick = () => run(async () => {
  if (!window.confirm("关闭后需在 AstrBot 插件设置重新开启，是否继续？")) return;
  await cancelBinding();
  await bridge.apiPost("flags", {csrf: boot.csrf, revision: boot.flags_revision, patch: {webui_enabled: false}, confirm: true});
  $("editor").hidden = true; report("管理 API 已关闭，请从本体插件设置恢复。");
  $("connect-form").hidden = true; $("connect-secret").value = "";
});
function panelOptions() {
  return {target_type: $("panel-target-type").value || "all", targets: $("panel-targets").value.split(",").map(v => v.trim()).filter(Boolean), menu_only: $("panel-menu-only").checked};
}
function panelScope() { return JSON.stringify([current.platform_id, current.fingerprint, current.applied_revision, scene(), panelOptions()]); }
$("remote-gate").onclick = () => run(async () => {
  const enabled = !boot.flags?.remote_menu_sync;
  if (!window.confirm(enabled ? "开启远端总闸？将恢复已确认范围的自动同步；不会自动接管人工面板。" : "关闭远端同步总闸？远端面板保留，在途结果仍需核对。")) return;
  const value = await bridge.apiPost("flags", {csrf: boot.csrf, revision: boot.flags_revision, patch: {remote_menu_sync: enabled}, confirm: true});
  boot.flags = value.flags; boot.flags_revision = value.revision;
  $("remote-gate-state").textContent = enabled ? "总闸开启" : "总闸关闭";
});
$("panel-plan").onclick = () => run(async () => {
  if (Object.keys(readDraft()).length || current.revision !== current.applied_revision) throw new Error("先保存并应用本地配置；发布预览只使用已应用版本。");
  panelPlan = await bridge.apiPost("panels/plan", payload(panelOptions())); panelPlanScope = panelScope();
  $("panel-output").textContent = JSON.stringify(panelPlan, null, 2);
});
$("panel-enable").onclick = () => run(async () => {
  if (!boot.flags?.remote_menu_sync) throw new Error("请先明确开启远端同步总闸。");
  if (!panelPlan || panelPlanScope !== panelScope()) throw new Error("实例、范围或选项变化，请重新预览发布范围。");
  if (panelPlan.issues.length) throw new Error("发布预览仍有阻碍，请调整配置或明确只发布菜单入口。");
  if (!window.confirm(`确认托管 AppID ${current.identity.appid} / ${scene()} / ${panelOptions().target_type}，并向 QQ 发布？后续稳定目录变化将自动同步。`)) return;
  const result = await bridge.apiPost("panels/enable", payload({...panelOptions(), plan_fingerprint: panelPlan.fingerprint, confirm: true}));
  $("panel-output").textContent = JSON.stringify(result, null, 2); panelPlan = null;
});
for (const operation of ["sync", "disable"]) $("panel-" + operation).onclick = () => run(async () => {
  if (!window.confirm(operation === "sync" ? "核对并同步此已托管范围？未知创建不会盲目重建。" : "停止此范围托管？不会删除远端面板。")) return;
  const value = await bridge.apiPost("panels/" + operation, payload({confirm: true}));
  $("panel-output").textContent = JSON.stringify(value, null, 2); panelPlan = null;
});

async function readRetained() {
  const platformId = $("instance").value;
  if (!platformId) throw new Error("请先选择顶部目录实例。");
  retainedView = null; retainedSelection.clear(); $("retained-list").replaceChildren();
  const value = await bridge.apiGet("inbox/retained", {platform_id: platformId});
  retainedView = value;
  for (const entry of value.entries) {
    const label = element("label"), check = element("input"); check.type = "checkbox";
    check.onchange = () => { if (check.checked) retainedSelection.add(entry.receipt); else retainedSelection.delete(entry.receipt); };
    label.append(check, element("span", ` #${entry.receipt} · ${entry.event_type} · ${entry.state} · ${entry.reason} · ${entry.size}字节 · ${new Date(entry.received_at * 1000).toISOString()}`));
    $("retained-list").append(label);
  }
  $("retained-status").textContent = `${value.platform_id} · ${JSON.stringify(value.counts)} · 确认60秒内有效，默认不选择任何记录。`;
}
$("retained-read").onclick = () => run(readRetained);
$("retained-select-all").onclick = () => run(async () => {
  if (!retainedView || retainedView.platform_id !== $("instance").value) throw new Error("请先读取当前实例的保留事件。");
  retainedView.entries.forEach(entry => retainedSelection.add(entry.receipt));
  for (const row of $("retained-list").children) row.children[0].checked = true;
  report(`已勾选本次读取的 ${retainedSelection.size} 条保留项，尚未丢弃。`);
});
$("retained-discard").onclick = () => run(async () => {
  if (!retainedView || retainedView.platform_id !== $("instance").value) throw new Error("请重新读取当前实例的保留事件。");
  const entries = retainedView.entries.filter(entry => retainedSelection.has(entry.receipt)).map(({receipt, version, confirmation}) => ({receipt, version, confirmation}));
  if (!entries.length) throw new Error("请明确勾选要丢弃的保留事件。");
  if (!window.confirm(`永久丢弃实例 ${retainedView.platform_id} 的 ${entries.length} 条保留事件？这些事件不会再处理，操作不可恢复；待交付聊天和发送账本不会删除。`)) return;
  let result;
  try {
    result = await bridge.apiPost("inbox/discard", {platform_id: retainedView.platform_id, fingerprint: retainedView.fingerprint, expires: retainedView.expires, csrf: boot.csrf, entries, confirm: true});
  } finally {
    retainedView = null; retainedSelection.clear(); $("retained-list").replaceChildren();
  }
  await readRetained(); report(`已明确丢弃 ${result.discarded} 条保留事件，其他记录未删除。`);
});

let connectionView, binding, bindingTimer;
const bindingActive = () => binding && ["creating", "pending", "ready_to_commit"].includes(binding.state);
function connectionFields() {
  return {appid: $("connect-appid").value.trim(), environment: $("connect-environment").value,
    transport: $("connect-transport").value, intents: Number($("connect-intents").value),
    shard: JSON.parse($("connect-shard").value), enable: $("connect-enable").checked,
    onebot: {enable: $("network-enable").checked, host: $("network-host").value.trim(), port: Number($("network-port").value), writes: $("network-writes").checked}};
}
function connectionPayload(extra = {}) {
  if (!connectionView || $("connect-id").value.trim() !== connectionView.platform_id) throw new Error("请先读取接入目标，再编辑或操作。");
  return {platform_id: connectionView.platform_id, fingerprint: connectionView.fingerprint, csrf: boot.csrf, ...extra};
}
function showConnection(value) {
  connectionView = value;
  $("connect-id").value = value.platform_id;
  for (const key of ["appid", "environment", "transport", "intents"]) $("connect-" + key).value = value.fields[key];
  $("connect-shard").value = JSON.stringify(value.fields.shard); $("connect-enable").checked = value.fields.enable;
  $("connect-secret").value = ""; $("connect-secret-action").value = "keep";
  $("connect-confirm-secret").checked = false; $("connect-confirm-identity").checked = false;
  const network = value.fields.onebot;
  $("network-enable").checked = network.enable; $("network-writes").checked = network.writes;
  $("network-host").value = network.host; $("network-port").value = String(network.port);
  $("network-token").value = ""; $("network-token-action").value = "keep";
  $("network-confirm-token").checked = false; $("network-confirm-writes").checked = false;
  $("network-token-state").textContent = `专用 token ${value.network_token_configured ? "已配置（不回传原值）" : "未配置"}`;
  $("network-gate-state").textContent = boot.flags?.onebot_network_enabled ? "总闸开启；未运行实例需重载" : "总闸关闭";
  $("network-capabilities").textContent = JSON.stringify(value.capabilities || {network_api: {state: "not_loaded"}}, null, 2);
  $("connect-status").textContent = `${value.platform_id} · 凭据${value.credentials_configured ? "已配置" : "未配置"} · ${value.runtime.state} · online=${value.runtime.online} · ${value.reload}。${value.webhook_path || ""}`;
  const failure = value.runtime.failure_details || value.runtime.last_transport_failure;
  if (failure) $("connect-status").textContent += ` 最近故障：${failure.code} / 业务或关闭码 ${failure.business_code ?? "无"} / HTTP ${failure.http_status ?? "无"}`;
  if (value.runtime.message_delivery) $("connect-status").textContent += ` 消息交付：${value.runtime.message_delivery} / ${value.runtime.delivery_error || "无故障"} / 待处理 ${value.runtime.pending_raw ?? "未知"}；保留 ${JSON.stringify(value.runtime.raw_disposition || {})}`;
  if (value.runtime.send_storage_failed) $("connect-status").textContent += " 发送账本写入失败；停止插件并修复存储后重载插件，勿直接重试。";
  $("connect-form").hidden = false;
}
function connectionDirty() {
  if (!connectionView) return false;
  try { return !!Object.keys(changed(connectionView.fields, connectionFields())).length || !!$("connect-secret").value || $("connect-secret-action").value !== "keep" || !!$("network-token").value || $("network-token-action").value !== "keep"; }
  catch { return true; }
}
function showBinding(value) {
  binding = value;
  $("bind-status").textContent = `${value.state} · AppID ${value.appid || "尚未取得"} · 剩余 ${value.expires_in}s / 租约 ${value.lease_seconds}s${value.error ? " · " + value.error : ""}。取到凭据不代表在线。`;
  $("bind-commit").hidden = value.state !== "ready_to_commit";
  $("bind-cancel").hidden = !bindingActive();
  const matrix = value.qr_matrix, canvas = $("bind-qr"); canvas.hidden = !matrix;
  if (matrix) {
    if (!Array.isArray(matrix) || matrix.length > 185 || matrix.some(row => !Array.isArray(row) || row.length !== matrix.length)) throw new Error("二维码数据无效。");
    const size = matrix.length; canvas.width = canvas.height = size * 4;
    const context = canvas.getContext("2d"); context.fillStyle = "#fff"; context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = "#000"; matrix.forEach((row, y) => row.forEach((dark, x) => { if (dark) context.fillRect(x * 4, y * 4, 4, 4); }));
  }
}
function scheduleBinding() {
  window.clearTimeout(bindingTimer);
  if (!bindingActive()) return;
  bindingTimer = window.setTimeout(async () => {
    if (loading) return scheduleBinding();
    await run(async () => {
      try { showBinding(await bridge.apiPost("onboarding/status", {csrf: boot.csrf, platform_id: binding.platform_id, ticket: binding.ticket, renew: true})); }
      catch (error) { window.clearTimeout(bindingTimer); $("bind-status").textContent = `续期失败：${error.message}；服务端租约将自动到期。`; binding = null; $("bind-qr").hidden = true; throw error; }
    });
    scheduleBinding();
  }, 5000);
}
async function cancelBinding() {
  window.clearTimeout(bindingTimer);
  if (bindingActive()) showBinding(await bridge.apiPost("onboarding/cancel", {csrf: boot.csrf, platform_id: binding.platform_id, ticket: binding.ticket}));
}
$("connect-read").onclick = () => run(async () => {
  if ((connectionDirty() || bindingActive()) && !window.confirm("丢弃未保存的连接编辑并取消当前扫码？")) return;
  await cancelBinding();
  showConnection(await bridge.apiGet("connection", {platform_id: $("connect-id").value.trim()}));
});
$("connect-save").onclick = () => run(async () => {
  if (bindingActive()) throw new Error("请先取消扫码，避免覆盖待提交的连接配置。");
  if (!window.confirm("保存到本体平台配置，不自动连接；现有运行代次将失效。继续？")) return;
  const value = connectionPayload({patch: changed(connectionView.fields, connectionFields()), secret_action: $("connect-secret-action").value,
    confirm: true, confirm_secret: $("connect-confirm-secret").checked, confirm_identity: $("connect-confirm-identity").checked,
    network_token_action: $("network-token-action").value, confirm_network_token: $("network-confirm-token").checked, confirm_network_writes: $("network-confirm-writes").checked});
  try {
    if (value.secret_action === "replace") value.secret = $("connect-secret").value;
    else if ($("connect-secret").value) throw new Error("已输入QQ凭据，请选择替换并确认，或清空输入以保留原凭据。");
    if (value.network_token_action === "replace") value.network_token = $("network-token").value;
    else if ($("network-token").value) throw new Error("已输入网络token，请选择替换并确认，或清空输入。");
    showConnection(await bridge.apiPost("connection/save", value));
  } finally { $("connect-secret").value = ""; $("network-token").value = ""; delete value.secret; delete value.network_token; }
});
$("connect-reload").onclick = () => run(async () => {
  if (connectionDirty() || bindingActive()) throw new Error("请先保存连接编辑或取消扫码，再重载已保存配置。");
  if (!window.confirm("停止旧代次并重载已保存配置？启用的实例会连接 QQ 并处理消息，是否继续？")) return;
  showConnection(await bridge.apiPost("connection/reload", connectionPayload({confirm: true})));
});
$("bind-start").onclick = () => run(async () => {
  if (connectionDirty()) throw new Error("请先保存或撤销连接编辑；扫码绑定当前已读取的目标与配置指纹。");
  if (!window.confirm("向固定 QQ 官方域名创建短期扫码任务？不会自动保存凭据、连接或授予管理员权限。")) return;
  showBinding(await bridge.apiPost("onboarding/start", connectionPayload({confirm: true}))); scheduleBinding();
});
$("bind-cancel").onclick = () => run(cancelBinding);
$("bind-commit").onclick = () => run(async () => {
  if (binding?.state !== "ready_to_commit") throw new Error("尚无可提交的已验证凭据。");
  if (!window.confirm("将扫码凭据写入此目标，保存后保持禁用；连接需另行启用和重载。继续？")) return;
  showBinding(await bridge.apiPost("onboarding/commit", connectionPayload({ticket: binding.ticket, commit_handle: binding.commit_handle,
    confirm: true, confirm_secret: $("connect-confirm-secret").checked, confirm_identity: $("connect-confirm-identity").checked})));
  scheduleBinding(); if (binding.result) showConnection(binding.result);
});
$("network-gate").onclick = () => run(async () => {
  if (!window.confirm("切换OneBot网络总闸？关闭将撤销所有监听；开启仍需实例启用并重载。")) return;
  const value = await bridge.apiPost("flags", {csrf: boot.csrf, revision: boot.flags_revision, patch: {onebot_network_enabled: !boot.flags.onebot_network_enabled}, confirm: true});
  boot.flags = value.flags; boot.flags_revision = value.revision;
  $("network-gate-state").textContent = boot.flags.onebot_network_enabled ? "总闸开启；需重载" : "总闸关闭";
});
window.addEventListener("pagehide", () => {
  window.clearTimeout(bindingTimer); $("connect-secret").value = ""; $("network-token").value = "";
  if (bindingActive()) bridge.apiPost("onboarding/cancel", {csrf: boot.csrf, platform_id: binding.platform_id, ticket: binding.ticket}).catch(() => {});
});
$("bind-commit").hidden = true; $("bind-cancel").hidden = true;
await run(async () => {
  if (!bridge) throw new Error("请从 AstrBot Plugin Pages 打开此页；无离线假数据。");
  await bridge.ready(); boot = await bridge.apiGet("bootstrap");
  $("instance").replaceChildren(...boot.instances.map(item => option(item.id, `${item.id} · ${item.appid || "未配置"}`)));
  $("connect-id").value = $("instance").value || "qq_v2";
  showConnection(await bridge.apiGet("connection", {platform_id: $("connect-id").value}));
  await load();
});
