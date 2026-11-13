const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function section(start, end) {
  const first = page.indexOf(start);
  const last = page.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `missing page section: ${start}`);
  return page.slice(first, last);
}

function setup() {
  class Element {
    constructor() {
      this.children = [];
      this.className = '';
      this.textContent = '';
    }
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.children.push(item); return item; }
    replaceChildren(...items) { this.children = items; }
    setAttribute() {}
  }
  const pane = new Element();
  const context = vm.createContext({
    document: {
      getElementById: id => (id === 'files-pane' ? pane : null),
      createElement: () => new Element(),
    },
    renderSourceEvaluationRunner: () => {
      const panel = new Element();
      panel.className = 'trace-overview-panel source-evaluation';
      return panel;
    },
    renderSkillInstallButton: () => {
      const panel = new Element();
      panel.className = 'trace-overview-panel skill-install';
      return panel;
    },
  });
  vm.runInContext(section('function renderFilesPane(', 'function taskStateClass('), context);
  return {context, pane};
}

test('the right pane is a reserved file view with one empty state', () => {
  const {context, pane} = setup();
  context.renderFilesPane();

  assert.equal(pane.children.length, 3);
  const [empty, evaluation, skill] = pane.children;
  assert.equal(empty.className, 'files-pane-empty');
  assert.equal(empty.children[0].textContent, '暂无打开的文件');
  assert.match(empty.children[1].textContent, /读取或修改文件后/);
  assert.match(evaluation.className, /source-evaluation/);
  assert.match(skill.className, /skill-install/);
});

test('re-rendering replaces the pane instead of stacking placeholders', () => {
  const {context, pane} = setup();
  context.renderFilesPane();
  context.renderFilesPane();
  const placeholders = pane.children.filter(
    node => node.className === 'files-pane-empty',
  );
  assert.equal(placeholders.length, 1);
});

test('the retired trace timeline no longer exists in the page script', () => {
  assert.match(page, /function renderFilesPane\(\)/);
  assert.doesNotMatch(page, /TIMELINE_KIND_TEXT|renderTraceView|renderTracePane/);
  assert.doesNotMatch(page, /trace-timeline-list|trace-event-card/);
});
