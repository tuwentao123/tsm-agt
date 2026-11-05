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

class Node {
  constructor(tag = 'div') {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.style = {};
    this.attributes = {};
    this.listeners = {};
    this.hidden = true;
    this.clientHeight = 600;
    this.scrollHeight = 6000;
    this.offsetHeight = 160;
    this._scrollTop = 0;
    const values = new Set();
    this.classList = {
      add: value => values.add(value),
      remove: value => values.delete(value),
      contains: value => values.has(value),
      toggle(value, force) {
        const on = force === undefined ? !values.has(value) : force;
        if (on) values.add(value); else values.delete(value);
        return on;
      },
    };
  }
  set scrollTop(value) { this._scrollTop = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  get scrollTop() { return this._scrollTop; }
  set innerHTML(value) { this.html = value; this.children = []; }
  appendChild(child) { this.children.push(child); return child; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  querySelector() { return null; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; if (name === 'src') delete this.src; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name, event = {}) { for (const callback of this.listeners[name] || []) callback(event); }
  focus() { this.focused = true; }
}

function setup() {
  const chat = new Node('section'), button = new Node('button'), dock = new Node('section');
  const modal = new Node(), target = new Node('img'), close = new Node('button');
  const previewList = new Node();
  const nodes = {'chat-area': chat, 'chat-jump-bottom': button, 'approval-dock': dock,
    'image-preview-modal': modal, 'image-preview-target': target,
    'image-preview-close': close, 'preview-list': previewList};
  const document = new Node(), window = new Node(), observers = [];
  document.activeElement = new Node('button');
  document.getElementById = id => nodes[id];
  document.createElement = tag => new Node(tag);
  const conversation = {id: 'chat', messages: [{role: 'user', content: '看这张图片',
    images: [{image_url: 'data:image/png;base64,test', name: '截图.png'}]}]};
  const context = vm.createContext({document, window, activeWorkspaceId: 'workspace',
    selectedImages: [], getActiveConversation: () => conversation,
    renderConversationTodo() {}, renderTracePane() {}, renderTurnCallPanel: () => null,
    renderMarkdown: text => `<p>${text}</p>`,
    ResizeObserver: class {
      constructor(callback) { this.callback = callback; this.observed = []; observers.push(this); }
      observe(node) { this.observed.push(node); }
    },
  });
  vm.runInContext(section('let shouldAutoFollowChat =', "document.addEventListener('click'"), context);
  vm.runInContext(section('function renderMessages(', 'function renderTracePane('), context);
  vm.runInContext(section('let imagePreviewReturnFocus =', 'const MAX_IMAGE_EDGE ='), context);
  function thumb() { return chat.children[0].children.find(item => item.className === 'message-images').children[0]; }
  function scroll(top) { chat.scrollTop = top; chat.dispatch('scroll'); }
  return {context, chat, button, dock, modal, target, close, document, window, observers, conversation,
    previewList, thumb, scroll};
}

test('the entire emitted script compiles', () => {
  assert.doesNotThrow(() => new vm.Script(page));
});

test('cold session reload renders real Task cards, later answers and durable timeline events', async () => {
  const s = setup();
  const trace = new Node('aside');
  const originalLookup = s.document.getElementById;
  s.document.getElementById = id => id === 'trace-pane' ? trace : originalLookup(id);
  const errors = [];
  Object.assign(s.context, {
    console: {error: error => errors.push(String(error))},
    activeConversationId: 'chat',
    defaultModelProfile: null, skillInstallInFlight: false,
    getConversationById: id => id === 'chat' ? s.conversation : null,
    renderConversations() {}, setLoading() {},
    followTask() { throw new Error('settled history must not restart a Task'); },
  });
  const banners = [];
  s.context.showError = message => banners.push(message);
  vm.runInContext('const taskDetailCollapseState = new Map();', s.context);
  vm.runInContext(section('const 状态文案 = {', 'const TERMINAL_STATES ='), s.context);
  vm.runInContext(section('const 风险文案 =', 'const TIMELINE_KIND_TEXT ='), s.context);
  vm.runInContext(section('function describeFailure(', 'async function handleCancelAction('), s.context);
  vm.runInContext(section('function renderTurnMarker(', 'function turnCallTarget('), s.context);
  vm.runInContext(section('const TIMELINE_KIND_TEXT = {', 'let skillInstallInFlight'), s.context);
  vm.runInContext(section('function renderTodoPanel(', 'function taskMessage('), s.context);
  vm.runInContext(section('function renderTracePane(', 'const 风险文案 ='), s.context);
  vm.runInContext(section('async function ensureConversationLoaded(', '// A finished Turn is only fully described'), s.context);
  const started = {kind: 'tool_started', sequence: 2, tool_name: 'core.read_file', tool_call_id: 'read'};
  const completed = {kind: 'tool_completed', sequence: 3, tool_call_id: 'read', ok: true};
  const history = {
    active_task_id: 'task',
    tasks: [{task_id: 'task', goal: 'Stored task', attached_sequence: 1,
      phase1_state: 'DONE', status: 'completed', progress_cursor: 30}],
    turns: [{task_id: 'task', turn_id: 'turn-one', settled: true, progress: [started, completed]},
      {task_id: 'task', turn_id: 'turn-two', settled: true, progress: []}],
    messages: [
      {role: 'user', content: 'First question', task_id: 'task', turn_id: 'turn-one', source_event_sequence: 1, first_turn_input: true, images: []},
      {role: 'assistant', content: 'First answer', task_id: 'task', turn_id: 'turn-one', source_event_sequence: 10},
      {role: 'user', content: 'Second question', task_id: 'task', turn_id: 'turn-two', source_event_sequence: 20, images: []},
      {role: 'assistant', content: 'Second answer', task_id: 'task', turn_id: 'turn-two', source_event_sequence: 30},
    ],
  };
  s.context.fetch = async () => ({ok: true, json: async () => history});
  assert.equal(await s.context.ensureConversationLoaded('chat'), true, errors.join('\n'));
  assert.deepEqual(errors, []);
  assert.equal(s.chat.children.filter(node => node.className === 'message task').length, 1);
  const answers = s.chat.children.filter(node => node.className === 'message assistant');
  assert.equal(answers.length, 2);
  assert.match(answers[1].children[0].html, /Second answer/);
  let events = trace.children[1];
  assert.equal(events.className, 'trace-timeline-list');
  assert.equal(events.children.filter(node => node.dataset.eventKey.includes(':2:tool_started')).length, 1);
  assert.equal(events.children.filter(node => node.dataset.eventKey.includes(':3:tool_completed')).length, 1);

  // Reconnection may deliver the same durable event into the live buffer.
  const task = s.conversation.messages.find(message => message.role === 'task').task;
  task.progress = [{sequence: 2, text: 'Read file', progressData: started}];
  s.context.renderTracePane(s.conversation);
  events = trace.children[1];
  assert.equal(events.children.filter(node => node.dataset.eventKey.includes(':2:tool_started')).length, 1);
  s.context.renderTracePane({messages: [], turnCalls: new Map()});
  assert.equal(trace.children[1].className, 'trace-timeline-caption');

  const existingMessages = s.conversation.messages;
  s.conversation.loaded = false;
  s.context.fetch = async () => ({ok: false, status: 503});
  assert.equal(await s.context.ensureConversationLoaded('chat'), false);
  assert.equal(s.conversation.messages, existingMessages);
  assert.match(banners[0], /历史加载失败.*503/);
});

test('three viewport heights is the exact jump threshold; short or hidden chats have no arrow', () => {
  const s = setup();
  s.context.renderMessages(true);
  assert.equal(s.button.hidden, true);
  s.scroll(3601); // 1799px left, just below three screens.
  assert.equal(s.button.hidden, true);
  s.scroll(3600);
  assert.equal(s.button.hidden, false);
  s.scroll(3599);
  assert.equal(s.button.hidden, false);
  s.chat.clientHeight = 1000;
  s.window.dispatch('resize');
  assert.equal(s.button.hidden, true, 'threshold follows viewport size');
  s.chat.scrollHeight = 500;
  s.context.updateChatJumpButton(s.chat);
  assert.equal(s.button.hidden, true);
  s.chat.clientHeight = 0;
  s.context.updateChatJumpButton(s.chat);
  assert.equal(s.button.hidden, true);
});

test('clicking jump returns to newest content and subsequent messages follow the bottom', () => {
  const s = setup();
  s.context.renderMessages(true);
  s.scroll(1000);
  assert.equal(s.button.hidden, false);
  assert.equal(vm.runInContext('shouldAutoFollowChat', s.context), false);
  s.context.jumpToChatBottom();
  assert.equal(s.chat.scrollTop, 5400);
  assert.equal(s.button.hidden, true);
  assert.equal(vm.runInContext('shouldAutoFollowChat', s.context), true);
  s.chat.scrollHeight += 900;
  s.conversation.messages.push({role: 'assistant', content: '新回复'});
  s.context.renderMessages();
  assert.equal(s.chat.scrollTop, 6300);
  assert.equal(s.button.hidden, true);
});

test('message rebuilds preserve a historical reading position and the arrow', () => {
  const s = setup();
  s.context.renderMessages(true);
  s.scroll(800);
  s.chat.scrollHeight += 700;
  s.context.renderMessages();
  assert.equal(s.chat.scrollTop, 800);
  assert.equal(s.button.hidden, false);
  assert.equal(s.chat.listeners.scroll.length, 1, 'do not bind listeners on every render');
  assert.equal(s.window.listeners.resize.length, 1);
  assert.equal(s.observers.length, 1);
  assert.deepEqual(s.observers[0].observed, [s.chat, s.dock]);
});

test('late image layout and dock resize update the arrow without hijacking historical scrolling', () => {
  const s = setup();
  s.context.renderMessages(true);
  s.scroll(3650);
  assert.equal(s.button.hidden, true);
  s.chat.scrollHeight += 300;
  s.chat.dispatch('load');
  assert.equal(s.chat.scrollTop, 3650);
  assert.equal(s.button.hidden, false);
  s.dock.classList.add('visible');
  s.observers[0].callback();
  assert.equal(s.button.style.bottom, '182px', 'arrow sits above the approval dock');
  s.context.jumpToChatBottom();
  s.chat.scrollHeight += 100;
  s.chat.dispatch('load');
  assert.equal(s.chat.scrollTop, 5800, 'image loading follows only if user was already at bottom');
});

test('streamed text growth updates the arrow and follows only after jumping', () => {
  const s = setup(), frames = [];
  s.context.renderMessages(true);
  s.scroll(3650);
  assert.equal(s.button.hidden, true);
  const message = {role: 'assistant', content: 'a'.repeat(200),
    textStream: {status: 'generating'},
    textPresentation: {id: 'stream', visible: '', frame: null, lastTime: null, credit: 0}};
  s.conversation.messages.push(message);
  const body = new Node(), element = new Node(), label = new Node();
  Object.defineProperty(body, 'textContent', {
    get() { return this.text || ''; },
    set(value) { this.text = value; s.chat.scrollHeight += 100; },
  });
  element.dataset.textStreamId = 'stream';
  element.querySelector = selector => selector === '.message-body' ? body : label;
  s.chat.querySelectorAll = () => [element];
  s.context.activeConversationId = 'chat';
  s.context.getConversationById = () => s.conversation;
  s.context.requestAnimationFrame = callback => { frames.push(callback); return frames.length; };
  vm.runInContext(section('const textGraphemes =', 'function clearTaskTextStreams('), s.context);
  s.context.scheduleTaskTextRender(message, 'chat');
  frames.shift()(16);
  assert.ok(body.textContent.length > 0, 'execute the actual stream painter');
  assert.equal(s.chat.scrollTop, 3650, 'stream growth preserves the historical position');
  assert.equal(s.button.hidden, false, 'crossing three screens reveals the arrow');
  s.context.jumpToChatBottom();
  assert.equal(s.button.hidden, true);
  frames.shift()(32);
  assert.equal(s.chat.scrollTop, s.chat.scrollHeight - s.chat.clientHeight);
  assert.equal(s.button.hidden, true, 'subsequent stream frames continue following the bottom');
});

test('empty session and workspace switches clear a stale arrow', () => {
  const s = setup();
  s.context.renderMessages(true);
  s.scroll(1000);
  s.conversation.messages = [];
  s.context.renderMessages(true);
  assert.equal(s.button.hidden, true);
  vm.runInContext('activeWorkspaceId = null', s.context);
  s.context.renderMessages(true);
  assert.equal(s.button.hidden, true);
});

test('sent and reloaded image thumbnails open an in-page preview and close by button/backdrop/Escape', () => {
  const s = setup();
  s.context.renderMessages(true);
  let image = s.thumb();
  assert.equal(image.attributes.role, 'button');
  assert.equal(image.tabIndex, 0);
  s.document.activeElement = image;
  image.onclick();
  assert.equal(s.modal.classList.contains('visible'), true);
  assert.equal(s.modal.attributes['aria-hidden'], 'false');
  assert.equal(s.target.src, 'data:image/png;base64,test');
  assert.equal(s.target.alt, '截图.png');
  assert.equal(s.close.focused, true);
  s.document.dispatch('click', {target: {id: 'image-preview-target'}});
  assert.equal(s.modal.classList.contains('visible'), true, 'image itself does not dismiss');
  s.document.dispatch('click', {target: {id: 'image-preview-close'}});
  assert.equal(s.modal.classList.contains('visible'), false);
  assert.equal(s.modal.attributes['aria-hidden'], 'true');
  assert.equal(s.target.src, undefined);
  assert.equal(image.focused, true, 'focus returns to the thumbnail');
  s.context.renderMessages(true); // The same renderer rebuilds historical images.
  image = s.thumb();
  image.onclick();
  s.document.dispatch('click', {target: {id: 'image-preview-modal'}});
  assert.equal(s.modal.classList.contains('visible'), false);
  for (const key of ['Enter', ' ']) {
    let prevented = false;
    image.onkeydown({key, preventDefault() { prevented = true; }});
    assert.equal(prevented, true);
    assert.equal(s.modal.classList.contains('visible'), true);
    s.document.dispatch('keydown', {key: 'Tab', preventDefault() {}});
    assert.equal(s.modal.classList.contains('visible'), true);
    s.document.dispatch('keydown', {key: 'Escape', preventDefault() {}});
    assert.equal(s.modal.classList.contains('visible'), false);
  }
});

function setupHistory(messages, previous = []) {
  const s = setup();
  s.conversation.messages = previous;
  s.conversation.loaded = false;
  const data = {messages, tasks: [], turns: [], active_task_id: null};
  Object.assign(s.context, {
    defaultModelProfile: 'default', console,
    getConversationById: () => s.conversation,
    renderConversations() {}, renderTurnMarker: () => new Node(),
    fetch: async () => ({ok: true, json: async () => data}),
  });
  vm.runInContext(section('async function ensureConversationLoaded(', 'async function loadWorkspaces('), s.context);
  return s;
}

function historyRow(turn, images) {
  const row = {role: 'user', content: '看图', task_id: 'task', turn_id: turn,
    source_event_sequence: turn === 'one' ? 1 : 2};
  if (images !== undefined) row.images = images;
  return row;
}

function thumbnails(s) {
  return s.chat.children.flatMap(element => element.children
    .filter(child => child.className === 'message-images')
    .flatMap(gallery => gallery.children));
}

test('cold history loads images by Turn, renders previews and retains them after refresh', async () => {
  const first = 'data:image/png;base64,first', second = 'data:image/png;base64,second';
  const s = setupHistory([
    historyRow('one', [{image_url: first}]),
    historyRow('two', [{image_url: second}]),
  ]);
  assert.equal(await s.context.ensureConversationLoaded('chat'), true);
  assert.deepEqual(thumbnails(s).map(image => image.src), [first, second]);
  thumbnails(s)[1].onclick();
  assert.equal(s.target.src, second);
  assert.equal(s.modal.classList.contains('visible'), true);
  assert.equal(await s.context.refreshConversationHistory('chat'), true);
  assert.deepEqual(thumbnails(s).map(image => image.src), [first, second]);
});

test('an explicit empty history attachment list cannot inherit a local image', async () => {
  const s = setupHistory([historyRow('one', []), historyRow('two', [])], [
    {role: 'user', content: '看图', images: [{image_url: 'data:image/png;base64,local'}]},
  ]);
  assert.equal(await s.context.ensureConversationLoaded('chat'), true);
  assert.equal(thumbnails(s).length, 0);
});

test('old-server local recovery uses Turn ids or the newest identical input', async () => {
  const local = {image_url: 'data:image/png;base64,local'};
  for (const ids of [{taskId: 'task', turnId: 'two'}, {}]) {
    const s = setupHistory([historyRow('one'), historyRow('two')], [
      {role: 'user', content: '看图', images: [local], ...ids},
    ]);
    assert.equal(await s.context.ensureConversationLoaded('chat'), true);
    const rows = s.conversation.messages;
    assert.equal(rows[0].images.length, 0);
    assert.equal(rows[1].images[0].image_url, local.image_url);
    assert.equal(thumbnails(s).length, 1);
    assert.equal(await s.context.refreshConversationHistory('chat'), true);
    assert.equal(s.conversation.messages[0].images.length, 0);
    assert.equal(s.conversation.messages[1].images[0].image_url, local.image_url);
  }
});

test('composer attachment preview continues to use the shared modal', () => {
  const s = setup();
  s.context.selectedImages.push({image_url: 'data:image/jpeg;base64,pending', name: '待发送.jpg'});
  s.context.renderPreviews();
  const image = s.previewList.children[0].children[0];
  image.onclick();
  assert.equal(s.target.src, 'data:image/jpeg;base64,pending');
  assert.equal(s.modal.classList.contains('visible'), true);
});
