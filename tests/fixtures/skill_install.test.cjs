// Execute the actual rendered page functions, with browser I/O substituted.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const pageScript = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const skillScript = pageScript.slice(
  pageScript.indexOf('function renderSkillInstallButton()'),
  pageScript.indexOf('async function startSourceEvaluation()'),
);
const feedbackScript = pageScript.slice(
  pageScript.indexOf('function showError(message)'),
  pageScript.indexOf('function clearMessages()'),
);

class Element {
  constructor() {
    this.children = [];
    this.style = {};
    this.disabled = false;
    this.value = '';
    const classes = new Set();
    this.classList = {add: name => classes.add(name), remove: name => classes.delete(name), contains: name => classes.has(name)};
  }
  set textContent(text) { this.text = text; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent || '').join(''); }
  replaceChildren() { this.children = []; this.text = ''; }
  append(...children) { this.children.push(...children); }
  close() { this.open = false; }
}

function setup(responses, {modalOpen = true, sidebar = true} = {}) {
  const nodes = new Map([
    'error-banner', 'skill-settings-dialog', 'skill-settings-status',
    'skill-discovered-catalog', 'skill-install-directory', 'skill-discovery-list', 'skill-installations',
  ].map(name => [name, new Element()]));
  if (sidebar) nodes.set('skill-install-status', new Element());
  nodes.get('skill-settings-dialog').open = modalOpen;
  const buttons = [new Element(), new Element()];
  const requests = [];
  const timers = [];
  const opened = [];
  const streams = [];
  const conversations = [];
  const context = vm.createContext({
    EventSource: class {
      constructor(url) { this.url = url; streams.push(this); }
      close() { this.closed = true; }
      emit(installation, watching = true) { this.onmessage({data: JSON.stringify({installation, watching})}); }
    },
    timers, opened, conversations, workspaces: [{id: 'recent', path: '/recent'}],
    setTimeout: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
    persistActiveDraft() {},
    getConversationById: id => conversations.find(row => row.id === id),
    writeBrowserStorage() {}, ACTIVE_WORKSPACE_STORAGE_KEY: 'recent-workspace',
    rememberWorkspaceUse() {}, updateWorkspaceState() {}, renderConversations() {},
    renderMessages() {}, restoreConversationDraft() {},
    ensureConversationLoaded: async id => { opened.push(id); return true; },
    document: {
      getElementById: id => nodes.get(id) || null,
      querySelectorAll: () => buttons,
      createElement: () => new Element(),
      createTextNode: text => ({textContent: text}),
    },
    FormData: class { append() {} },
    fetch: async (url, options) => {
      requests.push({url, options});
      const next = responses.shift();
      assert.notEqual(next, undefined, 'unexpected request');
      if (next instanceof Error) throw next;
      return typeof next === 'function' ? next() : next;
    },
  });
  vm.runInContext(skillScript + feedbackScript, context);
  return {context, nodes, buttons, requests, timers, opened, conversations, streams};
}

const file = {name: 'SKILL.md', webkitRelativePath: 'demo/SKILL.md'};
const response = (data, status = 200) => ({ok: status < 400, status, json: async () => data});
const installed = name => response({installed: true, skill: {name, plan_path: `/home/.tsm-agent/skills/${name}/SKILL.md`}});
const catalog = (...names) => response({skills: names.map(name => ({name, description: 'Demo'})), issues: []});

test('saved files start preparation and open the new recent-workspace conversation when ready', async () => {
  const job = {id: 'install-one', skill_name: 'demo', status: 'starting', summary: '正在检查',
    session_id: 'install-chat', workspace_id: 'recent', workspace_path: '/recent', created_at: '2026-10-22T08:00:00Z'};
  const state = setup([
    response({installed: true, skill: {name: 'demo', plan_path: '/skills/demo/SKILL.md'}, installation: job}),
    catalog('demo'),
  ]);
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.match(state.nodes.get('skill-settings-status').textContent, /文件已保存 demo/);
  assert.doesNotMatch(state.nodes.get('skill-settings-status').textContent, /已安装 demo/);
  assert.match(state.nodes.get('skill-installations').textContent, /正在创建安装会话/);
  assert.equal(state.opened.length, 0);
  assert.equal(state.streams.length, 1);
  assert.match(state.streams[0].url, /install-one\/events$/);
  state.streams[0].emit({...job, status: 'ready', task_id: 'install-task', summary: '无需安装环境', checks: ['SKILL.md 已读取，无前置依赖']}, false);
  assert.match(state.nodes.get('skill-installations').textContent, /安装成功，验证通过/);
  assert.match(state.nodes.get('skill-installations').textContent, /无前置依赖/);
  assert.deepEqual(state.opened, ['install-chat']);
  assert.equal(state.conversations[0].workspaceId, 'recent');
  assert.equal(vm.runInContext('activeConversationId', state.context), 'install-chat');
  assert.equal(state.nodes.get('skill-settings-dialog').open, false);
  assert.equal(state.timers.length, 0);
  assert.equal(state.streams[0].closed, true);
});

test('offline preparation remains pending and the retry action starts a new preparation', async () => {
  const job = {id: 'pending-one', skill_name: 'demo', status: 'pending', summary: '运行时不可用'};
  const state = setup([response({...job, status: 'starting', summary: '正在创建'})]);
  state.context.renderSkillInstallations([job]);
  assert.match(state.nodes.get('skill-installations').textContent, /准备待启动/);
  assert.equal(state.timers.length, 0);
  const retry = state.nodes.get('skill-installations').children[0].children.find(row => row.textContent === '重试准备');
  await retry.onclick();
  assert.match(state.requests[0].url, /pending-one\/retry$/);
  assert.match(state.nodes.get('skill-installations').textContent, /正在创建安装会话/);
  assert.equal(state.streams.length, 1);
});

test('lost progress connection retries without turning saved files into a failed upload', async () => {
  const job = {id: 'active', skill_name: 'demo', status: 'running', summary: '准备中'};
  const state = setup([]);
  state.context.renderSkillInstallations([job]);
  state.streams[0].onerror();
  assert.match(state.nodes.get('skill-settings-status').textContent, /正在重连/);
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
  state.streams[0].emit({...job, status: 'failed', summary: '依赖安装失败'}, false);
  assert.match(state.nodes.get('skill-installations').textContent, /依赖安装失败/);
  assert.equal(state.opened.length, 0);
  assert.equal(state.timers.length, 0);
  assert.equal(state.requests.length, 0);
});

test('install two Skills in settings, update catalog and preserve unsaved directory choices', async () => {
  const state = setup([installed('one'), catalog('one'), installed('two'), catalog('one', 'two')], {sidebar: false});
  vm.runInContext("skillSettingsDirectories = ['/unsaved/skills']", state.context);
  const input = {value: 'selected'};
  await state.context.uploadSkillDirectoryAndInstall([file], input);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 one/);
  assert.equal(input.value, '');
  input.value = 'selected-again';
  await state.context.uploadSkillDirectoryAndInstall([file], input);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 two/);
  assert.match(state.nodes.get('skill-discovered-catalog').textContent, /已发现 2 个 Skill/);
  assert.equal(vm.runInContext('skillSettingsDirectories[0]', state.context), '/unsaved/skills');
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
  assert.ok(state.buttons.every(button => !button.disabled));
  assert.equal(input.value, '');
});

test('sidebar install works without a settings refresh', async () => {
  const state = setup([installed('demo')], {modalOpen: false});
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.match(state.nodes.get('skill-install-status').textContent, /已安装 demo/);
  assert.equal(state.requests.length, 1);
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
});

test('catalog refresh failure preserves success and displays a refresh warning', async () => {
  const state = setup([installed('demo'), new Error('offline')]);
  await state.context.uploadSkillDirectoryAndInstall([file]);
  const message = state.nodes.get('skill-settings-status').textContent;
  assert.match(message, /已安装 demo/);
  assert.match(message, /目录列表刷新失败/);
  assert.ok(message.includes('\n'), 'warnings must use real newlines');
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
});

test('server refresh warning remains visible on a successful installation', async () => {
  const result = response({installed: true, skill: {name: 'demo', plan_path: '/skills/demo/SKILL.md'}, warnings: ['runtime catalog unavailable']});
  const state = setup([result, catalog('demo')]);
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 demo/);
  assert.match(state.nodes.get('skill-settings-status').textContent, /runtime catalog unavailable/);
});

test('changed duplicate is reported; retry clears the stale error and can succeed', async () => {
  const state = setup([response({detail: '同名 Skill 内容不同'}, 409), installed('demo'), catalog('demo')]);
  const input = {value: 'selected'};
  await state.context.uploadSkillDirectoryAndInstall([file], input);
  assert.match(state.nodes.get('skill-settings-status').textContent, /同名 Skill 内容不同/);
  assert.equal(input.value, '');
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), true);
  await state.context.uploadSkillDirectoryAndInstall([file], input);
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 demo/);
});

test('non-JSON, malformed success and interrupted upload each recover the controls', async () => {
  for (const result of [
    {ok: false, status: 500, json: async () => {throw new SyntaxError('HTML response');}},
    response({installed: true}),
    new Error('network disconnected'),
  ]) {
    const state = setup([result]);
    const input = {value: 'selected'};
    await state.context.uploadSkillDirectoryAndInstall([file], input);
    assert.equal(state.nodes.get('error-banner').classList.contains('visible'), true);
    assert.doesNotMatch(state.nodes.get('error-banner').textContent, /ReferenceError|is not defined|Cannot read/);
    assert.equal(input.value, '');
    assert.ok(state.buttons.every(button => !button.disabled));
    assert.equal(vm.runInContext('skillInstallInFlight', state.context), false);
  }
});

test('cancelled picker does not send an upload; Finder metadata is filtered', async () => {
  const state = setup([installed('demo')], {modalOpen: false});
  await state.context.uploadSkillDirectoryAndInstall([]);
  assert.equal(state.requests.length, 0);
  await state.context.uploadSkillDirectoryAndInstall([file, {name: '.DS_Store', webkitRelativePath: 'demo/.DS_Store'}]);
  assert.equal(state.requests.length, 1);
});

test('parallel clicks submit only one upload and release the busy state', async () => {
  let finish;
  const state = setup([() => new Promise(resolve => {finish = resolve;}), catalog('demo')]);
  const first = state.context.uploadSkillDirectoryAndInstall([file]);
  assert.ok(state.buttons.every(button => button.disabled));
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.equal(state.requests.length, 1);
  finish(installed('demo'));
  await first;
  assert.ok(state.buttons.every(button => !button.disabled));
});
