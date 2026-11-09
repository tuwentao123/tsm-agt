const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function source(name) {
  let first = page.indexOf('function ' + name + '(');
  assert.ok(first >= 0, name + ' is missing from the rendered page');
  if (page.slice(first - 6, first) === 'async ') first -= 6;
  return page.slice(first, page.indexOf('\n}\n', first) + 2);
}

function setup(autoApprove) {
  const conversation = {id: 'chat', messages: [], activeTaskId: 'task', autoApprove};
  const requests = [], followed = [], errors = [];
  const ctx = vm.createContext({
    console: {error() {}}, activeConversationId: 'chat',
    crypto: {randomUUID: () => 'command'},
    clearError() {}, renderMessages() {}, setLoading() {},
    getConversationById: id => (id === 'chat' ? conversation : null),
    getActiveConversation: () => conversation,
    taskMessage: () => null,
    followTask: (...args) => followed.push(args),
    showError: error => errors.push(error),
    fetch: async (url, options) => {
      requests.push({url, decision: JSON.parse(options.body).decision});
      return {ok: true, json: async () => ({})};
    },
    document: {getElementById: () => null},
  });
  const functions = ['upsertApprovalMessage', 'decideApproval', 'autoApprovePendingApprovals'];
  vm.runInContext(functions.map(source).join('\n'), ctx);
  return {ctx, conversation, requests, followed, errors};
}

function settle() {
  return new Promise(resolve => setImmediate(resolve));
}

test('an opted-in conversation approves its pending request exactly once', async () => {
  const s = setup(true);
  s.ctx.upsertApprovalMessage('task', {request_id: 'approval-1', turn_id: 'turn-1'}, 'chat');
  assert.equal(s.ctx.autoApprovePendingApprovals('chat'), 1);
  assert.equal(s.ctx.autoApprovePendingApprovals('chat'), 0);
  await settle();
  assert.deepEqual(s.requests, [{url: '/approvals', decision: 'APPROVE'}]);
  assert.deepEqual(s.followed, [['task', 'chat']]);
  assert.deepEqual(s.errors, []);
});

test('a conversation without the switch keeps the approval manual', async () => {
  const s = setup(false);
  s.ctx.upsertApprovalMessage('task', {request_id: 'approval-1'}, 'chat');
  assert.equal(s.ctx.autoApprovePendingApprovals('chat'), 0);
  await settle();
  assert.deepEqual(s.requests, []);
  assert.deepEqual(s.followed, []);
});

test('a rejected automatic approval returns control without a toast and is not retried', async () => {
  const s = setup(true);
  const message = s.ctx.upsertApprovalMessage('task', {request_id: 'approval-1'}, 'chat');
  s.ctx.fetch = async () => ({ok: false, json: async () => ({detail: 'approval expired'})});
  s.ctx.autoApprovePendingApprovals('chat');
  await settle();
  assert.equal(message.submitting, false);
  assert.equal(message.autoApproveFailed, true);
  assert.deepEqual(s.errors, []);
  assert.equal(s.ctx.autoApprovePendingApprovals('chat'), 0);
  assert.deepEqual(s.followed, []);
});
