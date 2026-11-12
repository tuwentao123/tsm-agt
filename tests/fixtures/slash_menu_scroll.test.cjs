// Drive the rendered page's slash menu with a fake DOM.
//
// The menu is maxHeight + overflowY:auto, so highlighting an entry past the
// visible window must scroll it into view; a new render must start at the top.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const pageScript = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const start = pageScript.indexOf('const slashCommands = [');
const end = pageScript.indexOf('function applySlashCommand(command, input) {');
assert.ok(
  start >= 0 && end > start,
  'the slash menu must stay above applySlashCommand() in the page script',
);

const ITEM_HEIGHT = 20;

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.style = {};
    this.dataset = {};
    this.children = [];
    this.listeners = new Map();
    this.offsetTop = 0;
    this.offsetHeight = 0;
    this.clientHeight = 0;
    this.scrollTop = 0;
    this.textContent = '';
  }

  appendChild(child) {
    child.offsetTop = this.children.length * ITEM_HEIGHT;
    child.offsetHeight = ITEM_HEIGHT;
    this.children.push(child);
    return child;
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }

  getBoundingClientRect() {
    const height = this.children.length * ITEM_HEIGHT;
    return {left: 0, top: 0, right: 240, bottom: height, width: 240, height};
  }

  set innerHTML(value) {
    this.children.length = 0;
  }

  get innerHTML() {
    return '';
  }
}

const body = new FakeElement('body');
globalThis.HTMLElement = FakeElement;
globalThis.window = {innerHeight: 800};
globalThis.document = {
  body,
  documentElement: {clientHeight: 800},
  createElement: tag => new FakeElement(tag),
};
globalThis.promptComposer = null;
globalThis.persistActiveDraft = () => {};
globalThis.applySlashMenuItem = () => {};

const skills = [];
for (let index = 0; index < 12; index += 1) {
  skills.push({name: `ph-item-${index}`, description: `第 ${index} 条`, source: 'user'});
}
globalThis.fetch = async () => ({ok: true, json: async () => ({skills})});

vm.runInThisContext(pageScript.slice(start, end), {filename: 'page.js'});

function composerInput() {
  return {
    value: '/ph',
    selectionStart: 3,
    selectionEnd: 3,
    focus() {},
    getBoundingClientRect: () => ({left: 10, top: 700, right: 700, bottom: 740, width: 690, height: 40}),
  };
}

function renderMenu(clientHeight) {
  globalThis.renderSlashMenu(composerInput());
  const menu = body.children[0];
  menu.clientHeight = clientHeight;
  menu.scrollTop = 0;
  return menu;
}

// 选中索引存在页面脚本的 let 绑定里，外部读不到，只能从高亮样式反查。
function activeIndex(menu) {
  return menu.children.findIndex(child => child.style.background === '#f1f5f9');
}

test('arrow keys scroll the highlighted entry into the visible window', async () => {
  await globalThis.loadSkillMenuSkills();
  const menu = renderMenu(100);

  assert.equal(menu.children.length, 8, 'direct Skill matches are capped at 8 entries');
  assert.equal(menu.scrollTop, 0);

  // 前 5 条（100px）本来就在可视区内，滚动位置不动。
  for (let step = 0; step < 5; step += 1) {
    globalThis.moveSlashMenuSelection(1);
  }
  assert.equal(activeIndex(menu), 4);
  assert.equal(menu.scrollTop, 0);
  assert.equal(menu.children[4].style.background, '#f1f5f9');

  // 第 6 条到底部之外，菜单必须跟着滚，并且该条贴住底部。
  globalThis.moveSlashMenuSelection(1);
  assert.equal(activeIndex(menu), 5);
  assert.equal(menu.scrollTop, 20);
  assert.equal(menu.children[5].offsetTop + ITEM_HEIGHT, menu.scrollTop + menu.clientHeight);

  // 走到最后一条仍然完整可见。
  globalThis.moveSlashMenuSelection(1);
  globalThis.moveSlashMenuSelection(1);
  assert.equal(activeIndex(menu), 7);
  assert.equal(menu.scrollTop, 60);
  assert.equal(menu.children[7].offsetTop + ITEM_HEIGHT, menu.scrollTop + menu.clientHeight);
});

test('moving back up scrolls to keep the highlighted entry visible', async () => {
  const menu = renderMenu(100);
  globalThis.moveSlashMenuSelection(-1);
  assert.equal(activeIndex(menu), 7);
  assert.equal(menu.scrollTop, 60);

  // index 3 的顶部是 60px，正好在可视区顶端，不需要继续退。
  for (let step = 0; step < 4; step += 1) {
    globalThis.moveSlashMenuSelection(-1);
  }
  assert.equal(activeIndex(menu), 3);
  assert.equal(menu.scrollTop, 60);

  // index 2 在顶端之外，菜单回退到该条顶部。
  globalThis.moveSlashMenuSelection(-1);
  assert.equal(activeIndex(menu), 2);
  assert.equal(menu.scrollTop, 40);
});

test('a short list never scrolls, and a new render starts at the top', async () => {
  const menu = renderMenu(400);
  for (let step = 0; step < 8; step += 1) {
    globalThis.moveSlashMenuSelection(1);
  }
  assert.equal(activeIndex(menu), 7);
  assert.equal(menu.scrollTop, 0);

  menu.scrollTop = 60;
  globalThis.renderSlashMenu(composerInput());
  assert.equal(menu.scrollTop, 0);
});
