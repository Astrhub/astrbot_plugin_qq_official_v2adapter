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
let flags = {remote_menu_sync: false, webui_enabled: true};
const timers = new Map(), events = new Map(); let timerId = 0, failSave = false;
const connection = {platform_id: 'fixture', fingerprint: 'connection-fingerprint', exists: true,
  fields: {appid: 'fixture-app', environment: 'production', transport: 'websocket', intents: 33554432, shard: [0, 1], enable: false},
  credentials_configured: true, runtime: {state: 'configured', online: false}, reload: 'not_requested'};
const binding = {ticket: 'fixture-ticket', platform_id: 'fixture', state: 'pending', expires_in: 180, lease_seconds: 30, qr_matrix: [[true, false], [false, true]]};
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
    if (endpoint === 'config/mutate') { if (body.operation === 'apply') state.applied_revision = state.revision; else state.revision++; state.draft.title = body.patch.title || state.draft.title; return structuredClone(state); }
    if (endpoint === 'flags') { Object.assign(flags, body.patch); return {flags: structuredClone(flags), revision: 'fixture-flags'}; }
    if (endpoint === 'panels/plan') return {fingerprint: 'panel-snapshot', issues: [], payload: {scope: body.scene}};
    if (['panels/enable', 'panels/sync', 'panels/disable'].includes(endpoint)) return {state: endpoint.endsWith('disable') ? 'stopped' : 'synced'};
    if (endpoint === 'inbox/discard') { assert.equal(body.confirm, true); assert.equal(body.entries.length, 1); assert.equal(body.entries[0].confirmation, 'fixture-confirmation'); retainedRows = []; return {discarded: 1, counts: {pending: 1}}; }
    if (endpoint === 'connection/save') { if (failSave) throw new Error('fixture conflict'); Object.assign(connection.fields, body.patch); return structuredClone(connection); }
    if (endpoint === 'connection/reload') { connection.runtime.state = 'connecting'; return structuredClone(connection); }
    if (endpoint === 'onboarding/start') return structuredClone(binding);
    if (endpoint === 'onboarding/status') return {...binding, state: 'ready_to_commit', qr_matrix: null, commit_handle: 'fixture-handle'};
    if (endpoint === 'onboarding/commit') return {...binding, state: 'configured', qr_matrix: null, result: structuredClone(connection)};
    if (endpoint === 'onboarding/cancel') return {...binding, state: 'cancelled', qr_matrix: null};
    throw new Error(endpoint);
  }
};
const window = {AstrBotPluginPage: bridge, confirm: () => true, addEventListener: (name, fn) => events.set(name, fn),
  setTimeout: (fn, delay) => { assert.equal(delay, 5000); timers.set(++timerId, fn); return timerId; }, clearTimeout: id => timers.delete(id)};
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
await nodes.get('bind-start').onclick();
assert.equal(nodes.get('bind-qr').hidden, false); assert.equal(timers.size, 1);
await [...timers.values()][0]();
assert.equal(calls.at(-1)[1], 'onboarding/status'); assert.equal(calls.at(-1)[2].renew, true);
assert.equal(nodes.get('bind-commit').hidden, false);
nodes.get('connect-confirm-secret').checked = true; nodes.get('connect-confirm-identity').checked = true;
await nodes.get('bind-commit').onclick();
assert.equal(calls.at(-1)[2].commit_handle, 'fixture-handle'); assert.equal(timers.size, 0);
assert.equal(nodes.get('connect-secret').value, '');
await nodes.get('bind-start').onclick(); events.get('pagehide')();
assert.equal(timers.size, 0); assert.equal(calls.at(-1)[1], 'onboarding/cancel');
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
console.log('PAGE: bridge-ready, real endpoint wiring, save/preview split, partial patches, safe text and parameter assistant passed');
