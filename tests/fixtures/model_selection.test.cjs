const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function setup({save, profileOverrides = {}} = {}) {
  class Select {
    constructor() { this.children = []; this.value = ''; this.disabled = false; }
    replaceChildren() { this.children = []; }
    appendChild(item) { this.children.push(item); }
    get options() { return this.children; }
    get selectedOptions() { return this.children.filter(item => item.value === this.value); }
  }
  const select = new Select(), effort = new Select();
  effort.children = ['', 'none', 'low', 'high'].map(value => ({value}));
  let current = {id: 'new', messages: []};
  const saves = [], errors = [];
  const profiles = [
    {id: 'a', label: 'Model A', protocol: 'openai_chat', enabled: true, reasoning_efforts: ['none', 'high']},
    {id: 'b', label: 'Model B', protocol: 'anthropic_messages', enabled: true, reasoning_efforts: []},
    {id: 'image', label: 'Image', protocol: 'openai_chat', enabled: false, disabled_reason: 'not a text agent'},
  ];
  profiles.forEach(profile => Object.assign(profile, profileOverrides[profile.id] || {}));
  const context = vm.createContext({console, document: {
    getElementById: id => id === 'model-profile' ? select : effort,
    createElement: () => ({}),
  }, getActiveConversation: () => current,
    showError: error => errors.push(error),
    fetch: async (url, options) => {
      if (url === '/model-profiles') return {ok: true, json: async () => ({default_profile: 'a', profiles})};
      assert.match(url, /^\/sessions\/[^/]+\/model-profile$/);
      const body = JSON.parse(options.body); saves.push(body.model_profile_id);
      return save ? save(url, body) : {ok: true, json: async () => body};
    },
  });
  vm.runInContext(page.slice(page.indexOf('let modelProfiles = [];'), page.indexOf('function renderConversations()')), context);
  return {context, select, effort, current, saves, errors, switchTo: value => { current = value; context.renderModelSelector(); }};
}

test('profile catalog loads without exposing configuration and disabled options remain disabled', async () => {
  const s = setup();
  await s.context.loadModelProfiles();
  assert.equal(s.select.value, 'a');
  assert.equal(s.select.options[0].textContent, 'Model A');
  assert.equal(s.select.options[1].textContent, 'Model B');
  assert.equal(s.select.options[2].disabled, true);
  s.select.value = 'b'; await s.select.onchange();
  assert.equal(s.current.modelProfileId, 'b');
  assert.ok(s.effort.options.filter(option => option.value).every(option => option.disabled));
});

test('unverified and limited capabilities do not disable a configured model', async () => {
  const s = setup({profileOverrides: {
    a: {status: 'unverified'}, b: {status: 'limited', status_detail: 'Text works; tool route requires repair'},
  }});
  await s.context.loadModelProfiles();
  assert.equal(s.select.options[0].textContent, 'Model A · 待验证');
  assert.equal(s.select.options[1].textContent, 'Model B · 部分能力受限');
  assert.equal(s.select.options[1].disabled, false);
  assert.match(s.select.options[1].title, /Text works/);
  assert.equal(s.select.options[2].textContent, 'Image · 未启用');
});

test('a used session can switch models and a different conversation keeps its choice', async () => {
  const s = setup(); await s.context.loadModelProfiles();
  const existing = {id: 'existing', modelProfileId: 'b', latestTaskId: 'running', messages: [{role: 'user', content: 'old'}]};
  s.switchTo(existing);
  assert.equal(s.select.value, 'b');
  assert.equal(s.select.disabled, false);
  s.select.value = 'a'; await s.select.onchange();
  assert.equal(existing.modelProfileId, 'a');
  assert.deepEqual(s.saves, ['a']);
  s.switchTo({modelProfileId: 'a', messages: []});
  assert.equal(s.select.disabled, false);
  assert.equal(s.select.value, 'a');
  s.effort.value = 'low'; s.context.updateReasoningOptions();
  assert.equal(s.effort.value, '');
});

test('removed profile is visible as unavailable rather than silently selecting a replacement', async () => {
  const s = setup(); await s.context.loadModelProfiles();
  s.switchTo({modelProfileId: 'removed', latestTaskId: 'old', messages: []});
  assert.equal(s.select.value, 'removed');
  assert.equal(s.select.options.at(-1).disabled, true);
  assert.equal(s.select.disabled, false);
});

test('rapid changes save in order and leave the last choice selected', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const s = setup({save: async (_url, body) => {
    if (body.model_profile_id === 'b') await gate;
    return {ok: true, json: async () => body};
  }});
  await s.context.loadModelProfiles();
  s.select.value = 'b'; const first = s.select.onchange();
  s.select.value = 'a'; const last = s.select.onchange();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(s.saves, ['b']);
  release(); await Promise.all([first, last]);
  assert.deepEqual(s.saves, ['b', 'a']);
  assert.equal(s.current.confirmedModelProfileId, 'a');
  assert.equal(s.select.value, 'a');
});

test('a rejected switch restores the confirmed selection and shows the error', async () => {
  const s = setup({save: async () => ({ok: false, status: 409, json: async () => ({detail: 'profile disabled'})})});
  await s.context.loadModelProfiles();
  s.select.value = 'b'; await s.select.onchange();
  assert.equal(s.current.modelProfileId, 'a');
  assert.equal(s.select.value, 'a');
  assert.deepEqual(s.errors, ['profile disabled']);
});

test('multiple rejected changes restore the confirmed model rather than an unsaved intermediate choice', async () => {
  const s = setup({save: async () => ({ok: false, status: 409, json: async () => ({detail: 'not saved'})})});
  await s.context.loadModelProfiles();
  s.select.value = 'b'; const first = s.select.onchange();
  s.select.value = 'a'; const last = s.select.onchange();
  await Promise.all([first, last]);
  assert.equal(s.current.modelProfileId, 'a');
  assert.equal(s.select.value, 'a');
});

test('reasoning preference is restored per session and service default clears it', async () => {
  const s = setup(); await s.context.loadModelProfiles();
  const first = {modelProfileId: 'a', reasoningEffort: 'high', messages: []};
  s.switchTo(first);
  assert.equal(s.effort.value, 'high');
  s.switchTo({modelProfileId: 'a', messages: []});
  assert.equal(s.effort.value, '');
  s.switchTo(first);
  s.effort.value = ''; s.effort.onchange();
  assert.equal(first.reasoningEffort, null);
  s.switchTo(first);
  assert.equal(s.effort.value, '');
});
