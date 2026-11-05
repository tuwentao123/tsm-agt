// Execute the real updateComposerButtonState() from the rendered page.
//
// A Turn that is stopped or interrupted never reaches a Session result, so its
// ``settled`` flag stays false. The cancel button must still disappear when the
// Task itself reports a terminal phase.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const pageScript = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const start = pageScript.indexOf('function updateComposerButtonState()');
const end = pageScript.indexOf('function updateWorkspaceState()');
assert.ok(
  start >= 0 && end > start,
  'updateComposerButtonState() must stay a top-level function in the page script',
);
const composerScript = pageScript.slice(start, end);

function makeButton() {
  const classes = new Set(['hidden']);
  return {
    dataset: {},
    disabled: false,
    classList: {
      add: name => classes.add(name),
      remove: name => classes.delete(name),
      contains: name => classes.has(name),
    },
  };
}

let scenario = {conversation: null, cards: {}, buttons: {}};

globalThis.document = {
  getElementById: id => scenario.buttons[id] || null,
};
globalThis.getActiveWorkspace = () => ({id: 'ws-1'});
globalThis.getActiveConversation = () => scenario.conversation;
globalThis.taskMessage = (conversationId, taskId) => scenario.cards[taskId] || null;
globalThis.TERMINAL_STATES = [
  'INTERRUPTED', 'DONE', 'FAILED', 'CANCELLED', 'NEEDS_REVIEW',
];
vm.runInThisContext(composerScript, {filename: 'page.js'});

function run(conversation, cards) {
  scenario = {
    conversation,
    cards: cards || {},
    buttons: {'send-btn': makeButton(), 'cancel-btn': makeButton()},
  };
  globalThis.updateComposerButtonState();
  return scenario.buttons['cancel-btn'];
}

function conversationWith(turnId, settled, taskId = 'task-1') {
  return {
    activeTaskId: taskId,
    turnCalls: new Map([[turnId, {task_id: taskId, turn_id: turnId, settled}]]),
  };
}

test('cancel stays visible while a turn is unsettled and the task runs', () => {
  const cancel = run(conversationWith('turn-1', false), {
    'task-1': {task: {phase1State: 'RUNNING', status: 'running'}},
  });
  assert.equal(cancel.classList.contains('hidden'), false);
  assert.equal(cancel.dataset.turnId, 'turn-1');
  assert.equal(cancel.disabled, false);
});

test('cancel hides after the task reaches a terminal phase without settling', () => {
  for (const phase of ['INTERRUPTED', 'DONE', 'FAILED', 'CANCELLED', 'NEEDS_REVIEW']) {
    const cancel = run(conversationWith('turn-1', false), {
      'task-1': {task: {phase1State: phase, status: 'stopped'}},
    });
    assert.equal(
      cancel.classList.contains('hidden'), true,
      `phase ${phase} must not keep the cancel button visible`,
    );
    assert.equal(cancel.dataset.turnId, '');
  }
});

test('a terminal snapshot from the previous turn does not hide cancel', () => {
  const cancel = run(conversationWith('turn-2', false), {
    'task-1': {
      task: {phase1State: 'DONE', status: 'done', settledTurnId: 'turn-1'},
    },
  });
  assert.equal(cancel.classList.contains('hidden'), false);
  assert.equal(cancel.dataset.turnId, 'turn-2');
});

test('cancel hides when there is no unsettled turn left', () => {
  const cancel = run(conversationWith('turn-1', true), {
    'task-1': {task: {phase1State: 'RUNNING', status: 'running'}},
  });
  assert.equal(cancel.classList.contains('hidden'), true);
});

test('cancel stays visible when the task card is missing', () => {
  const cancel = run(conversationWith('turn-1', false), {});
  assert.equal(cancel.classList.contains('hidden'), false);
});
