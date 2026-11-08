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
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.disabled = false;
    this.value = '';
    const classes = new Set();
    this.classList = {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        if (on) classes.add(name); else classes.delete(name);
      },
    };
    this.setAttribute = (name, value) => { this.attributes[name] = String(value); };
    this.getAttribute = name => (name in this.attributes ? this.attributes[name] : null);
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
    'skill-discovered-catalog', 'skill-install-directory', 'skill-discovery-list', 'skill-installations', 'skill-builtin-catalog',
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
      createElement: tag => new Element(tag),
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
const installed = name => response({installed: true, loadable: true, skill: {name, plan_path: `/home/.tsm-agent/skills/${name}/SKILL.md`}});
const catalog = (...names) => response({skills: names.map(name => ({name, description: 'Demo'})), issues: []});

// Settings cards nest controls inside header/footer, so tests locate them by tag.
const findElement = (node, tag) => {
  for (const child of node.children || []) {
    if (child.tag === tag) return child;
    const found = findElement(child, tag);
    if (found) return found;
  }
  return null;
};

test('built-in workflow controls preserve source labels and persist disablement with directory settings', async () => {
  const data = {install_directory: '/home/skills', discovery_directories: ['/home/skills'],
    builtin_skills: [{name: 'verify-change'}], disabled_builtin_skills: [],
    skills: [{name: 'verify-change', description: 'Verification', source: 'builtin', plan_path: 'builtin://verify-change/SKILL.md'}]};
  const s = setup([response({...data, skills: [], disabled_builtin_skills: ['verify-change']})]);
  s.nodes.set('save-skill-settings', new Element());
  s.nodes.set('skill-directory-path', new Element());
  s.context.applySkillSettings(data);
  assert.match(s.nodes.get('skill-discovered-catalog').children[1].textContent, /内置/);
  const checkbox = findElement(s.nodes.get('skill-builtin-catalog'), 'input');
  assert.equal(checkbox.checked, true);
  checkbox.checked = false; checkbox.onchange();
  await s.context.saveSkillSettings();
  const payload = JSON.parse(s.requests[0].options.body);
  assert.deepEqual(payload.disabled_builtin_skills, ['verify-change']);
  assert.equal(findElement(s.nodes.get('skill-builtin-catalog'), 'input').checked, false);
});

test('file import stays in settings without preparation, SSE or opening a conversation', async () => {
  const job = {id: 'install-one', skill_name: 'demo', status: 'installed', summary: '未执行指令或准备环境'};
  const state = setup([
    response({installed: true, loadable: true, skill: {name: 'demo', plan_path: '/skills/demo/SKILL.md'}, installation: job}),
    catalog('demo'),
  ]);
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 · 可加载 demo/);
  assert.match(state.nodes.get('skill-settings-status').textContent, /未执行指令或准备环境/);
  assert.match(state.nodes.get('skill-installations').textContent, /文件导入完成/);
  assert.equal(state.opened.length, 0);
  assert.equal(state.streams.length, 0);
  assert.deepEqual(state.conversations, []);
  assert.equal(state.nodes.get('skill-settings-dialog').open, true);
  assert.equal(state.timers.length, 0);
});

test('historical pending receipt can confirm file import without starting preparation', async () => {
  const job = {id: 'pending-one', skill_name: 'demo', status: 'pending', summary: '运行时不可用'};
  const state = setup([response({...job, status: 'installed', summary: '未执行指令或准备环境'})]);
  state.context.renderSkillInstallations([job]);
  assert.match(state.nodes.get('skill-installations').textContent, /准备待启动/);
  assert.equal(state.timers.length, 0);
  const retry = state.nodes.get('skill-installations').children[0].children.find(row => row.textContent === '确认文件安装');
  await retry.onclick();
  assert.match(state.requests[0].url, /pending-one\/retry$/);
  assert.match(state.nodes.get('skill-installations').textContent, /文件导入完成/);
  assert.equal(state.streams.length, 0);
  assert.deepEqual(state.opened, []);
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
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 · 可加载 one/);
  assert.equal(input.value, '');
  input.value = 'selected-again';
  await state.context.uploadSkillDirectoryAndInstall([file], input);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 · 可加载 two/);
  assert.match(state.nodes.get('skill-discovered-catalog').textContent, /已发现 2 个 Skill/);
  assert.equal(vm.runInContext('skillSettingsDirectories[0]', state.context), '/unsaved/skills');
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
  assert.ok(state.buttons.every(button => !button.disabled));
  assert.equal(input.value, '');
});

test('sidebar install works without a settings refresh', async () => {
  const state = setup([installed('demo')], {modalOpen: false});
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.match(state.nodes.get('skill-install-status').textContent, /已安装 · 可加载 demo/);
  assert.equal(state.requests.length, 1);
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
});

test('catalog refresh failure preserves success and displays a refresh warning', async () => {
  const state = setup([installed('demo'), new Error('offline')]);
  await state.context.uploadSkillDirectoryAndInstall([file]);
  const message = state.nodes.get('skill-settings-status').textContent;
  assert.match(message, /已安装 · 可加载 demo/);
  assert.match(message, /目录列表刷新失败/);
  assert.ok(message.includes('\n'), 'warnings must use real newlines');
  assert.equal(state.nodes.get('error-banner').classList.contains('visible'), false);
});

test('server refresh warning remains visible on a successful installation', async () => {
  const result = response({installed: true, loadable: true, skill: {name: 'demo', plan_path: '/skills/demo/SKILL.md'}, warnings: ['runtime catalog unavailable']});
  const state = setup([result, catalog('demo')]);
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 · 可加载 demo/);
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
  assert.match(state.nodes.get('skill-settings-status').textContent, /已安装 · 可加载 demo/);
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

test('clicking outside the settings panel closes it while panel clicks keep it open', () => {
  const state = setup([]);
  const dialog = state.nodes.get('skill-settings-dialog');
  const innerCard = new Element();

  state.context.trackSkillSettingsBackdropPress({target: innerCard, currentTarget: dialog});
  state.context.handleSkillSettingsBackdropClose({target: innerCard, currentTarget: dialog});
  assert.equal(dialog.open, true, 'panel content clicks must keep the panel open');

  // Press inside and release on the backdrop (text selection drag) must not close.
  state.context.trackSkillSettingsBackdropPress({target: innerCard, currentTarget: dialog});
  state.context.handleSkillSettingsBackdropClose({target: dialog, currentTarget: dialog});
  assert.equal(dialog.open, true, 'dragging out of the panel must not close it');

  state.context.trackSkillSettingsBackdropPress({target: dialog, currentTarget: dialog});
  state.context.handleSkillSettingsBackdropClose({target: dialog, currentTarget: dialog});
  assert.equal(dialog.open, false, 'a backdrop press must close the panel');
});

const discovered = name => ({name, source: 'user', plan_path: `/external/skills/${name}/SKILL.md`, description: 'Demo'});
const preparation = (name, status = 'installed') => ({id: `install-${name}`, skill_name: name, status, loadable: status === 'installed', plan_path: `/home/skills/${name}/SKILL.md`, source_plan_path: `/external/skills/${name}/SKILL.md`, summary: `${name} file import`});
const discoveryData = (skills, installations = []) => ({skills, installations, issues: []});
const discoveryControls = state => Array.from(vm.runInContext('discoverySkillControls.values()', state.context));
const discoveryToolbar = state => state.nodes.get('skill-discovered-catalog').children[0];
const selectAllDiscovery = state => {
  const checkbox = findElement(discoveryToolbar(state), 'input');
  checkbox.checked = true;
  checkbox.onchange();
};
const discoveryReceipt = (skill, job = preparation(skill.name)) => response({
  installed: true, loadable: job.loadable === true, skill: {...skill, plan_path: `/home/skills/${skill.name}/SKILL.md`}, installation: job,
});

test('discovery cards have selection and install actions; builtins and ready Skills are excluded', () => {
  const state = setup([]);
  state.context.renderSkillCatalog(discoveryData([
    discovered('one'), discovered('two'), {name: 'builtin', source: 'builtin'}, discovered('ready'),
  ], [preparation('ready', 'ready')]));
  const controls = discoveryControls(state);
  assert.equal(controls.length, 4);
  assert.ok(controls.every(row => row.checkbox.tag === 'input' && row.button.tag === 'button'));
  assert.equal(controls[0].checkbox.getAttribute('aria-label'), '选择 one');
  assert.equal(controls[0].button.textContent, '安装');
  assert.equal(controls[2].button.textContent, '无需安装');
  assert.equal(controls[3].button.textContent, '已安装');
  assert.equal(controls[2].checkbox.disabled, true);
  assert.equal(controls[3].checkbox.disabled, true);
  assert.equal(findElement(discoveryToolbar(state), 'button').disabled, true);
  controls[0].checkbox.checked = true;
  controls[0].checkbox.onchange();
  const all = findElement(discoveryToolbar(state), 'input');
  assert.equal(all.indeterminate, true);
  assert.equal(all.checked, false);
  selectAllDiscovery(state);
  assert.deepEqual(controls.map(row => row.checkbox.checked), [true, true, false, false]);
  assert.equal(all.indeterminate, false);
  assert.equal(all.checked, true);
  assert.equal(findElement(discoveryToolbar(state), 'button').textContent, '全部安装');
  all.checked = false;
  all.onchange();
  assert.ok(controls.every(row => !row.checkbox.checked));
  assert.equal(findElement(discoveryToolbar(state), 'button').disabled, true);
});

test('single discovery install submits its manifest and immediately records import without model dispatch', async () => {
  const skill = discovered('one');
  const job = preparation('one');
  const state = setup([discoveryReceipt(skill, job), response(discoveryData([skill], [job]))]);
  state.context.renderSkillCatalog(discoveryData([skill]));
  await discoveryControls(state)[0].button.onclick();
  assert.equal(state.requests[0].url, '/skills/install');
  assert.equal(state.requests[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(state.requests[0].options.body), {plan_path: skill.plan_path});
  assert.equal(state.requests[1].url, '/settings/skills');
  assert.equal(discoveryControls(state)[0].button.disabled, true);
  assert.doesNotMatch(state.nodes.get('skill-settings-status').textContent, /验证通过/);
  assert.equal(state.streams.length, 0);
  assert.equal(discoveryControls(state)[0].button.textContent, '已安装 · 可加载');
  assert.match(state.nodes.get('skill-settings-status').textContent, /未执行指令或准备环境/);
  assert.equal(state.nodes.get('skill-settings-dialog').open, true);
  assert.deepEqual(state.opened, []);
  assert.equal(discoveryControls(state)[0].checkbox.disabled, true);
});

test('select all installs sequentially, prevents duplicate clicks and keeps each preparation in settings', async () => {
  const one = discovered('one'), two = discovered('two');
  const jobs = [preparation('one'), preparation('two')];
  let finish;
  const state = setup([
    () => new Promise(resolve => {finish = resolve;}), discoveryReceipt(two, jobs[1]),
    response(discoveryData([one, two], jobs)),
  ]);
  vm.runInContext("skillSettingsDirectories = ['/unsaved/skills']", state.context);
  state.context.renderSkillCatalog(discoveryData([one, two, {name: 'builtin', source: 'builtin'}]));
  selectAllDiscovery(state);
  const batch = findElement(discoveryToolbar(state), 'button');
  const first = batch.onclick();
  assert.equal(state.requests.length, 1);
  assert.ok(discoveryControls(state).every(row => row.checkbox.disabled && row.button.disabled));
  assert.equal(batch.disabled, true);
  await batch.onclick();
  await state.context.uploadSkillDirectoryAndInstall([file]);
  assert.equal(state.requests.length, 1, 'shared busy state must prevent another install or upload');
  finish(discoveryReceipt(one, jobs[0]));
  await first;
  assert.deepEqual(state.requests.filter(row => row.options?.method === 'POST').map(row => JSON.parse(row.options.body).plan_path), [one.plan_path, two.plan_path]);
  assert.match(state.nodes.get('skill-settings-status').textContent, /可加载 2\/2/);
  assert.equal(state.streams.length, 0);
  assert.equal(vm.runInContext('selectedDiscoverySkills.size', state.context), 0);
  assert.equal(vm.runInContext('skillSettingsDirectories[0]', state.context), '/unsaved/skills');
  assert.equal(vm.runInContext('skillInstallInFlight', state.context), false);
  assert.equal(state.nodes.get('skill-settings-dialog').open, true);
  assert.deepEqual(state.opened, []);
});

test('partial failure continues the batch, preserves failed selection and allows a clean retry', async () => {
  const one = discovered('one'), two = discovered('two');
  const state = setup([
    response({detail: '同名 Skill 内容不同'}, 409), discoveryReceipt(two),
    response(discoveryData([one, two], [preparation('two')])),
    discoveryReceipt(one), response(discoveryData([one, two], [preparation('one'), preparation('two')])),
  ]);
  state.context.renderSkillCatalog(discoveryData([one, two]));
  selectAllDiscovery(state);
  await findElement(discoveryToolbar(state), 'button').onclick();
  assert.match(state.nodes.get('skill-settings-status').textContent, /可加载 1\/2/);
  assert.match(discoveryControls(state)[0].status.textContent, /同名 Skill 内容不同/);
  assert.equal(discoveryControls(state)[0].checkbox.checked, true);
  assert.equal(discoveryControls(state)[1].checkbox.checked, false);
  assert.equal(discoveryControls(state)[0].button.disabled, false);
  await discoveryControls(state)[0].button.onclick();
  assert.doesNotMatch(discoveryControls(state)[0].status.textContent, /内容不同/);
  assert.equal(vm.runInContext('selectedDiscoverySkills.size', state.context), 0);
});

test('historical pending discovery imports the source through the common install endpoint', async () => {
  const skill = discovered('pending');
  const state = setup([discoveryReceipt(skill), response(discoveryData([skill], [preparation('pending')]))]);
  state.context.renderSkillCatalog(discoveryData([skill], [preparation('pending', 'pending')]));
  assert.equal(discoveryControls(state)[0].button.textContent, '安装');
  await discoveryControls(state)[0].button.onclick();
  assert.equal(state.requests[0].url, '/skills/install');
  assert.deepEqual(JSON.parse(state.requests[0].options.body), {plan_path: skill.plan_path});
  assert.equal(state.streams.length, 0);
  assert.equal(discoveryControls(state)[0].button.disabled, true);
  assert.deepEqual(state.opened, []);
});

test('interrupted discovery request reports unknown outcome and releases all controls', async () => {
  const skill = discovered('one');
  const state = setup([new Error('network interrupted')]);
  state.context.renderSkillCatalog(discoveryData([skill]));
  await discoveryControls(state)[0].button.onclick();
  assert.match(state.nodes.get('skill-settings-status').textContent, /结果尚未确认/);
  assert.equal(state.requests.length, 1, 'must not automatically duplicate an unknown-outcome install');
  assert.equal(discoveryControls(state)[0].button.disabled, false);
  assert.equal(vm.runInContext('skillInstallInFlight', state.context), false);
});

test('refresh preserves current selection and removes vanished skills; an empty list cannot install', () => {
  const state = setup([]);
  state.context.renderSkillCatalog(discoveryData([discovered('one'), discovered('two')]));
  selectAllDiscovery(state);
  state.context.renderSkillCatalog(discoveryData([discovered('two')]));
  assert.equal(vm.runInContext('selectedDiscoverySkills.size', state.context), 1);
  assert.equal(discoveryControls(state)[0].checkbox.checked, true);
  state.context.renderSkillCatalog(discoveryData([]));
  assert.equal(vm.runInContext('selectedDiscoverySkills.size', state.context), 0);
  assert.equal(findElement(discoveryToolbar(state), 'input').disabled, true);
  assert.equal(findElement(discoveryToolbar(state), 'button').disabled, true);
});


test('failed load validation preserves imported files and permits retry without claiming loadability', async () => {
  const skill = discovered('broken');
  const job = {...preparation('broken'), loadable: false};
  const state = setup([discoveryReceipt(skill, job), response(discoveryData([skill], [job]))]);
  state.context.renderSkillCatalog(discoveryData([skill]));
  await discoveryControls(state)[0].button.onclick();
  assert.equal(discoveryControls(state)[0].button.disabled, false);
  assert.equal(discoveryControls(state)[0].button.textContent, '已导入 · 重试加载');
  assert.match(state.nodes.get('skill-settings-status').textContent, /可加载 0\/1/);
  assert.equal(state.streams.length, 0);
});
