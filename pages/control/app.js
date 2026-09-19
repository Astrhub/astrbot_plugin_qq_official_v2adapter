const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
let boot, current, catalog, draft, loadedScene, page = 0, pageCount = 1, loading = false;
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
      patch[key] = old[key] && next[key] && typeof old[key] === "object" && typeof next[key] === "object" && !Array.isArray(next[key])
        ? changed(old[key], next[key]) : next[key];
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
function readDraft() {
  draft.title = $("title").value;
  draft.scene_overrides = JSON.parse($("scene-overrides").value);
  draft.node_overrides = JSON.parse($("node-overrides").value);
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
  if (!id) return report("请先在 AstrBot 平台管理中添加 QQ 官方 V2，填写一次 AppID/secret；页面不保存凭据副本。");
  current = await bridge.apiGet("config", {platform_id: id});
  catalog = await bridge.apiGet("commands", {platform_id: id, scene: scene()});
  loadedScene = scene();
  $("card-preview").replaceChildren();
  draft = clone(current.draft);
  $("title").value = draft.title; $("mode").value = selection().mode;
  $("scene-overrides").value = JSON.stringify(draft.scene_overrides, null, 2);
  $("node-overrides").value = JSON.stringify(draft.node_overrides, null, 2);
  $("versions").replaceChildren(...current.versions.map(v => option(v, `版本 ${v}`)));
  $("versions-state").textContent = `草稿 ${current.revision} / 已应用 ${current.applied_revision} / 远端 ${current.remote_state}`;
  $("connection").textContent = `${current.platform_id} · AppID ${current.identity.appid} · ${current.identity.environment} · 凭据${current.credentials_configured ? "已配置" : "未配置"} · ${current.runtime_state}。连接字段请在本体平台管理修改并重载。`;
  $("preview-output").textContent = "尚未预览；不发送真实消息。";
  $("help").replaceChildren(); $("command-copy").value = "";
  renderLayouts(); renderCatalog(); renderSelected(); renderNodes();
  $("editor").hidden = false; report("已读取真实目录与本地配置。远端功能尚未实现。");
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
  await bridge.apiPost("config/mutate", payload({operation, patch, confirm: operation === "apply", restore_revision: Number($("versions").value)}));
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
  await bridge.apiPost("flags", {csrf: boot.csrf, revision: boot.flags_revision, patch: {webui_enabled: false}, confirm: true});
  $("editor").hidden = true; report("管理 API 已关闭，请从本体插件设置恢复。");
});
await run(async () => {
  if (!bridge) throw new Error("请从 AstrBot Plugin Pages 打开此页；无离线假数据。");
  await bridge.ready(); boot = await bridge.apiGet("bootstrap");
  $("instance").replaceChildren(...boot.instances.map(item => option(item.id, `${item.id} · ${item.appid || "未配置"}`)));
  await load();
});
