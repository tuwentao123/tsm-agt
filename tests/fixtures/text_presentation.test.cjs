const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const helpers = page.slice(page.indexOf('const TERMINAL_STATES ='), page.indexOf('function renderTurnMarker('));

test('unknown tool outcomes show timeout and observation guidance without claiming a pause', () => {
  const context = vm.createContext({});
  const descriptions = page.slice(page.indexOf('function describeFailure('), page.indexOf('async function handleCancelAction('));
  const replay = page.slice(page.indexOf('function turnProgressLines('), page.indexOf('function renderTurnCallPanel('));
  vm.runInContext(descriptions + replay, context);
  const failure = {kind: 'tool_completed', tool_name: 'core.run_command', tool_call_id: 'push',
    ok: false, error_code: 'UNKNOWN_OUTCOME', detail: 'tool exceeded 62 second deadline'};
  const live = context.describeProgress(failure);
  assert.match(live, /62 秒/);
  assert.match(live, /结果未确认/);
  assert.match(live, /继续查询/);
  assert.doesNotMatch(live, /已暂停|请先核对/);
  const waiting = {kind: 'waiting', waiting_kind: 'outcome_reconciliation',
    action: '操作结果尚未确认，已暂停后续操作；请先核对目标系统中的实际状态。'};
  assert.match(context.describeProgress(waiting), /核对/);
  const lines = context.turnProgressLines([
    {kind: 'tool_started', tool_name: 'core.run_command', tool_call_id: 'push'}, failure, waiting,
  ]);
  assert.match(lines[1].text, /62 秒/);
  assert.match(lines[1].text, /结果未确认/);
  assert.match(lines[2].text, /核对/);
});

test('context overflow from tool output does not blame image attachments', () => {
  const context = vm.createContext({});
  vm.runInContext(page.slice(page.indexOf('function describeFailure('), page.indexOf('function describeProgress(')), context);
  const message = context.describeFailure('context cannot fit after safe compaction: estimated_input=219318, hard_input=119808; allocations: current_turn_and_tool_protocol=186570');
  assert.match(message, /工具输出/);
  assert.doesNotMatch(message, /缩小图片/);
  assert.match(message, /未发送给模型/);
  assert.equal(context.describeFailure('unrelated failure'), 'unrelated failure');
});

function setup() {
  const conversation = {messages: []}, frames = [], nodes = new Map();
  let rebuilds = 0, time = 0;
  const classes = () => {
    const values = new Set();
    return {add: x => values.add(x), remove: x => values.delete(x),
      contains: x => values.has(x), toggle: (x, on) => on ? values.add(x) : values.delete(x)};
  };
  const body = () => {
    let text = '';
    return {classList: classes(), firstChild: null,
      get textContent() { return text; },
      set textContent(value) {
        text = value;
        this.firstChild = value ? {nodeType: 3, appendData: suffix => { text += suffix; }} : null;
      },
      set innerHTML(value) { this.html = value; },
    };
  };
  const chat = {scrollTop: 0, scrollHeight: 100,
    querySelectorAll: () => [...nodes.values()]};
  const context = vm.createContext({Intl,
    activeConversationId: 'chat', shouldAutoFollowChat: false,
    lastConversationScrollTop: 0, isNearChatBottom: () => false,
    updateChatJumpButton() {},
    getConversationById: () => conversation,
    document: {getElementById: () => chat},
    requestAnimationFrame: callback => { frames.push(callback); return frames.length; },
    renderMarkdown: text => `<formatted>${text}</formatted>`,
    scheduleProgressRender() { mount(); },
  });
  vm.runInContext(helpers, context);
  function mount() {
    rebuilds += 1;
    nodes.clear();
    for (const message of conversation.messages) {
      if (!message.textPresentation) continue;
      const content = body(), label = {};
      const element = {dataset: {textStreamId: message.textPresentation.id}, classList: classes(),
        content, label, querySelector: selector => selector === '.message-body' ? content : label};
      nodes.set(message.textPresentation.id, element);
      context.paintTaskTextBody(content, message);
      context.scheduleTaskTextRender(message, 'chat');
    }
  }
  function tick() {
    time += 16;
    frames.splice(0).forEach(callback => callback(time));
  }
  function drain() {
    for (let i = 0; frames.length && i < 300; i++) tick();
    assert.equal(frames.length, 0, 'animation must stop after draining or removal');
  }
  function delta(text, sequence = 1, phase = 'final_answer') {
    context.applyTaskText('task', {kind: 'text_delta', text_id: 'text', text,
      text_sequence: sequence, phase, turn_id: 'turn'}, 'chat');
  }
  function state(status, phase = 'final_answer') {
    context.applyTaskText('task', {kind: 'text_state', text_id: 'text',
      text_status: status, phase}, 'chat');
  }
  return {context, conversation, nodes, tick, drain, delta, state, mount,
    get rebuilds() { return rebuilds; }};
}

test('a large chunk unfolds across frames and further deltas reuse the existing text node', () => {
  const s = setup();
  s.delta('这是一段用来验证逐字显示而不是整块刷新出来的模型回答');
  const node = s.nodes.get('text'), message = s.conversation.messages[0];
  assert.equal(node.content.textContent, '');
  s.tick(); s.tick();
  const partial = node.content.textContent;
  assert.ok(partial.length > 0 && partial.length < message.content.length);
  const textNode = node.content.firstChild;
  s.delta('后续内容', 2);
  s.tick(); s.tick();
  assert.equal(s.rebuilds, 1, 'text deltas must not rebuild the conversation');
  assert.equal(node.content.firstChild, textNode, 'append to the existing DOM text node');
  assert.equal(s.nodes.get('text'), node);
  s.drain();
  assert.equal(node.content.textContent, message.content);
});

test('emoji and combining characters appear as complete graphemes', () => {
  const s = setup();
  s.delta('👨‍👩‍👧‍👦e\u0301好');
  s.tick(); s.tick();
  assert.equal(s.nodes.get('text').content.textContent, '👨‍👩‍👧‍👦');
  s.tick();
  assert.equal(s.nodes.get('text').content.textContent, '👨‍👩‍👧‍👦e\u0301');
  s.drain();
  assert.equal(s.nodes.get('text').content.textContent, '👨‍👩‍👧‍👦e\u0301好');
});

test('final confirmation and history replacement retain the animation until all text is shown', () => {
  const s = setup();
  const text = '  **答案**会平滑显示完成，不因最终快照到达而突然展开整段。  ';
  s.delta(text);
  s.tick(); s.tick();
  s.context.appendTaskAssistantText('task', text.trim(), 'chat');
  const message = s.conversation.messages[0];
  const visible = message.textPresentation.visible;
  assert.ok(visible.length < message.content.length);
  // A history refresh transfers the presentation to its authoritative message.
  s.conversation.messages = [{...message}];
  s.mount();
  assert.equal(s.nodes.get('text').content.textContent, visible);
  s.drain();
  assert.equal(s.nodes.get('text').content.html, `<formatted>${text.trim()}</formatted>`);
  assert.equal(s.conversation.messages[0].textPresentation, undefined);
  assert.equal(s.conversation.messages.length, 1);
});

test('discard stops queued animation and reconnect displays the recovered text immediately', () => {
  const s = setup();
  s.delta('这段候选回答会被撤回，不能被动画重新显示。');
  s.tick(); s.state('discarded'); s.drain();
  assert.equal(s.conversation.messages.length, 0);
  s.context.applyTaskText('task', {text_id: 'restored', text: '已经收到的历史',
    text_sequence: 3, phase: 'final_answer'}, 'chat', true);
  assert.equal(s.nodes.get('restored').content.textContent, '已经收到的历史');
  s.drain();
});

test('committed commentary finishes its visible text before moving into the execution history', () => {
  const s = setup();
  s.delta('我先检查文件，再确认调用路径。', 1, 'commentary');
  s.state('committed', 'commentary');
  assert.equal(s.conversation.messages.length, 1);
  s.tick(); s.tick();
  assert.ok(s.nodes.get('text').content.textContent.length > 0);
  s.drain();
  assert.equal(s.conversation.messages.length, 0);
});

test('switching conversation pauses drawing and returning resumes from the visible prefix', () => {
  const s = setup();
  s.delta('切换会话时不要继续重绘旧页面，回来后继续显示未展开的内容。');
  s.tick(); s.tick();
  const prefix = s.conversation.messages[0].textPresentation.visible;
  s.context.activeConversationId = 'other';
  s.drain();
  assert.equal(s.conversation.messages[0].textPresentation.visible, prefix);
  s.context.activeConversationId = 'chat';
  s.mount(); s.drain();
  assert.equal(s.nodes.get('text').content.textContent, s.conversation.messages[0].content);
});
