const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function setup() {
  class Element {
    constructor() {
      this.children = [];
      this.attributes = {};
      this.dataset = {};
      this.textContent = '';
      this.innerHTML = '';
      this.className = '';
    }
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.append(item); return item; }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = value; }
    querySelector() { return null; }
    addEventListener() {}
  }
  const context = vm.createContext({
    document: {getElementById: () => null, createElement: () => new Element()},
    activeWorkspaceId: 'ws-1',
    skillInstallInFlight: false,
    renderTodoPanel: (todoList) => {
      const todos = todoList && Array.isArray(todoList.todos) ? todoList.todos : [];
      if (!todos.length) return null;
      const stub = new Element();
      stub.className = 'todo-panel-stub';
      return stub;
    },
  });
  vm.runInContext(
    page.slice(page.indexOf('const TIMELINE_KIND_TEXT = {'), page.indexOf('let skillInstallInFlight')),
    context,
  );
  return {context, pane: new Element()};
}

function conversation() {
  return {tasks: [
    {
      taskId: 'task-one',
      goal: '修复历史图片渲染',
      status: 'running',
      todoList: null,
      progress: [
        {sequence: 1, text: '读取 app.py', progressData: {kind: 'tool_start', tool_name: 'core.read_file'}},
        {sequence: 2, text: '命令执行失败', progressData: {kind: 'tool_error', tool_name: 'core.run_command'}},
      ],
    },
    {
      taskId: 'task-two',
      goal: '第二条任务',
      projection: {display_status: 'RUNNING'},
      todoList: {revision: 3, todos: [{id: '0', content: '步骤 1', status: 'pending'}]},
      progress: [{sequence: 5, text: '模型输出片段', progressData: {kind: 'model_delta'}}],
    },
  ]};
}

function classNames(element) {
  return element.children.map(child => String(child.className || '')).join(' ');
}

test('trace pane renders one merged timeline instead of the legacy trace board', () => {
  const s = setup();
  s.context.renderTraceView(s.pane, conversation());

  const head = s.pane.children[0];
  assert.equal(head.className, 'trace-timeline-head');
  assert.equal(head.children[0].textContent, '执行轨迹');
  assert.equal(head.children[1].textContent, '5 个事件 · 2 个任务 · 2 个执行中');

  const list = s.pane.children[1];
  assert.equal(list.className, 'trace-timeline-list');
  assert.equal(list.children.length, 5);
  assert.deepEqual(
    list.children.map(item => item.dataset.eventKey),
    ['task-one:start', 'task-one:1:tool_start', 'task-one:2:tool_error', 'task-two:start', 'task-two:5:model_delta'],
  );

  const start = list.children[0];
  assert.equal(start.className, 'trace-event trace-event-running');
  assert.equal(start.children[0].className, 'trace-event-card');
  assert.equal(start.children[0].children[0].children[0].textContent, '任务');
  assert.equal(start.children[0].children[0].children[1].textContent, '修复历史图片渲染');
  assert.equal(start.children[0].children[0].children[2].textContent, '执行中');
  assert.equal(start.children[0].children[1].textContent, 'task task-one');

  const tool = list.children[1];
  assert.equal(tool.className, 'trace-event trace-event-running');
  assert.equal(tool.children[0].children[0].children[0].textContent, '工具');
  assert.equal(tool.children[0].children[0].children[1].textContent, '工具开始 · core.read_file');
  assert.equal(tool.children[0].children[0].children[2].textContent, '执行中');
  assert.equal(tool.children[0].children[1].textContent, '读取 app.py');

  const failed = list.children[2];
  assert.equal(failed.className, 'trace-event trace-event-failed');
  assert.equal(failed.children[0].children[0].children[2].textContent, '失败');

  const model = list.children[4];
  assert.equal(model.children[0].children[0].children[0].textContent, '模型');
  assert.equal(model.children[0].children[0].children[1].textContent, '模型');

  assert.match(classNames(s.pane), /todo-panel-stub/);
  assert.match(classNames(s.pane), /trace-timeline-tools/);
  assert.doesNotMatch(classNames(s.pane), /trace-task-card|trace-summary-strip|trace-legend-grid/);
});

test('timeline surfaces waiting approvals and finished task ends', () => {
  const s = setup();
  s.context.renderTraceView(s.pane, {tasks: [{
    taskId: 'task-three',
    goal: '等待授权',
    status: 'cancelled',
    waiting: {kind: 'APPROVAL', approval: {action: 'core.run_command'}},
    progress: [],
  }]});
  const list = s.pane.children[1];
  assert.deepEqual(
    list.children.map(item => item.dataset.eventKey),
    ['task-three:start', 'task-three:waiting', 'task-three:end'],
  );
  const waitingCard = list.children[1].children[0];
  assert.equal(list.children[1].className, 'trace-event trace-event-waiting');
  assert.equal(waitingCard.children[0].children[0].textContent, '授权');
  assert.equal(waitingCard.children[0].children[1].textContent, '等待用户授权');
  assert.equal(waitingCard.children[0].children[2].textContent, '等待');
  assert.equal(waitingCard.children[1].textContent, 'core.run_command');
  assert.equal(list.children[2].className, 'trace-event trace-event-failed');
  assert.equal(list.children[2].children[0].children[0].children[2].textContent, '已停止');
});

test('empty conversation renders a timeline hint and re-render drops stale events', () => {
  const s = setup();
  s.context.renderTraceView(s.pane, conversation());
  assert.match(classNames(s.pane), /trace-timeline-list/);
  s.context.renderTraceView(s.pane, {tasks: []});
  assert.doesNotMatch(classNames(s.pane), /trace-timeline-list|trace-timeline-tools/);
  assert.equal(s.pane.children[0].className, 'trace-timeline-head');
  assert.equal(s.pane.children[1].className, 'trace-timeline-caption');
  assert.match(s.pane.children[1].textContent, /暂无执行事件/);
});
