// Run the rendered page's slash-menu resolvers for real.
//
// Typing "/ph" must list discovered Skills on its own, and choosing one must
// replace the "/ph" trigger text instead of leaving it in front of the mention.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

const pageScript = fs.readFileSync(process.env.TSM_SKILL_TEST_SCRIPT, 'utf8');

function slice(startMarker, endMarker) {
  const start = pageScript.indexOf(startMarker);
  const end = pageScript.indexOf(endMarker);
  assert.ok(
    start >= 0 && end > start,
    `${startMarker} must stay above ${endMarker} in the page script`,
  );
  return pageScript.slice(start, end);
}

// Menu resolution: command table, Skill directory cache and both resolvers.
const menuScript = slice('const slashCommands = [', 'function ensureSlashMenu() {');
// Selection: command prefix filling and the Skill mention sentence.
const applyScript = slice('function skillMentionText(name) {', '// 等页面 DOM 就绪后再绑定到真正的输入框');

globalThis.promptComposer = null;
globalThis.persistActiveDraft = () => {};
globalThis.hideSlashMenu = () => {};
globalThis.renderSlashMenu = () => {};
vm.runInThisContext(menuScript, {filename: 'page.js'});
vm.runInThisContext(applyScript, {filename: 'page.js'});

const skills = [];
for (let index = 0; index < 10; index += 1) {
  skills.push({name: `ph-skill-${index}`, description: `第 ${index} 个 ph Skill`, source: 'user'});
}
skills.push({name: 'verify-change', description: '验证改动', source: 'builtin'});
skills.push({name: 'xph-legacy-tool', description: '名称中段包含 ph', source: 'user'});
skills.push({name: 'ios-build-error-fix', description: 'iOS 构建报错', source: 'user'});

globalThis.fetch = async () => ({ok: true, json: async () => ({skills})});

test('/ph lists matching Skills without a "/skill" prefix', async () => {
  await globalThis.loadSkillMenuSkills();
  const items = globalThis.resolveSlashMenuItems('/ph');

  assert.equal(items.length, 8, 'the menu caps direct Skill matches at 8 entries');
  assert.ok(items.every(entry => entry.kind === 'skill'));
  assert.ok(
    items.every(entry => entry.label.startsWith('ph-')),
    'only name-prefix matches are shown before the cap applies',
  );
  assert.equal(items[0].label, 'ph-skill-0');
  assert.equal(items[0].badge, '用户目录');
});

test('a bare "/" still lists only commands', async () => {
  await globalThis.loadSkillMenuSkills();
  const items = globalThis.resolveSlashMenuItems('/');

  assert.ok(items.length > 0);
  assert.ok(items.every(entry => entry.kind === 'command'));
});

test('command prefixes keep matching next to Skill entries', async () => {
  await globalThis.loadSkillMenuSkills();
  const items = globalThis.resolveSlashMenuItems('/s');
  const labels = items.map(entry => entry.label);

  // 命令排在 Skill 前面，方向键默认停在命令上。
  assert.deepEqual(labels.slice(0, 2), ['/steer <补充要求>', '/skill <名称>']);
  assert.equal(items[0].kind, 'command');
  // 名称中段包含关键词的 Skill 也进入菜单，但受同一个 8 条上限约束。
  assert.equal(items.length, 10);
  assert.ok(items.slice(2).every(entry => entry.kind === 'skill'));
  assert.equal(items[2].label, 'ph-skill-0');
});

test('the "/skill" prefix still lists every discovered Skill', async () => {
  await globalThis.loadSkillMenuSkills();
  const items = globalThis.resolveSlashMenuItems('/skill');

  assert.deepEqual(items.map(entry => entry.label), skills.map(skill => skill.name));
});

test('choosing a Skill replaces the "/ph" trigger text', async () => {
  await globalThis.loadSkillMenuSkills();
  const input = {
    value: '/ph',
    selectionStart: 3,
    selectionEnd: 3,
    focus() {},
  };
  const entry = globalThis.resolveSlashMenuItems('/ph')[0];

  globalThis.applySlashMenuItem(entry, input);

  assert.equal(
    input.value,
    '使用 Skill「ph-skill-0」：先用 skill.load 读取 SKILL.md，再按其中的步骤执行。',
  );
  assert.equal(input.selectionStart, input.value.length);
  assert.equal(input.selectionEnd, input.value.length);
});

test('a mid-sentence slash token is left alone when the mention is inserted', async () => {
  await globalThis.loadSkillMenuSkills();
  const input = {
    value: '请先看 ',
    selectionStart: 4,
    selectionEnd: 4,
    focus() {},
  };

  globalThis.applySkillMention({name: 'verify-change'}, input);

  assert.equal(
    input.value,
    '请先看 使用 Skill「verify-change」：先用 skill.load 读取 SKILL.md，再按其中的步骤执行。',
  );
});
