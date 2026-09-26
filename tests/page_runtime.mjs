import assert from 'node:assert/strict';
import fs from 'node:fs';
const defaults = JSON.parse(fs.readFileSync(0, 'utf8'));
const source = fs.readFileSync('/plugin/pages/control/app.js', 'utf8');
class Element {
  constructor(tag = 'div') { this.tag = tag; this.children = []; this.value = ''; this.textContent = ''; this.disabled = false; this.hidden = false; this.checked = false; }
  set innerHTML(_) { throw new Error('Untrusted markup must not be inserted'); }
  append(...nodes) { this.children.push(...nodes); if (this.tag === 'select' && !this.value && nodes.length) this.value = String(nodes[0].value); }
  replaceChildren(...nodes) { this.children = []; if (this.tag === 'select') this.value = ''; this.append(...nodes); }
  setAttribute() {}
  focus() {}
  select() {}
  getContext() { return {fillRect() {}, fillStyle: ''}; }
}
const ids = [...fs.readFileSync('/plugin/pages/control/index.html', 'utf8').matchAll(/<([\w-]+)[^>]*\bid="([^"]+)"/g)];
const nodes = new Map(ids.map(([, tag, id]) => [id, new Element(tag)]));
nodes.get('scene').value = 'group'; nodes.get('layer').value = 'home';
const document = {getElementById: (id) => { assert(nodes.has(id), id); return nodes.get(id); },
  createElement: (tag) => new Element(tag), querySelectorAll: () => [...nodes.values()]};
const command = {id: 'fixture_handler', name: 'fixture', command: '/fixture', usage: '/fixture <input>', plugin: '<script>not-html</script>', system: true,
  menu_entry: false, enabled: true, parent: null, group: false, description: '<img onerror=evil()>', aliases: ['fixture'],
  permission: ['admin'], reasons: [], conditions: [], compatibility: 'fixture only', parameters: [{name: 'input', required: true, type: 'str', default_visible: false}]};
let calls = [], ready = false;
let state = {platform_id: 'fixture', revision: 0, applied_revision: 0, remote_state: 'not_implemented', fingerprint: 'fixture-fingerprint',
  draft: structuredClone(defaults), versions: [], identity: {appid: 'fixture-app', environment: 'sandbox'}, credentials_configured: true, runtime_state: 'transport_not_ready'};
let flags = {remote_menu_sync: false, webui_enabled: true, onebot_network_enabled: false};
const events = new Map(), timers = new Map(); let failSave = false, timerId = 0, lastDelay = 0, deferQr = false;
const binding = {ticket: 'fixture-ticket', platform_id: 'fixture', state: 'pending', hint: '请用手机 QQ 扫码，并在 QQ 里确认授权。',
  expires_in: 180, lease_seconds: 30, qr_matrix: [[true, false], [false, true]], appid: null, commit_handle: null};
const connection = {platform_id: 'fixture', fingerprint: 'connection-fingerprint', exists: true,
  fields: {appid: 'fixture-app', is_sandbox: false, type: 'qq_official_v2', intents: 33554432, shard_mode: 'auto', shard: [0, 1], enable: false, onebot: {host: '127.0.0.1', port: 5700, writes: false, enable: false}},
  credentials_configured: true, runtime: {state: 'configured', online: false}, reload: 'not_requested'};
let retainedRows = [{receipt: 7, version: 'a'.repeat(64), confirmation: 'fixture-confirmation', event_type: '<script>plain text</script>', state: 'extension', reason: 'unsupported', received_at: 1800000000, size: 100}];
const bridge = {
  async ready() { ready = true; },
  async apiGet(endpoint, params = {}) {
    assert(ready); calls.push(['GET', endpoint, structuredClone(params)]);
    if (endpoint === 'bootstrap') return {csrf: 'fixture-csrf', flags: structuredClone(flags), flags_revision: 'fixture-flags', instances: [{id: 'fixture', appid: 'fixture-app'}]};
    if (endpoint === 'config') return structuredClone(state);
    if (endpoint === 'commands') return {nodes: [command], version: 'fixture-catalog'};
    if (endpoint === 'connection') return structuredClone(connection);
    if (endpoint === 'inbox/retained') return {platform_id: params.platform_id, fingerprint: state.fingerprint, expires: Math.floor(Date.now() / 1000) + 60, entries: structuredClone(retainedRows), counts: {extension: retainedRows.length, pending: 1}};
    throw new Error(endpoint);
  },
  async apiPost(endpoint, body) {
    calls.push(['POST', endpoint, structuredClone(body)]);
    assert.equal(body.csrf, 'fixture-csrf');
    if (endpoint === 'preview') return {panel: {slots: 2, issues: []}, card: {pages: 1, items: [command], layout: {style: 'plain', show_description: true}}};
    if (endpoint === 'config/mutate') { if (body.operation === 'apply') state.applied_revision = state.revision; else state.revision++; state.draft.title = body.patch.title || state.draft.title; if (body.patch.extensions) Object.assign(state.draft.extensions, body.patch.extensions); return structuredClone(state); }
    if (endpoint === 'flags') { Object.assign(flags, body.patch); return {flags: structuredClone(flags), revision: 'fixture-flags'}; }
    if (endpoint === 'panels/plan') return {fingerprint: 'panel-snapshot', issues: [], payload: {scope: body.scene}};
    if (['panels/enable', 'panels/sync', 'panels/disable'].includes(endpoint)) return {state: endpoint.endsWith('disable') ? 'stopped' : 'synced'};
    if (endpoint === 'inbox/discard') { assert.equal(body.confirm, true); assert.equal(body.entries.length, 1); assert.equal(body.entries[0].confirmation, 'fixture-confirmation'); retainedRows = []; return {discarded: 1, counts: {pending: 1}}; }
    if (endpoint === 'connection/save') { if (failSave) throw new Error('fixture conflict'); const {onebot, ...rest} = body.patch; Object.assign(connection.fields, rest); Object.assign(connection.fields.onebot, onebot || {}); if (body.network_token_action !== 'keep') connection.network_token_configured = body.network_token_action === 'replace'; return structuredClone(connection); }
    if (endpoint === 'connection/reload') { connection.runtime.state = 'connecting'; return structuredClone(connection); }
    if (endpoint === 'onboarding/start') return deferQr ? {...binding, state: 'creating', hint: '正在向 QQ 申请二维码。不需要事先填写 AppSecret。', qr_matrix: null} : structuredClone(binding);
    if (endpoint === 'onboarding/status') {
      if (deferQr) { deferQr = false; return structuredClone(binding); }
      return {...binding, state: 'ready_to_commit', hint: '已拿到 AppID 和 AppSecret。保存后机器人保持关闭。', qr_matrix: null, appid: 'new-app', commit_handle: 'fixture-handle'};
    }
    if (endpoint === 'onboarding/commit') return {...binding, state: 'configured', hint: '凭据已保存，机器人仍是关闭的。', qr_matrix: null, appid: 'new-app', result: structuredClone(connection)};
    if (endpoint === 'onboarding/cancel') return {...binding, state: 'cancelled', hint: '已取消，没有写入凭据。', qr_matrix: null};
    throw new Error(endpoint);
  }
};
const window = {AstrBotPluginPage: bridge, confirm: () => true, addEventListener: (name, fn) => events.set(name, fn),
  setTimeout: (fn, delay) => { assert.ok(delay === 400 || delay === 5000, String(delay)); lastDelay = delay; timers.set(++timerId, fn); return timerId; },
  clearTimeout: id => timers.delete(id)};
const AsyncFunction = Object.getPrototypeOf(async function() {}).constructor;
await new AsyncFunction('window', 'document', source)(window, document);
assert.equal(nodes.get('editor').hidden, false);
assert(calls.every(c => c[0] === 'GET'), 'loading must not save/publish');
nodes.get('title').value = 'local test';
await nodes.get('preview').onclick();
assert.equal(calls.at(-1)[1], 'preview');
assert.deepEqual(calls.at(-1)[2].patch, {title: 'local test'});
await nodes.get('apply').onclick();
assert(nodes.get('status').textContent.includes('先保存'));
assert.equal(calls.at(-1)[1], 'preview', 'unsaved draft must not be applied');
await nodes.get('save').onclick();
const saved = calls.find(c => c[1] === 'config/mutate');
assert.equal(saved[2].operation, 'save');
assert.deepEqual(saved[2].patch, {title: 'local test'});
assert.equal(state.draft.title, 'local test');
nodes.get('ext-keyboard').checked = true; nodes.get('ext-media-max').value = '1000000';
await nodes.get('preview').onclick();
assert.deepEqual(calls.at(-1)[2].patch, {extensions: {keyboard_enabled: true, media_max_bytes: 1000000}});
await nodes.get('apply').onclick(); assert(nodes.get('status').textContent.includes('先保存'));
await nodes.get('save').onclick();
assert.equal(state.draft.extensions.keyboard_enabled, true);
assert.equal(nodes.get('ext-media-max').value, '1000000');
assert.equal(state.draft.extensions.management_writes, false);
// Description is plain text; metadata does not run as HTML.
const group = nodes.get('catalog').children[0];
const row = group.children[1]; row.children[1].onclick();
assert(nodes.get('help').children.some(n => n.textContent === '<img onerror=evil()>'));
const input = nodes.get('help').children.find(n => n.tag === 'label').children[0];
const build = nodes.get('help').children.at(-1);
input.value = 'two words'; build.onclick();
assert(nodes.get('status').textContent.includes('空白'));
input.value = 'argument'; build.onclick();
assert.equal(nodes.get('command-copy').value, '/fixture argument');
assert(calls.every(c => !c[1].includes('send') && !c[1].includes('publish')));
assert.equal(nodes.get('connect-secret').value, '');
nodes.get('connect-intents').value = '1';
await nodes.get('connect-save').onclick();
assert.deepEqual(calls.at(-1)[2].patch, {intents: 1});
assert.equal(calls.at(-1)[2].secret_action, 'keep'); assert(!('secret' in calls.at(-1)[2]));
assert(!calls.some(c => c[1] === 'connection/reload'), 'save must not reload');
nodes.get('connect-secret-action').value = 'replace'; nodes.get('connect-secret').value = 'fixture-new-secret';
nodes.get('connect-confirm-secret').checked = true; failSave = true;
await nodes.get('connect-save').onclick(); assert.equal(nodes.get('connect-secret').value, '');
assert(nodes.get('status').textContent.includes('fixture conflict')); failSave = false;
await nodes.get('connect-read').onclick();
const beforePanels = calls.length;
await nodes.get('panel-enable').onclick();
assert.equal(calls.length, beforePanels, 'publishing needs the independent switch');
await nodes.get('apply').onclick();
await nodes.get('panel-plan').onclick();
assert.equal(calls.at(-1)[1], 'panels/plan');
await nodes.get('remote-gate').onclick();
nodes.get('panel-menu-only').checked = true;
const afterGate = calls.length; await nodes.get('panel-enable').onclick();
assert.equal(calls.length, afterGate, 'scope change invalidates confirmation');
await nodes.get('panel-plan').onclick();
await nodes.get('panel-enable').onclick();
assert.equal(calls.at(-1)[1], 'panels/enable');
assert.equal(calls.at(-1)[2].plan_fingerprint, 'panel-snapshot');
assert.equal(calls.at(-1)[2].menu_only, true); assert.equal(calls.at(-1)[2].confirm, true);
await nodes.get('panel-disable').onclick();
assert.equal(calls.at(-1)[1], 'panels/disable');
const beforeRetained = calls.length;
await nodes.get('retained-discard').onclick(); assert.equal(calls.length, beforeRetained, 'discard needs a preview');
await nodes.get('retained-read').onclick();
assert.equal(calls.at(-1)[1], 'inbox/retained'); assert.equal(calls.at(-1)[2].platform_id, 'fixture');
const retainedCheck = nodes.get('retained-list').children[0].children[0];
assert.equal(retainedCheck.checked, false);
const beforeSelect = calls.length; await nodes.get('retained-discard').onclick(); assert.equal(calls.length, beforeSelect);
retainedCheck.checked = true; retainedCheck.onchange();
nodes.get('instance').value = 'another'; await nodes.get('retained-discard').onclick(); assert.equal(calls.length, beforeSelect, 'discard is bound to the displayed instance');
nodes.get('instance').value = 'fixture'; window.confirm = () => false;
await nodes.get('retained-discard').onclick(); assert.equal(calls.length, beforeSelect, 'discard needs explicit confirmation');
retainedCheck.checked = false; retainedCheck.onchange(); await nodes.get('retained-select-all').onclick();
assert.equal(retainedCheck.checked, true); assert.equal(calls.length, beforeSelect, 'selection does not discard');
window.confirm = () => true; await nodes.get('retained-discard').onclick();
const discard = calls.find(c => c[1] === 'inbox/discard'); assert.equal(discard[2].platform_id, 'fixture'); assert.equal(discard[2].fingerprint, 'fixture-fingerprint');
assert.equal(nodes.get('retained-list').children.length, 0); assert(nodes.get('status').textContent.includes('已明确丢弃 1'));
assert.equal(nodes.get('network-enable').checked, false); assert.equal(nodes.get('network-writes').checked, false);
assert.equal(nodes.get('network-host').value, '127.0.0.1'); assert.equal(nodes.get('network-token').value, '');
nodes.get('network-enable').checked = true; nodes.get('network-writes').checked = true; nodes.get('network-port').value = '5799';
nodes.get('network-token-action').value = 'replace'; nodes.get('network-token').value = 'synthetic-network-token-only';
nodes.get('network-confirm-token').checked = true; nodes.get('network-confirm-writes').checked = true;
const beforeNetworkSave = calls.length; await nodes.get('connect-save').onclick();
const networkSaved = calls.at(-1); assert.equal(networkSaved[1], 'connection/save');
assert.deepEqual(networkSaved[2].patch, {onebot: {enable: true, writes: true, port: 5799}});
assert.equal(networkSaved[2].network_token, 'synthetic-network-token-only'); assert.equal(networkSaved[2].confirm_network_token, true);
assert.equal(networkSaved[2].confirm_network_writes, true); assert.equal(nodes.get('network-token').value, '');
assert(!calls.slice(beforeNetworkSave).some(c => c[1] === 'connection/reload'));
assert(nodes.get('network-token-state').textContent.includes('已配置'));
assert(!nodes.get('network-capabilities').textContent.includes('synthetic-network-token-only'));
nodes.get('network-token-action').value = 'replace'; nodes.get('network-token').value = 'synthetic-failing-token'; failSave = true;
await nodes.get('connect-save').onclick(); assert.equal(nodes.get('network-token').value, ''); failSave = false;
await nodes.get('connect-read').onclick();
await nodes.get('network-gate').onclick(); assert.equal(calls.at(-1)[1], 'flags'); assert.equal(calls.at(-1)[2].patch.onebot_network_enabled, true);
const beforeHide = calls.length; nodes.get('network-token').value = 'synthetic-ephemeral-token'; events.get('pagehide')();
assert.equal(nodes.get('network-token').value, ''); assert.equal(calls.length, beforeHide, 'page close must not disable listener or rotate token');
await nodes.get('bind-start').onclick();
assert.equal(calls.at(-1)[1], 'onboarding/start'); assert.equal(calls.at(-1)[2].confirm, true);
assert.equal(nodes.get('bind-qr').hidden, false); assert.equal(lastDelay, 5000);
await [...timers.values()][0]();
assert.equal(calls.at(-1)[1], 'onboarding/status'); assert.equal(calls.at(-1)[2].renew, true);
assert.equal(nodes.get('bind-save').hidden, false); assert.equal(nodes.get('bind-qr').hidden, true);
await nodes.get('bind-save').onclick();
assert.equal(calls.at(-1)[1], 'onboarding/commit'); assert.equal(calls.at(-1)[2].commit_handle, 'fixture-handle');
assert.equal(calls.at(-1)[2].confirm_secret, true); assert.equal(timers.size, 0);
await nodes.get('bind-start').onclick(); events.get('pagehide')();
assert.equal(calls.at(-1)[1], 'onboarding/cancel');
deferQr = true; await nodes.get('bind-start').onclick();
assert.equal(nodes.get('bind-qr').hidden, true); assert.equal(lastDelay, 400);
await [...timers.values()][0]();
assert.equal(nodes.get('bind-qr').hidden, false); assert.equal(lastDelay, 5000);
await nodes.get('bind-cancel').onclick();
assert.equal(nodes.get('manual-shard').hidden, true);
assert.equal(nodes.get('ws-settings').hidden, false);
nodes.get('connect-shard-mode').value = 'manual'; nodes.get('connect-shard-mode').onchange();
assert.equal(nodes.get('manual-shard').hidden, false);
nodes.get('connect-shard-index').value = '1'; nodes.get('connect-shard-count').value = '3';
await nodes.get('connect-save').onclick();
assert.deepEqual(calls.at(-1)[2].patch, {shard_mode: 'manual', shard: [1, 3]});
nodes.get('connect-type').value = 'qq_official_v2_webhook'; nodes.get('connect-type').onchange();
assert.equal(nodes.get('ws-settings').hidden, true);
nodes.get('connect-sandbox').checked = true; nodes.get('connect-confirm-identity').checked = true;
await nodes.get('connect-save').onclick();
assert.deepEqual(calls.at(-1)[2].patch, {type: 'qq_official_v2_webhook', is_sandbox: true, shard: [0, 1]});
assert.equal(nodes.get('connect-intents').value, '1');
assert.equal(nodes.get('connect-secret').value, '');
nodes.get('connect-type').value = 'qq_official_v2'; nodes.get('connect-type').onchange();
nodes.get('connect-shard-mode').value = 'auto'; nodes.get('connect-shard-mode').onchange();
assert.equal(nodes.get('manual-shard').hidden, true);
await nodes.get('connect-save').onclick();
assert.deepEqual(calls.at(-1)[2].patch, {type: 'qq_official_v2', shard_mode: 'auto'});
connection.runtime.gateway_group = {mode: 'auto', recommended: 3, planned: 3, connected: 2, state: 'degraded', shards: [
  {index: 0, count: 3, state: 'online'}, {index: 1, count: 3, state: 'backoff', failure: {code: 'gateway_closed'}}]};
await nodes.get('connect-read').onclick();
assert(nodes.get('shard-status').textContent.includes('QQ 建议 3 / 已计划 3 / 已连接 2 · degraded'));
assert(nodes.get('shard-status').textContent.includes('[1,3] backoff / gateway_closed'));
const beforeCleanReload = calls.length; await nodes.get('connect-reload').onclick();
assert.equal(calls.at(-1)[1], 'connection/reload'); assert.equal(calls.length, beforeCleanReload + 1);
console.log('PAGE: bridge-ready, real endpoint wiring, save/preview split, partial patches, safe text and parameter assistant passed');
