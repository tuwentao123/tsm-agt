// Drive the page's execution card with a fake DOM.
//
// The card is what turns a tool call into "执行命令 … / 参数 / 输出 / 标识" with a
// collapse toggle, so these checks cover the collapsed default, the toggle and
// the fields the card promises to show.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const pageScript = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const start = pageScript.indexOf('function toolFailureDescription(');
const end = pageScript.indexOf('function renderTurnCallPanel(');
assert.ok(start >= 0 && end > start, 'the card, its merge step and the turn lines must be sliceable');

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.style = {};
    this.dataset = {};
    this.attributes = {};
    this.children = [];
    this.listeners = new Map();
    this.className = '';
    this.textContent = '';
    this.title = '';
    this.hidden = false;
    this.type = '';
  }

  appendChild(child) {
    this.children.push(child);
    return child;
  }

  addEventListener(type, handler) {
    this.listeners.set(type, handler);
  }

  setAttribute(name, value) {
    this.attributes[name] = String(value);
  }
}

globalThis.document = {createElement: tag => new FakeElement(tag)};

vm.runInThisContext(pageScript.slice(start, end), {filename: 'page.js'});

function findAll(node, predicate) {
  const found = [];
  const walk = current => {
    if (predicate(current)) found.push(current);
    (current.children || []).forEach(walk);
  };
  walk(node);
  return found;
}

function textOf(node) {
  return findAll(node, () => true).map(item => item.textContent).join(' ');
}

function build(model) {
  const card = globalThis.toolExecutionCard(globalThis.toolCardModel(model));
  const [head, body] = card.children;
  return {card, head, body};
}

const completed = {
  kind: 'tool_completed',
  tool_name: 'core.run_command',
  tool_call_id: 'run_command_toolu_bdrk_01FM',
  ok: true,
  elapsed_seconds: 1.24,
  operation_presentation: 'cd /data1/aide && python3 process.py',
  operation_arguments: {argv: ['cd', '/data1/aide', 'python3', 'process.py'], cwd: '.'},
  output: {stdout: '日志字节量/天 估算:\n   Credit 179,272,114 行'},
};

test('the card starts collapsed and expands on click', () => {
  const {card, head, body} = build(completed);

  assert.equal(card.dataset.open, 'false');
  assert.equal(body.hidden, true);
  // 只写 hidden 时，任何 display 作者样式都会把它盖掉；这里要真折叠。
  assert.equal(body.style.display, 'none');
  assert.equal(head.attributes['aria-expanded'], 'false');

  head.listeners.get('click')();
  assert.equal(card.dataset.open, 'true');
  assert.equal(body.hidden, false);
  assert.equal(body.style.display, '');
  assert.equal(head.attributes['aria-expanded'], 'true');

  head.listeners.get('click')();
  assert.equal(card.dataset.open, 'false');
  assert.equal(body.hidden, true);
  assert.equal(body.style.display, 'none');
});

test('the header names the command, its duration and its status', () => {
  const {head} = build(completed);

  const title = head.children[1];
  assert.equal(title.textContent, '已运行');
  const command = head.children[2];
  assert.equal(command.textContent, 'cd /data1/aide && python3 process.py');
  assert.equal(command.title, 'cd /data1/aide && python3 process.py');

  const meta = head.children[3];
  assert.equal(meta.children[0].textContent, '1.2s');
  assert.equal(meta.children[1].textContent, '完成');
  assert.match(meta.children[1].className, /ok/);
});

test('the expanded body shows arguments, real output and call identity', () => {
  const {body} = build(completed);

  const labels = findAll(body, node => node.className === 'tool-card-section-title')
    .map(node => node.textContent);
  assert.deepEqual(labels, ['参数', '输出']);

  const [argumentsBlock, outputBlock] = findAll(body, node => node.className === 'tool-card-pre');
  // 参数是 “key: value” 行，不再是带括号引号的 JSON。
  assert.equal(
    argumentsBlock.textContent,
    'argv: cd /data1/aide python3 process.py\ncwd: .',
  );
  assert.doesNotMatch(argumentsBlock.textContent, /[{}]|"argv"/);
  assert.match(outputBlock.textContent, /Credit 179,272,114 行/);

  const facts = textOf(findAll(body, node => node.className === 'tool-card-facts')[0]);
  assert.match(facts, /工具标识 core\.run_command/);
  assert.match(facts, /工具分类 execute/);
  assert.match(facts, /调用 ID run_command_toolu_bdrk_01FM/);
});

test('the open state survives a re-render of the same call', () => {
  const call = {
    kind: 'tool_completed',
    tool_name: 'core.run_command',
    tool_call_id: 'call-state-1',
    ok: true,
    operation_presentation: 'pytest -q',
  };

  const first = build(call);
  first.head.listeners.get('click')();
  assert.equal(first.card.dataset.open, 'true');

  // 进度事件的每一帧都会重建 DOM，收起状态不能因此丢失。
  const second = build({...call, output: {stdout: '23 passed'}});
  assert.equal(second.card.dataset.open, 'true');
  assert.equal(second.body.hidden, false);
  assert.equal(second.body.style.display, '');

  second.head.listeners.get('click')();
  const third = build({...call, output: {stdout: '23 passed'}});
  assert.equal(third.card.dataset.open, 'false');
  assert.equal(third.body.hidden, true);
  assert.equal(third.body.style.display, 'none');
});

test('a call without a presentation still names what ran', () => {
  const fromArgv = build({
    kind: 'tool_started',
    tool_name: 'core.run_command',
    operation_arguments: {argv: ['pytest', '-q'], cwd: '.'},
  });
  assert.equal(fromArgv.head.children[2].textContent, 'pytest -q');

  const durable = build({
    kind: 'tool_started',
    tool_name: 'core.run_command',
    command: 'grep -rn pattern src',
  });
  assert.equal(durable.head.children[2].textContent, 'grep -rn pattern src');

  const readOnly = build({
    kind: 'tool_completed',
    tool_name: 'core.read_file',
    ok: true,
    target: 'tests/test_tool_failure_ui.py',
  });
  assert.equal(readOnly.head.children[2].textContent, 'core.read_file tests/test_tool_failure_ui.py');
  assert.match(textOf(readOnly.body), /该工具不产生命令行输出/);

  const commandNoOutput = build({
    kind: 'tool_completed',
    tool_name: 'core.run_command',
    ok: true,
    operation_presentation: 'true',
  });
  assert.match(textOf(commandNoOutput.body), /无标准输出/);
});

test('truncated arguments lose the JSON punctuation', () => {
  const {body} = build({
    kind: 'tool_completed',
    tool_name: 'core.run_command',
    ok: true,
    arguments_text: '{\n  "argv": ["grep", "needle"',
  });
  const [argumentsBlock] = findAll(body, node => node.className === 'tool-card-pre');
  assert.equal(argumentsBlock.textContent, '\"argv\": [\"grep\", \"needle\"');
  assert.equal(argumentsBlock.textContent.startsWith('{'), false);
});

test('a finished call keeps one card instead of a leftover running one', () => {
  const started = {
    kind: 'tool_started',
    tool_name: 'core.apply_patch',
    tool_call_id: 'call-x',
    command: 'core.apply_patch src/app.py',
    target: 'src/app.py',
    arguments_text: 'path: src/app.py',
  };
  const completed = {
    kind: 'tool_completed',
    tool_call_id: 'call-x',
    ok: true,
    output: {stdout: 'Applied 1 edit'},
  };

  const merged = globalThis.mergeDurableToolItems([started, completed]);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].kind, 'tool_completed');
  assert.equal(merged[0].command, 'core.apply_patch src/app.py');

  const lines = globalThis.turnProgressLines([started, completed]);
  const toolLines = lines.filter(line => line.card);
  assert.equal(toolLines.length, 1);
  assert.equal(toolLines[0].card.status, 'ok');
  assert.equal(toolLines[0].card.output, 'Applied 1 edit');
  assert.match(toolLines[0].text, /完成/);
});

test('a call without a completion stays running', () => {
  const lines = globalThis.turnProgressLines([
    {kind: 'tool_started', tool_name: 'core.run_command', tool_call_id: 'call-y', command: 'sleep 30'},
  ]);
  assert.equal(lines.filter(line => line.card).length, 1);
  assert.equal(lines[0].card.status, 'running');
});

test('an ended turn never leaves a card claiming it is still running', () => {
  const lines = globalThis.turnProgressLines([
    {kind: 'turn_started'},
    {kind: 'tool_started', tool_name: 'core.run_command', tool_call_id: 'call-orphan',
      operation_presentation: 'pytest -q'},
    {kind: 'turn_interrupted', detail: 'stopped'},
  ]);
  const toolLines = lines.filter(line => line.card);
  assert.equal(toolLines.length, 1);
  assert.equal(toolLines[0].card.status, 'incomplete');

  const {head, body} = build({kind: 'tool_started', tool_name: 'core.run_command',
    tool_call_id: 'call-orphan', incomplete: true});
  assert.equal(head.children[1].textContent, '已结束');
  assert.equal(head.children[3].children[0].textContent, '未完成');
  assert.match(textOf(body), /工具标识 core\.run_command/);
});

test('a commentary fragment is a note, not a grey meta line', () => {
  globalThis.describeProgress = item => (item.kind === 'commentary' ? item.activity : '');
  const lines = globalThis.turnProgressLines([
    {kind: 'model_completed', model_call: 1},
    {kind: 'commentary', activity: '先看渲染循环的当前写法'},
    {kind: 'turn_completed'},
  ]);
  const note = lines.find(line => line.text === '先看渲染循环的当前写法');
  assert.ok(note, 'the commentary keeps its text');
  assert.equal(note.note, true);
  assert.equal(note.tone, '');
  const meta = lines.find(line => /模型第 1 轮/.test(line.text));
  assert.equal(meta.note, undefined);
});

test('compaction progress collapses to one status line and is not a note', () => {
  globalThis.describeProgress = item => (
    item.kind === 'commentary' || item.kind === 'context_summary' ? item.activity : ''
  );
  const lines = globalThis.turnProgressLines([
    {kind: 'context_summary', activity: '正在压缩上下文，准备摘要输入'},
    {kind: 'context_summary', activity: '正在压缩上下文：摘要输入已整理，已用 2 秒'},
    {kind: 'context_summary', activity: '正在压缩上下文：等待摘要响应，已用 21 秒，摘要请求 1/2'},
  ]);
  const statuses = lines.filter(line => /正在压缩上下文/.test(line.text));
  assert.equal(statuses.length, 1, 'one compaction status line per turn');
  assert.match(statuses[0].text, /已用 21 秒/, 'the newest stage wins');
  assert.equal(statuses[0].note, undefined, 'compaction is runtime status, not model prose');
});

test('live entries merge a finished call into its started entry', () => {
  const entries = [
    {text: '执行命令 vitest', progressData: {kind: 'tool_started', tool_name: 'core.run_command',
      tool_call_id: 'call-z', operation_presentation: 'vitest'}},
    {text: '工具 core.run_command 完成', progressData: {kind: 'tool_completed',
      tool_call_id: 'call-z', ok: true, output: {stdout: 'passed'}}},
  ];

  const merged = globalThis.mergeLiveToolEntries(entries);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].text, '工具 core.run_command 完成');
  const card = globalThis.toolCardModel(merged[0].progressData);
  assert.equal(card.status, 'ok');
  assert.equal(card.command, 'vitest');
  assert.equal(card.output, 'passed');
});

test('a failed command reports its exit code and stderr', () => {
  const {head, body} = build({
    kind: 'tool_completed',
    tool_name: 'core.run_command',
    ok: false,
    elapsed_seconds: 0.42,
    command_failure: {exit_code: 2, stderr: 'grep: docs: No such file or directory'},
  });

  assert.equal(head.children[3].children[0].textContent, '420ms');
  const status = head.children[3].children[1];
  assert.equal(status.textContent, '退出码 2');
  assert.match(status.className, /failed/);
  assert.match(textOf(body), /grep: docs: No such file or directory/);
});

test('a recorded error code wins over the exit code, and a running call says so', () => {
  const failed = build({
    kind: 'tool_completed',
    tool_name: 'core.run_command',
    ok: false,
    elapsed_seconds: 0.5,
    error_code: 'PROCESS_EXIT_NON_ZERO',
    command_failure: {exit_code: 1},
  });
  assert.equal(failed.head.children[3].children[1].textContent, 'PROCESS_EXIT_NON_ZERO');

  const running = build({
    kind: 'tool_started',
    tool_name: 'core.run_command',
    operation_presentation: 'ls -1',
  });
  assert.equal(running.head.children[1].textContent, '执行命令');
  assert.equal(running.head.children[3].children[0].textContent, '运行中');
  assert.match(textOf(running.body), /运行中，尚未产生输出/);
});

test('durable items without live arguments still name their target and family', () => {
  const {head, body} = build({
    kind: 'tool_completed',
    tool_name: 'mcp.gitlab.search',
    ok: true,
    target: 'src/app.py',
    arguments_text: '{\n  "path": "src/app.py"\n}',
  });

  assert.equal(head.children[2].textContent, 'mcp.gitlab.search src/app.py');
  const facts = textOf(findAll(body, node => node.className === 'tool-card-facts')[0]);
  assert.match(facts, /工具分类 mcp/);
  assert.match(facts, /调用对象 src\/app\.py/);
  assert.match(textOf(body), /path: src\/app\.py/);
});
