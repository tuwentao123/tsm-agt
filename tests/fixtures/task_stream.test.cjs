const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const constants = page.slice(page.indexOf('const TERMINAL_STATES ='), page.indexOf('function appendTaskAssistantText'));
const follower = page.slice(page.indexOf('function followTask(taskId, conversationId)'), page.indexOf('async function loadSessions()'));

function setup({realFinal = false} = {}) {
  const streams = [], timers = [], requests = [], finishes = [], histories = [], answers = [];
  const cards = new Map();
  const card = id => {
    if (!cards.has(id)) cards.set(id, {task: {cursor: 0, progress: []}});
    return cards.get(id);
  };
  const context = vm.createContext({
    console, activeConversationId: 'chat',
    EventSource: class {
      constructor(url) { this.url = url; streams.push(this); }
      close() { this.closed = true; }
      emit(payload) { this.onmessage({data: JSON.stringify(payload)}); }
    },
    setTimeout: (callback, delay) => { const timer = {callback, delay}; timers.push(timer); return timer; },
    clearTimeout: timer => { timer.cancelled = true; },
    taskMessage: (chat, id) => card(id), upsertTaskCard: (chat, raw) => card(raw.task_id),
    appendTaskProgress: (id, packet) => card(id).task.progress.push(packet),
    applyTaskProjection: (task, projection) => { task.projection = projection; },
    applyTaskProtocol() {}, renderSkillInstallations() {}, bindLatestInputToTask() {},
    upsertApprovalMessage: (id, approval) => { card(id).task.approval = approval; },
    clearApprovalMessage: id => { card(id).task.approval = null; },
    renderMessages() {}, setLoading() {},
    appendTaskAssistantText: (id, text) => { answers.push(text); },
    getConversationById: () => ({messages: []}), updateComposerButtonState() {},
    renderFinalResult: async (id, state) => { finishes.push({id, state}); },
    refreshConversationHistory: async id => { histories.push(id); return true; },
    fetch: async url => { requests.push(url); return {ok: true, json: async () => ({status: 'running', phase1_state: 'RUNNING'})}; },
  });
  const finalRenderer = page.slice(page.indexOf('async function renderFinalResult('), page.indexOf('// Each follower owns exactly one Task card.'));
  vm.runInContext(constants + (realFinal ? finalRenderer : '') + follower, context);
  async function emit(source, payload) {
    const current = vm.runInContext("followers.get('task')", context);
    source.emit(payload);
    if (current) await current.queue;
  }
  return {context, streams, timers, requests, finishes, histories, answers, card, emit};
}

test('healthy SSE updates tools, Todo and final answer without a status GET or interval', async () => {
  const s = setup();
  s.context.followTask('task', 'chat');
  const stream = s.streams[0];
  await s.emit(stream, {type: 'stream.open', active_turn_id: 'turn-1', active_turn_sequence: 10});
  const tool = {type: 'tool.requested', sequence: 11, turn_id: 'turn-1', progress: {kind: 'tool_started'}};
  await s.emit(stream, tool);
  await s.emit(stream, tool);
  await s.emit(stream, {type: 'task.snapshot', cursor: 12, turn_id: 'turn-1', status: 'running', phase1_state: 'RUNNING', todo_list: {revision: 2}});
  assert.equal(s.card('task').task.progress.length, 1);
  assert.equal(s.card('task').task.todoList.revision, 2);
  await s.emit(stream, {type: 'task.snapshot', cursor: 13, turn_id: 'turn-1', status: 'awaiting_input', phase1_state: 'WAITING', assistant_text: 'Done'});
  assert.equal(stream.closed, true);
  assert.equal(s.finishes.length, 1);
  assert.equal(s.requests.length, 0);
  assert.equal(s.timers.length, 0);
});

test('awaiting_input completion preserves the delivered answer across a stale history refresh', async () => {
  const s = setup({realFinal: true});
  s.context.followTask('task', 'chat');
  const stream = s.streams[0];
  await s.emit(stream, {type: 'stream.open', active_turn_id: 'turn-1', active_turn_sequence: 10});
  await s.emit(stream, {type: 'task.snapshot', cursor: 20, turn_id: 'turn-1',
    status: 'awaiting_input', phase1_state: 'WAITING', assistant_text: 'Saved final answer'});
  assert.equal(stream.closed, true);
  assert.equal(s.histories.length, 1);
  // The history fixture omits the answer: the final renderer must restore it.
  assert.deepEqual(s.answers, ['Saved final answer', 'Saved final answer']);
});

test('durable turn panels receive tool and transport progress while the volatile panel is hidden', () => {
  const turn = {progress: [{sequence: 10, kind: 'turn_started'}]};
  const prior = {progress: []};
  const card = {task: {cursor: 10, streamTurnId: 'new', progress: []}};
  const conversation = {messages: [], turnCalls: new Map([['new', turn], ['old', prior]])};
  const ctx = vm.createContext({taskMessage: () => card,
    getConversationById: () => conversation, describeProgress: p => p.kind,
    scheduleProgressRender() {}});
  vm.runInContext(page.slice(page.indexOf('function appendTaskProgress('), page.indexOf('function renderApprovalCard(')), ctx);
  for (const [sequence, kind] of [[11, 'model_completed'], [12, 'tool_started'], [13, 'tool_completed']]) {
    ctx.appendTaskProgress('task', {sequence, turn_id: 'new', progress: {kind, tool_name: 'core.read_file'}}, 'chat');
  }
  ctx.appendTaskProgress('task', {sequence: 13, turn_id: 'new', progress: {kind: 'tool_completed'}}, 'chat');
  assert.deepEqual(turn.progress.map(p => p.kind), ['turn_started', 'model_completed', 'tool_started', 'tool_completed']);
  assert.equal(prior.progress.length, 0);
});

test('an older history response cannot erase the newest completed turn or reopen its stream', async () => {
  const pending = [];
  const conversation = {loaded: false, messages: []};
  const ctx = vm.createContext({console, activeConversationId: 'chat',
    getConversationById: () => conversation,
    fetch: () => new Promise(resolve => pending.push(resolve)),
    renderConversations() {}, renderMessages() {}, setLoading() {},
    followTask() { throw new Error('Stale history reopened a stream'); },
  });
  vm.runInContext(page.slice(page.indexOf('async function ensureConversationLoaded('), page.indexOf('// A finished Turn is only fully described')), ctx);
  const first = ctx.ensureConversationLoaded('chat');
  conversation.loaded = false;
  const second = ctx.ensureConversationLoaded('chat');
  pending[1]({ok: true, json: async () => ({messages: [{role: 'assistant', content: 'Final answer', source_event_sequence: 20}]})});
  await second;
  pending[0]({ok: true, json: async () => ({messages: [{role: 'user', content: 'Question', source_event_sequence: 10}],
    active_task_id: 'task', tasks: [{task_id: 'task', phase1_state: 'RUNNING'}]})});
  await first;
  assert.equal(conversation.messages.length, 1);
  assert.equal(conversation.messages[0].content, 'Final answer');
  assert.equal(conversation.loaded, true);
});

test('an interrupted task keeps its unanswered Turn trace anchored under the user input', async () => {
  const conversation = {loaded: false, messages: []};
  const ctx = vm.createContext({console, getConversationById: () => conversation,
    TERMINAL_STATES: ['INTERRUPTED', 'DONE'],
    fetch: async () => ({ok: true, json: async () => ({active_task_id: 'task',
      tasks: [{task_id: 'task', phase1_state: 'INTERRUPTED'}],
      turns: [{task_id: 'task', turn_id: 'turn', settled: false, progress: [{kind: 'turn_interrupted'}]}],
      messages: [{role: 'user', content: 'Fix it', task_id: 'task', turn_id: 'turn', source_event_sequence: 10}],
    })}), renderConversations() {}, renderMessages() {},
  });
  vm.runInContext(page.slice(page.indexOf('async function ensureConversationLoaded('), page.indexOf('// A finished Turn is only fully described')), ctx);
  await ctx.ensureConversationLoaded('chat');
  assert.equal(conversation.turnCalls.get('turn').settled, false);
  assert.equal(conversation.turnCalls.get('turn').progress[0].kind, 'turn_interrupted');
});

test('old turn replay cannot overwrite or complete a new turn', async () => {
  const s = setup();
  s.context.followTask('task', 'chat');
  const stream = s.streams[0];
  await s.emit(stream, {type: 'stream.open', active_turn_id: 'new', active_turn_sequence: 100});
  await s.emit(stream, {type: 'turn.started', turn_id: 'old', sequence: 20, progress: {kind: 'turn_started'}});
  await s.emit(stream, {type: 'turn.completed', turn_id: 'old', sequence: 30, progress: {kind: 'turn_completed'}});
  await s.emit(stream, {type: 'task.snapshot', turn_id: 'old', cursor: 30, final: true, phase1_state: 'DONE'});
  assert.equal(s.finishes.length, 0);
  assert.equal(s.card('task').task.progress.length, 0);
  await s.emit(stream, {type: 'tool.requested', turn_id: 'new', sequence: 101, progress: {kind: 'tool_started'}});
  assert.equal(s.card('task').task.progress.length, 1);
  assert.equal(stream.closed, undefined);
});

test('reconnect resumes the durable cursor and ignores callbacks from replaced listeners', async () => {
  const s = setup();
  s.context.followTask('task', 'chat');
  const old = s.streams[0];
  await s.emit(old, {type: 'stream.open', active_turn_id: 'one', active_turn_sequence: 1});
  await s.emit(old, {type: 'tool.requested', turn_id: 'one', sequence: 25, progress: {kind: 'tool_started'}});
  old.onerror();
  assert.equal(s.timers[0].delay, 1000);
  s.timers.shift().callback();
  assert.match(s.streams[1].url, /after=25$/);
  s.context.followTask('task', 'chat');
  await s.emit(old, {type: 'task.snapshot', final: true, phase1_state: 'DONE'});
  assert.equal(s.finishes.length, 0);
  assert.equal(s.requests.length, 0);
});

test('reset drops an invalid cursor while transient fragments never advance it', async () => {
  const s = setup();
  s.context.followTask('task', 'chat');
  const stream = s.streams[0];
  s.card('task').task.cursor = 999;
  await s.emit(stream, {type: 'stream.reset_required', sequence: 0});
  assert.equal(s.card('task').task.cursor, 0);
  await s.emit(stream, {type: 'stream.open', active_turn_id: 'new', active_turn_sequence: 1});
  await s.emit(stream, {type: 'progress.transient', turn_id: 'new', progress: {kind: 'reasoning_delta'}});
  assert.equal(s.card('task').task.cursor, 0);
});

test('approval snapshot stops cleanly and retains the actionable request', async () => {
  const s = setup();
  s.context.followTask('task', 'chat');
  const stream = s.streams[0];
  await s.emit(stream, {type: 'stream.open', active_turn_id: 'one'});
  await s.emit(stream, {type: 'task.snapshot', turn_id: 'one', status: 'awaiting_approval', phase1_state: 'WAITING', approval: {request_id: 'approve-1'}, waiting: {kind: 'APPROVAL'}});
  assert.equal(stream.closed, true);
  assert.equal(s.card('task').task.approval.request_id, 'approve-1');
  assert.equal(s.requests.length, 0);
});

test('only exhausted reconnects perform one recovery status read', async () => {
  const s = setup();
  s.context.followTask('task', 'chat');
  for (let attempt = 0; attempt < 6; attempt++) {
    s.streams.at(-1).onerror();
    assert.equal(s.requests.length, 0);
    s.timers.shift().callback();
  }
  s.streams.at(-1).onerror();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(s.requests, ['/tasks/task']);
});
