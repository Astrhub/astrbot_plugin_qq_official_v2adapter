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
const bridge = {
  async ready() { ready = true; },
  async apiGet(endpoint) {
    assert(ready); calls.push(['GET', endpoint]);
    if (endpoint === 'bootstrap') return {csrf: 'fixture-csrf', flags_revision: 'fixture-flags', instances: [{id: 'fixture', appid: 'fixture-app'}]};
    if (endpoint === 'config') return structuredClone(state);
    if (endpoint === 'commands') return {nodes: [command], version: 'fixture-catalog'};
    throw new Error(endpoint);
  },
  async apiPost(endpoint, body) {
    calls.push(['POST', endpoint, structuredClone(body)]);
    assert.equal(body.csrf, 'fixture-csrf');
    if (endpoint === 'preview') return {panel: {slots: 2, issues: []}, card: {pages: 1, items: [command], layout: {style: 'plain', show_description: true}}};
    if (endpoint === 'config/mutate') { state.revision++; state.draft.title = body.patch.title || state.draft.title; return structuredClone(state); }
    throw new Error(endpoint);
  }
};
const window = {AstrBotPluginPage: bridge, confirm: () => true};
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
console.log('PAGE: bridge-ready, real endpoint wiring, save/preview split, partial patches, safe text and parameter assistant passed');
