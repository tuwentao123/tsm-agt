const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');
const page = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');
const script = page.slice(page.indexOf('function setupWorkspaceResizer()'), page.indexOf('let activeConversationId = null;'));
function setup(stored = null) {
  const events = {}, attrs = {}, classes = new Set(), saves = [];
  let captured = null, resize;
  const stage = {style: {}, classList: {add: n => classes.add(n), remove: n => classes.delete(n)},
    getBoundingClientRect: () => ({left: 100, width: 1014})};
  const resizer = {style: {}, hidden: true, setAttribute: (k,v) => attrs[k] = v,
    addEventListener: (n,f) => events[n] = f,
    setPointerCapture: id => captured = id, hasPointerCapture: id => captured === id,
    releasePointerCapture: () => captured = null};
  const desktop = {matches: true, addEventListener: (_, f) => desktop.change = f};
  const context = vm.createContext({document: {querySelector: () => stage, getElementById: () => resizer},
    window: {matchMedia: () => desktop},
    readBrowserStorage: () => stored, writeBrowserStorage: (_,key,value) => saves.push(value),
    ResizeObserver: class {constructor(f) {resize = f;} observe() {}}});
  vm.runInContext(script + 'setupWorkspaceResizer();', context);
  const send = (name, data = {}) => events[name]({pointerId: 1, button: 0, preventDefault() {}, ...data});
  return {stage, resizer, attrs, classes, desktop, saves, send, resize: () => resize(), captured: () => captured};
}
test('pointer dragging changes real grid width, clamps both panes, persists and releases capture', () => {
  const s = setup();
  assert.equal(s.resizer.hidden, false);
  assert.equal(s.stage.style.gridTemplateColumns, '590px minmax(0, 1fr)');
  s.send('pointerdown');
  assert.equal(s.captured(), 1);
  s.send('pointermove', {clientX: 857});
  assert.equal(s.stage.style.gridTemplateColumns, '680px minmax(0, 1fr)');
  s.send('pointermove', {clientX: 0});
  assert.equal(s.stage.style.gridTemplateColumns, '320px minmax(0, 1fr)');
  assert.equal(s.attrs['aria-valuenow'], '32');
  s.send('pointerup');
  assert.equal(s.captured(), null);
  assert.equal(s.classes.has('resizing'), false);
  s.send('pointermove', {clientX: 600});
  assert.equal(s.stage.style.gridTemplateColumns, '320px minmax(0, 1fr)');
  assert.equal(s.saves.length, 2);
});
test('keyboard operation and stored split work', () => {
  const s = setup('0.4');
  assert.equal(s.stage.style.gridTemplateColumns, '400px minmax(0, 1fr)');
  s.send('keydown', {key: 'ArrowRight'});
  assert.equal(s.stage.style.gridTemplateColumns, '424px minmax(0, 1fr)');
  s.send('keydown', {key: 'End'});
  assert.equal(s.stage.style.gridTemplateColumns, '680px minmax(0, 1fr)');
  s.send('keydown', {key: 'Home'});
  assert.equal(s.stage.style.gridTemplateColumns, '320px minmax(0, 1fr)');
});
test('single column layout resets inline grid and cancels dragging, then restores the split', () => {
  const s = setup();
  s.send('pointerdown');
  s.desktop.matches = false;
  s.desktop.change();
  assert.equal(s.resizer.hidden, true);
  assert.equal(s.stage.style.gridTemplateColumns, '');
  assert.equal(s.captured(), null);
  s.desktop.matches = true;
  s.desktop.change();
  assert.equal(s.resizer.hidden, false);
  assert.equal(s.stage.style.gridTemplateColumns, '590px minmax(0, 1fr)');
});
test('cancelled pointers and non-primary mouse buttons do not keep resizing', () => {
  const s = setup('invalid');
  s.send('pointerdown', {button: 2});
  assert.equal(s.captured(), null);
  s.send('pointerdown');
  s.send('pointercancel');
  assert.equal(s.classes.has('resizing'), false);
  s.send('pointerdown');
  s.send('lostpointercapture');
  assert.equal(s.captured(), null);
});
