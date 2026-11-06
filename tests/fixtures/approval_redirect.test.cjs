const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function source(name) {
  let first = page.indexOf(`function ${name}(`);
  assert.ok(first >= 0);
  if (page.slice(first - 6, first) === 'async ') first -= 6;
  return page.slice(first, page.indexOf('\n}\n', first) + 2);
}

function setup() {
  const conversation = {messages: [], activeTaskId: 'task'};
  const card = {task: {goal: 'original', waiting: {kind: 'APPROVAL'}}};
  const requests = [], followed = [], errors = [], bindings = [], final = [];
  const prompt = {value: 'new context'};
  const ctx = vm.createContext({
    console: {error() {}}, activeConversationId: 'chat', activeWorkspaceId: 'workspace',
    selectedImages: [], diagnoseNextInput: false, diagnosticSessionId: null,
    TERMINAL_STATES: ['INTERRUPTED', 'DONE', 'FAILED'],
    crypto: {randomUUID: () => 'command'}, clearError() {}, renderMessages() {}, setLoading() {},
    getConversationById: () => conversation, getActiveConversation: () => conversation,
    taskMessage: () => card, followTask: (...args) => followed.push(args),
    showError: error => errors.push(error),
    fetch: async (url, options) => {
      requests.push({url, body: JSON.parse(options.body)});
      return {ok: true};
    },
    document: {getElementById: id => id === 'prompt' ? prompt : {}},
    appendMessage: (role, content) => conversation.messages.push({role, content}),
    renderPreviews() {}, updateComposerButtonState() {}, removeBrowserStorage() {},
    draftStorageKey: () => 'draft',
    bindLatestInputToTask: (...args) => bindings.push(args),
    upsertTaskCard: (_, raw) => { card.task.phase1State = raw.phase1_state; return card; },
    renderFinalResult: (...args) => final.push(args),
  });
  vm.runInContext(['upsertApprovalMessage', 'clearApprovalMessage', 'decideApproval', 'runTask']
    .map(source).join('\n'), ctx);
  const message = ctx.upsertApprovalMessage('task', {request_id: 'old', turn_id: 'old-turn'}, 'chat');
  return {ctx, conversation, card, requests, followed, errors, message, bindings, final};
}

for (const decision of ['APPROVE', 'DENY']) {
  test(`${decision} follows background resolution and replaces a superseded approval dock`, async () => {
    const s = setup();
    await s.ctx.decideApproval(s.message, decision);
    assert.equal(s.requests[0].body.decision, decision);
    assert.deepEqual(s.followed, [['task', 'chat']]);
    assert.equal(s.message.submitting, true);
    const current = s.ctx.upsertApprovalMessage('task', {request_id: 'new', turn_id: 'old-turn'}, 'chat');
    assert.deepEqual(s.conversation.messages.map(item => item.approval.request_id), ['new']);
    assert.equal(Boolean(current.submitting), false);
  });
}

test('failed approval submission restores controls; a pending click is not submitted twice', async () => {
  const s = setup();
  let finish;
  let calls = 0;
  s.ctx.fetch = () => { calls += 1; return new Promise(resolve => { finish = resolve; }); };
  const first = s.ctx.decideApproval(s.message, 'DENY');
  await s.ctx.decideApproval(s.message, 'DENY');
  assert.equal(calls, 1);
  finish({ok: false, json: async () => ({detail: 'approval expired'})});
  await first;
  assert.equal(s.message.submitting, false);
  assert.deepEqual(s.errors, ['approval expired']);
  assert.equal(s.followed.length, 0);
});

test('accepted redirect follows its successor even while the receipt contains the interrupted predecessor', async () => {
  const s = setup();
  s.ctx.fetch = async () => ({ok: true, json: async () => ({
    kind: 'continuation', superseded_turn_id: 'old-turn',
    task: {task_id: 'task', state: 'INTERRUPTED', phase1_state: 'INTERRUPTED',
      status: 'interrupted', cursor: 42, approval: {request_id: 'old', turn_id: 'old-turn'}},
  })});
  await s.ctx.runTask();
  assert.deepEqual(s.errors, []);
  assert.deepEqual(s.followed, [['task', 'chat']]);
  assert.equal(s.final.length, 0);
  assert.equal(s.card.task.phase1State, 'PREPARING');
  assert.equal(s.card.task.waiting, null);
  assert.equal(s.bindings[0][2], null);
  assert.equal(s.conversation.messages.some(item => item.role === 'approval'), false);
});
