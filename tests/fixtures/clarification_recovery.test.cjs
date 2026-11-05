const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const helpers = page.slice(page.indexOf('function syncClarificationMessage('), page.indexOf('function upsertTaskCard('));
function setup(statuses = ['resumed']) {
  const conversation = {messages: []};
  const requests = [], followed = [], errors = [];
  const context = vm.createContext({
    activeConversationId: 'chat', console,
    getConversationById: () => conversation,
    document: {createElement: () => ({children: [], classList: {add() {}},
      append(...items) {this.children.push(...items);}, appendChild(item) {this.children.push(item);}})},
    crypto: {randomUUID: () => 'command'}, AbortSignal: {timeout: () => null},
    clearError() {}, renderMessages() {}, setLoading() {},
    showError: error => errors.push(error), refreshConversationHistory: async () => {},
    followTask: (...args) => followed.push(args), setTimeout: callback => callback(),
    fetch: async (url, options) => {
      requests.push({url, options});
      return {ok: true, json: async () => url === '/clarifications'
        ? {accepted: true} : {status: statuses.shift()}};
    },
  });
  vm.runInContext(helpers, context);
  const request = {request_id: 'unknown', kind: 'OUTCOME_RECONCILIATION', question: 'Check actual state',
    choices: [{value: 'CONFIRM_SUCCEEDED', label: 'Confirmed succeeded'},
      {value: 'CONFIRM_NOT_APPLIED', label: 'Confirmed not applied'}, {value: 'KEEP_BLOCKED', label: 'Keep waiting'}]};
  context.syncClarificationMessage('task', request, 'chat');
  return {context, conversation, requests, followed, errors, request};
}
test('reconciliation buttons send an explicit choice and follow only after durable handoff', async () => {
  const s = setup(['pending', 'resumed']);
  const message = s.conversation.messages[0];
  const card = s.context.document.createElement('div');
  s.context.renderClarificationCard(card, message);
  const actions = card.children[2];
  assert.equal(actions.children.length, 3);
  await actions.children[1].onclick();
  const body = JSON.parse(s.requests[0].options.body);
  assert.equal(body.selected_choice, 'CONFIRM_NOT_APPLIED');
  assert.equal(body.request_id, 'unknown');
  assert.equal(body.task_id, 'task');
  assert.equal(body.resume_token, undefined);
  assert.equal(s.requests.length, 3);
  assert.deepEqual(s.followed, [['task', 'chat']]);
  assert.equal(s.conversation.messages.length, 0);
});
test('keep waiting stays answerable; failed submissions restore controls', async () => {
  const s = setup(['waiting']);
  const message = s.conversation.messages[0];
  await s.context.answerClarification(message, {selected_choice: 'KEEP_BLOCKED'});
  assert.equal(s.followed.length, 0);
  assert.equal(message.submitting, false);
  assert.equal(s.conversation.messages.length, 1);
  s.context.fetch = async () => ({ok: false, json: async () => ({detail: 'stale request'})});
  await s.context.answerClarification(message, {selected_choice: 'CONFIRM_SUCCEEDED'});
  assert.deepEqual(s.errors, ['stale request']);
  assert.equal(message.submitting, false);
});
