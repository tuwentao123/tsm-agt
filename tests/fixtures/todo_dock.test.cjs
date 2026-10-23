const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function setup() {
  class Element {
    constructor() { this.children = []; this.attributes = {}; this.hidden = true; }
    append(...items) { this.children.push(...items); }
    appendChild(item) { this.append(item); return item; }
    replaceChildren(...items) { this.children = items; }
    setAttribute(key, value) { this.attributes[key] = value; }
  }
  const dock = new Element();
  const context = vm.createContext({document: {
    getElementById: () => dock, createElement: () => new Element(),
  }});
  vm.runInContext(page.slice(page.indexOf('function renderConversationTodo('),
    page.indexOf('function renderTaskCard(')), context);
  return {dock, render: conversation => context.renderConversationTodo(conversation)};
}
function task(id, statuses) {
  return {task: {taskId: id, todoList: {todos: statuses.map((status, i) => ({
    id: String(i), content: `步骤 ${i + 1}`, status,
  }))}}};
}

test('Todo dock follows progress, checks completed items and disappears only when all done', () => {
  const s = setup();
  const message = task('a', ['in_progress', 'pending']);
  const conversation = {activeTaskId: 'a', messages: [message]};
  s.render(conversation);
  assert.equal(s.dock.hidden, false);
  assert.equal(s.dock.children[0].children[0].children[1].textContent, '0/2 已完成');
  message.task.todoList.todos[0].status = 'completed';
  message.task.todoList.todos[1].status = 'blocked';
  s.render(conversation);
  const list = s.dock.children[0].children[1];
  assert.equal(list.children[0].children[0].textContent, '✓');
  assert.equal(list.children[0].children[0].attributes['aria-checked'], 'true');
  assert.equal(list.children[1].className, 'todo-item blocked');
  assert.equal(s.dock.hidden, false);
  message.task.todoList.todos[1].status = 'completed';
  s.render(conversation);
  assert.equal(s.dock.hidden, true);
  assert.equal(s.dock.children.length, 0);
});

test('switching or reloading conversations restores only the current list', () => {
  const s = setup();
  const old = task('old', ['pending']);
  s.render({messages: [old, task('new', ['completed'])]});
  assert.equal(s.dock.hidden, true);
  s.render({messages: [old, {task: {taskId: 'new', todoList: null}}]});
  assert.equal(s.dock.hidden, true);
  s.render({activeTaskId: 'old', messages: [old, task('new', ['completed'])]});
  assert.equal(s.dock.hidden, false);
  s.render(null);
  assert.equal(s.dock.hidden, true);
  s.render(JSON.parse(JSON.stringify({messages: [old]})));
  assert.equal(s.dock.hidden, false);
  old.task.todoList.todos = [];
  s.render({messages: [old]});
  assert.equal(s.dock.hidden, true);
});
