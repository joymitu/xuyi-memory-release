import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, writeFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { installRhythm, typingDelay } from './rhythm.js';
import { apply, archiveEvent, archiveGroupEvents, bubbleHint, createLogicLoader, dshPython, fallbackInterpreter, findProjectDir, logicFileOf, notePrompt, noteScopeKey, parseDiary, parseGroupLines, parseNoteLines, pickBubbleCount, recordFailure, resolveNoteRoute, resolvePeer, resolveRuntime, resolveScope, startBoardNotes, sweepBoardNotes, venvPython } from './index.js';

test('official mapping gates private chats and carries identity over a new session', t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-dsh-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefs = join(folder, 'prefs.json');
  const set = sessionIds => writeFileSync(prefs, JSON.stringify({ sessionIds }));
  set({ 'qqbot:a:c2c:p': 'old', 'qqbot:b:c2c:p': 'other', 'qqbot:a:group:g': 'group' });
  const peer = resolvePeer(prefs, 'old');
  assert.ok(peer);
  assert.notEqual(peer, resolvePeer(prefs, 'other'));
  assert.equal(resolvePeer(prefs, 'group'), null);
  assert.equal(resolvePeer(prefs, 'desktop'), null);
  set({ 'qqbot:a:c2c:p': 'new' });
  assert.equal(resolvePeer(prefs, 'new'), peer);
  assert.equal(resolvePeer(prefs, 'old'), null);
  set({ 'qqbot:a:c2c:p': 'new', 'qqbot:a:group:g': 'new' });
  assert.equal(resolvePeer(prefs, 'new'), null);
});

const user = (seq, text) => ({ type: 'user/message', surfaceOp: 'append', seq, time: 1794050000000 + seq,
  data: { role: 'user', source: { kind: 'user' }, content: [{ type: 'text', text }] } });

test('archive keeps the host text verbatim and skips projected or synthetic messages', () => {
  const event = user(2, '[Quoted message begins]\n先前的话\n[Current message]\n你好');
  assert.equal(archiveEvent('peer', 'session', event).text, event.data.content[0].text);
  assert.equal(archiveEvent('peer', 'session', event).origin, 'quoted');
  assert.equal(archiveEvent('peer', 'session', { ...event, surfaceOp: { op: 'replace' } }), null);
  assert.equal(archiveEvent('peer', 'session', { ...event, data: { ...event.data, source: { kind: 'runtime-context' } } }), null);
  assert.equal(archiveEvent('peer', 'session', { ...event, type: 'assistant/message',
    data: { message: { role: 'assistant', content: [{ type: 'text', text: '回复' }] } } }).text, '回复');
});

test('DSH-shaped events use the real Python archive and Chinese recall across sessions', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-dsh-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefs = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefs, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id,
    'qqbot:bot:c2c:another': 'other', 'qqbot:bot:group:room': 'group' } }));
  map('first');
  const handlers = {};
  let tool, options;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: () => {} },
    tools: { register: (spec, conditions) => { tool = spec; options = conditions; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'), prefsPath: prefs });
  const send = (id, event) => handlers['session/event']({ id }, event);
  const call = id => tool.execute({ query: '宠物叫什么？' }, { agent: { id } });
  assert.equal(options.when({ agent: { id: 'group' } }), true);   // 群聊现在也是已识别会话
  send('first', { type: 'turn/start' });
  send('first', user(1, '我的猫叫甲号。'));
  send('first', user(1, '我的猫叫甲号。'));
  send('group', user(1, '群聊秘密'));
  send('other', user(1, '别的私聊秘密'));
  assert.match(await call('first'), /甲号/);
  assert.match(await call('first'), /甲号/);          // 每轮可查两次
  assert.match(await call('first'), /已经查过两次/);
  send('first', { type: 'turn/end' });
  map('second');
  send('second', { type: 'turn/start' });
  const recall = await call('second');
  assert.match(recall, /甲号/);
  assert.match(recall, /unverified_current_highlight/);
  assert.doesNotMatch(recall, /秘密/);
  assert.equal((recall.match(/我的猫叫甲号/g) ?? []).length, 1);
  assert.ok(Buffer.byteLength(recall) <= 1600);
  assert.match(await call('first'), /只在已识别/);
  send('second', { type: 'turn/end' });
  send('second', { type: 'turn/start' });
  send('second', user(2, '更正一下，我的猫叫乙号。'));
  const corrected = await call('second');
  assert.match(corrected, /"unverified_current_highlight":\{[^}]*乙号/);
  assert.doesNotMatch(corrected, /甲号/);
  handlers.dispose();
});


test('automatic recall uses the current private turn, falls silent on misses and skips redundant tools', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-auto-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: {
    'qqbot:bot:c2c:person': id, 'qqbot:bot:c2c:another': 'other', 'qqbot:bot:group:room': 'group',
  } }));
  map('old');
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'), prefsPath, sessionSnapshot: false });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  const call = (id, query) => tool.execute({ query }, { agent: { id } });
  const contextFor = id => context.text({ agent: { id } });
  const prepare = async (id, text, decisionKind = 'enter') => {
    const message = user(3, text).data;
    const decision = { kind: decisionKind, messages: [message] };
    const actual = await handlers['agent/pre-step']({ agent: { id }, messages: [message],
      signal: new AbortController().signal }, async () => decision);
    assert.equal(actual, decision);
    return contextFor(id);
  };
  send('old', { type: 'turn/start' });
  send('old', user(1, '我的猫叫甲号。'));
  send('old', user(2, '更正一下，我的猫叫乙号。'));
  assert.match(await call('old', '宠物叫什么？'), /乙号/); // Wait until the real writes have completed.
  send('old', { type: 'turn/end' });

  map('new');
  send('new', { type: 'turn/start' });
  assert.equal(context.interpolate, false);
  assert.equal(contextFor('new'), '');
  assert.equal(await prepare('new', '宠物叫什么？', 'reject'), '');
  assert.equal(await prepare('new', '[Voice message] 宠物叫什么？'), '');
  assert.equal(await prepare('new', '哈哈，晚安'), '');
  assert.equal(await prepare('new', '我的宠物名字是甲号。'), '');
  assert.equal(await prepare('new', '给我的宠物起个名字吧。'), '');
  assert.equal(await prepare('new', '我的猫以前叫甲号吗？'), '');
  for (const id of ['other', 'group']) {
    send(id, { type: 'turn/start' });
    assert.equal(await prepare(id, '宠物叫什么？'), '');
  }
  const actual = await prepare('new', '宠物叫什么？');
  assert.match(actual, /乙号/);
  assert.match(actual, /更正一下/);
  assert.doesNotMatch(actual, /甲号/);
  assert.ok(Buffer.byteLength(actual) <= 900);
  // 自动重点算一次，工具还能再查一次（仍是新值），第三次才拦。
  assert.match(await call('new', '宠物叫什么？'), /乙号/);
  assert.match(await call('new', '宠物叫什么？'), /已经查过两次/);

  send('new', { type: 'turn/end' });
  send('new', { type: 'turn/start' });
  assert.equal(contextFor('new'), '');
  assert.equal(await prepare('new', '我的猫以前叫甲号吗？'), '');
  assert.match(await call('new', '我的猫以前叫甲号吗？'), /甲号/);
});

test('automatic recall covers preference and appointment questions', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-auto-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id } }));
  map('s1');
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'), prefsPath, sessionSnapshot: false });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  const call = (id, query) => tool.execute({ query }, { agent: { id } });
  const contextFor = id => context.text({ agent: { id } });
  const prepare = async (id, text) => {
    const message = user(9, text).data;
    const decision = { kind: 'enter', messages: [message] };
    await handlers['agent/pre-step']({ agent: { id }, messages: [message],
      signal: new AbortController().signal }, async () => decision);
    return contextFor(id);
  };
  send('s1', { type: 'turn/start' });
  send('s1', user(1, '从今天起，我最喜欢的饮料改成茶了。'));
  send('s1', user(2, '我本来约好周六见小林。'));
  send('s1', user(3, '周六见小林的约定取消了。'));
  assert.match(await call('s1', '随便查查'), /周六/); // Wait until the real writes have completed.
  send('s1', { type: 'turn/end' });

  map('s2');
  send('s2', { type: 'turn/start' });
  assert.equal(await prepare('s2', '今天天气怎么样？'), '');
  assert.equal(await prepare('s2', '你还好吗'), '');
  const preference = await prepare('s2', '我现在最爱喝什么？');
  assert.match(preference, /改成茶/);
  assert.match(preference, /unverified_current_highlight/);
  assert.ok(Buffer.byteLength(preference) <= 900);
  send('s2', { type: 'turn/end' });

  send('s2', { type: 'turn/start' });
  const appointment = await prepare('s2', '和小林的安排还要赴约吗？');
  assert.match(appointment, /已取消/);
  assert.match(appointment, /周六见小林/);
  assert.ok(Buffer.byteLength(appointment) <= 900);
  send('s2', { type: 'turn/end' });
  send('s2', { type: 'turn/start' });
  assert.equal(await prepare('s2', '帮我改个名字'), '');
  send('s2', { type: 'turn/end' });
});

test('session start snapshot is on by default, can be disabled, once per session and budget-capped', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-snap-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id } }));
  map('s1');
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'),
    prefsPath }); // no sessionSnapshot option: the default is on
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  const call = (id, query) => tool.execute({ query }, { agent: { id } });
  const contextFor = id => context.text({ agent: { id } });
  const prepare = async (id, text) => {
    const message = user(9, text).data;
    const decision = { kind: 'enter', messages: [message] };
    await handlers['agent/pre-step']({ agent: { id }, messages: [message],
      signal: new AbortController().signal }, async () => decision);
    return contextFor(id);
  };
  send('s1', { type: 'turn/start' });
  send('s1', user(1, '请叫我小渔。'));
  send('s1', user(2, '我本来约好周六见小林。'));
  send('s1', user(3, '周六见小林的约定取消了。'));
  assert.match(await call('s1', '周六'), /周六/); // Wait until the real writes have completed.
  send('s1', { type: 'turn/end' });

  map('s2');
  send('s2', { type: 'turn/start' });
  const greeting = await prepare('s2', '在吗');
  assert.match(greeting, /unverified_current_highlight/);
  // The host-announced current session makes "last thread" available at the first turn (before the
  // new session's first message is archived); the cancellation reaches the model through that
  // verbatim line, and the same source is not repeated as a structured appointment line.
  assert.match(greeting, /unverified_resume/);
  assert.match(greeting, /约定取消了/);
  assert.ok(Buffer.byteLength(greeting) <= 800);
  // 快照只是背景，不占本轮的查询：新会话第一句就是问题时，模型仍要能查（拷问集第二批问题 1）。
  const lookup = await call('s2', '叫我什么');
  assert.doesNotMatch(lookup, /本轮已经查过/);
  assert.match(lookup, /小渔/);
  // 每轮最多查两次（真机：模型常换个关键词再查一次）；第三次才拦。
  assert.match(await call('s2', '周六'), /周六/);
  assert.match(await call('s2', '小林'), /本轮已经查过两次/);
  send('s2', { type: 'turn/end' });
  send('s2', { type: 'turn/start' });
  assert.equal(await prepare('s2', '在吗'), ''); // once per session
  send('s2', { type: 'turn/end' });
  // 问句触发的自动重点算一次：之后工具还能查一次，再查就拦。
  send('s2', { type: 'turn/start' });
  assert.match(await prepare('s2', '你该叫我什么？'), /小渔/);
  assert.doesNotMatch(await call('s2', '称呼'), /本轮已经查过/);
  assert.match(await call('s2', '名字'), /本轮已经查过两次/);
  send('s2', { type: 'turn/end' });
});


test('autoRelated topic gate recalls on rare entities only', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-rel-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id } }));
  map('r1');
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'),
    prefsPath, autoRelated: true });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  const call = (id, query) => tool.execute({ query }, { agent: { id } });
  const contextFor = id => context.text({ agent: { id } });
  const prepare = async (id, text) => {
    const message = user(9, text).data;
    const decision = { kind: 'enter', messages: [message] };
    await handlers['agent/pre-step']({ agent: { id }, messages: [message],
      signal: new AbortController().signal }, async () => decision);
    return contextFor(id);
  };
  send('r1', { type: 'turn/start' });
  send('r1', user(1, '周六和小林去爬山。'));
  assert.match(await call('r1', '爬山'), /爬山/); // flush writes
  send('r1', { type: 'turn/end' });
  send('r1', { type: 'turn/start' });
  const related = await prepare('r1', '我碰到小林了');
  assert.match(related, /爬山/);
  send('r1', { type: 'turn/end' });
  send('r1', { type: 'turn/start' });
  assert.equal(await prepare('r1', '今天天气不错呀'), '');
  send('r1', { type: 'turn/end' });
});

test('core interpreter path follows the platform venv layout', () => {
  const posix = path => path.split(String.fromCharCode(92)).join('/');
  assert.match(posix(venvPython('/p', 'win32')), /\/p\/\.venv\/Scripts\/python\.exe$/);
  assert.match(posix(venvPython('/p', 'darwin')), /\/p\/\.venv\/bin\/python$/);
});

test('automatic recall waits for the in-flight write of the current message', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-race-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id } }));
  map('s1');
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'), prefsPath });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  send('s1', { type: 'turn/start' });
  send('s1', user(1, '我家的猫叫年糕。'));
  assert.match(await tool.execute({ query: '猫' }, { agent: { id: 's1' } }), /年糕/);
  send('s1', { type: 'turn/end' });

  map('s2');
  send('s2', { type: 'turn/start' });
  // The host emits the user message and enters the pre-step at once; the write is still in flight.
  const question = user(2, '我家猫叫啥来着？');
  send('s2', question);
  const decision = { kind: 'enter', messages: [question.data] };
  await handlers['agent/pre-step']({ agent: { id: 's2' }, messages: [question.data],
    signal: new AbortController().signal }, async () => decision);
  assert.match(context.text({ agent: { id: 's2' } }), /年糕/);
  send('s2', { type: 'turn/end' });
});

test('project directory is discovered on Windows and macOS without host config', t => {
  const home = mkdtempSync(join(tmpdir(), 'xuyi-home-'));
  t.after(() => rmSync(home, { recursive: true }));
  const posix = path => path.split(String.fromCharCode(92)).join('/');
  const legacy = join(home, 'Documents', 'Codex', 'xuyi-memory');
  for (const platform of ['win32', 'darwin']) {
    const installed = new Set();
    const exists = path => installed.has(posix(path));
    const find = (config = {}, env = {}) => posix(findProjectDir(config, { env, home, exists, platform }));
    // Nothing installed anywhere: fall back to the legacy macOS default.
    assert.equal(find(), posix(legacy));
    writeFileSync(join(home, '.xuyi-placeholder'), '');
    const registered = join(home, 'code', 'xuyi');
    const { mkdirSync } = process.getBuiltinModule('node:fs');
    mkdirSync(join(home, '.xuyi'), { recursive: true });
    writeFileSync(join(home, '.xuyi', 'project.json'), JSON.stringify({ projectDir: registered }));
    installed.add(posix(venvPython(registered, platform)));
    assert.equal(find(), posix(registered));
    // An explicit config wins only when it is a real install; a stale one is skipped.
    assert.equal(find({ projectDir: join(home, 'missing') }), posix(registered));
    const env = join(home, 'env', 'xuyi');
    installed.add(posix(venvPython(env, platform)));
    assert.equal(find({}, { XUYI_PROJECT_DIR: env }), posix(env));
    rmSync(join(home, '.xuyi'), { recursive: true });
  }
});

test('eating turns auto-recall allergies; other turns stay silent', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-diet-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefsPath = join(folder, 'prefs.json');
  writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': 's1' } }));
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; },
    systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'), prefsPath });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  send('s1', { type: 'turn/start' });
  send('s1', user(1, '我对芒果过敏，吃了会起疹子。'));
  assert.match(await tool.execute({ query: '芒果' }, { agent: { id: 's1' } }), /芒果/);
  send('s1', { type: 'turn/end' });
  const ask = async (seq, text) => {
    send('s1', { type: 'turn/start' });
    const message = user(seq, text);
    send('s1', message);
    const decision = { kind: 'enter', messages: [message.data] };
    await handlers['agent/pre-step']({ agent: { id: 's1' }, messages: [message.data],
      signal: new AbortController().signal }, async () => decision);
    const seen = context.text({ agent: { id: 's1' } });
    send('s1', { type: 'turn/end' });
    return seen;
  };
  assert.match(await ask(2, '推荐个外卖呗'), /对芒果过敏/);
  assert.equal(await ask(3, '推荐个电影呗'), '');
});

test('core call failures leave a diagnostic line without message text', t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-diag-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const error = Object.assign(new Error('Command failed: python -m xuyi ingest'), { code: 1 });
  recordFailure(join(folder, 'data'), 'ingest', error, 'Traceback...\nsqlite3.OperationalError: database is locked\n',
    new Date('2026-11-09T08:20:00Z'));
  const { readFileSync } = process.getBuiltinModule('node:fs');
  const line = readFileSync(join(folder, 'data', 'adapter-errors.log'), 'utf8');
  assert.match(line, /^2026-11-09T08:20:00.000Z ingest code=1 /);
  assert.match(line, /database is locked/);
});

test('the recall tool tells the model not to volunteer memories or mention looking them up', () => {
  let tool;
  apply({ on: () => {}, systemPrompt: { context: () => {} }, tools: { register: spec => { tool = spec; } } },
    { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(tmpdir(), 'unused.sqlite') });
  assert.match(tool.description, /打招呼、闲聊、他没提到的事都不要查/);
  assert.match(tool.description, /不要拿来当开场白/);
  assert.match(tool.description, /不要提「翻了一下」/);
});

test('fallback interpreter comes from pyvenv.cfg on Windows only and tolerates stray quotes', () => {
  const cfg = 'home = "C:/Py/cpython-3.12"\nversion_info = 3.12.1\n';
  const read = () => cfg;
  const found = fallbackInterpreter('C:/proj', 'win32', read);
  assert.equal(found.python, join('C:/Py/cpython-3.12', 'python.exe'));
  assert.match(found.pythonPath, /Lib[\\/]site-packages$/);
  assert.match(found.pythonPath, /^C:[\\/]proj;/);
  assert.equal(fallbackInterpreter('/p', 'darwin', read), null);
  assert.equal(fallbackInterpreter('C:/proj', 'win32', () => { throw new Error('nope'); }), null);
  assert.equal(fallbackInterpreter('C:/proj', 'win32', () => 'implementation = CPython\n'), null);
});

test('a broken venv launcher falls back to the base interpreter and still archives the chat', async t => {
  if (process.platform !== 'win32') return t.skip('Windows launcher only');
  const repo = dirname(dirname(fileURLToPath(import.meta.url)));
  const { readFileSync, mkdirSync, cpSync, writeFileSync: write } = process.getBuiltinModule('node:fs');
  const cfg = /^home\s*=\s*(.+?)\s*$/m.exec(readFileSync(join(repo, '.venv', 'pyvenv.cfg'), 'utf8'));
  if (!cfg) return t.skip('no pyvenv.cfg');
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-launcher-'));
  // Windows 上刚被杀掉的启动器可能还占着目录一会儿；清理失败不该让测试变红。
  t.after(() => { try { rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }); } catch {} });
  cpSync(join(repo, 'xuyi'), join(folder, 'xuyi'), { recursive: true });
  mkdirSync(join(folder, '.venv', 'Scripts'), { recursive: true });
  write(join(folder, '.venv', 'pyvenv.cfg'), `home = ${cfg[1]}\n`);
  write(join(folder, '.venv', 'Scripts', 'python.exe'), 'this is not an executable');
  const prefsPath = join(folder, 'prefs.json');
  write(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': 's1' } }));
  const handlers = {};
  apply({ on: (name, fn) => { handlers[name] = fn; }, systemPrompt: { context: () => {} },
    tools: { register: () => {} } },
  { projectDir: folder, dbPath: join(folder, 'memory.sqlite'), prefsPath });
  t.after(() => handlers.dispose());
  handlers['session/event']({ id: 's1' }, { type: 'turn/start' });
  handlers['session/event']({ id: 's1' }, user(1, '我家的猫叫年糕。'));
  const { DatabaseSync } = await import('node:sqlite');
  let rows = 0;
  for (let i = 0; i < 100 && rows < 1; i++) {
    await new Promise(done => setTimeout(done, 100));
    try { const db = new DatabaseSync(join(folder, 'memory.sqlite'), { readOnly: true });
      rows = db.prepare('SELECT COUNT(*) AS n FROM events').get().n; db.close(); } catch { /* not yet */ }
  }
  assert.equal(rows, 1);
  const log = readFileSync(join(folder, 'data', 'adapter-errors.log'), 'utf8');
  assert.match(log, /ingest code=/);          // 启动器失败有记录
  assert.doesNotMatch(log, /fallback\)/);     // 兜底成功，没有二次失败
});

test('sessionSnapshot:false keeps new sessions silent on a greeting', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-snapoff-'));
  t.after(() => rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id } }));
  map('s1');
  const handlers = {};
  let context, tool;
  apply({ on: (name, fn) => { handlers[name] = fn; }, systemPrompt: { context: spec => { context = spec; } },
    tools: { register: spec => { tool = spec; } } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'),
    prefsPath, sessionSnapshot: false });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  send('s1', { type: 'turn/start' });
  send('s1', user(1, '请叫我小渔。'));
  assert.match(await tool.execute({ query: '叫我' }, { agent: { id: 's1' } }), /小渔/);
  send('s1', { type: 'turn/end' });
  map('s2');
  send('s2', { type: 'turn/start' });
  const message = user(9, '在吗').data;
  const decision = { kind: 'enter', messages: [message] };
  await handlers['agent/pre-step']({ agent: { id: 's2' }, messages: [message],
    signal: new AbortController().signal }, async () => decision);
  assert.equal(context.text({ agent: { id: 's2' } }), '');
});

test('bubble count is rolled by the adapter, shaped by the message and never repeats three times', () => {
  // A tiny seeded generator so the distribution check is deterministic.
  const lcg = seed => () => (seed = (seed * 1664525 + 1013904223) % 4294967296) / 4294967296;
  const share = (text, n = 6000) => {
    const random = lcg(7), counts = [0, 0, 0];
    for (let i = 0; i < n; i++) counts[pickBubbleCount(text, [], random) - 1]++;
    return counts.map(c => c / n);
  };
  const tiny = share('嗯'), medium = share('今天又加班好烦啊'), long = share('今天下班路上碰到一个很久没见的同学，聊了半天，说他换了工作还搬了家');
  assert.ok(tiny[0] > 0.6 && tiny[2] < 0.08, JSON.stringify(tiny));          // 一两个字：多半一条
  assert.ok(long[2] > 0.2 && long[0] < 0.35, JSON.stringify(long));            // 长话：更常多说几条
  assert.ok(medium.every(p => p > 0.05), JSON.stringify(medium));             // 中等：一二三条都会出现
  const excited = share('我中奖了！！哈哈哈');
  assert.ok(excited[2] > share('我中奖了')[2], 'excitement shifts weight toward more bubbles');
  // 连着两次同样的条数后，第三次不再出现同一个数
  for (const same of [1, 2, 3]) {
    const random = lcg(11);
    for (let i = 0; i < 500; i++) assert.notEqual(pickBubbleCount('今天降温了啊哈哈', [same, same], random), same);
  }
  assert.equal(pickBubbleCount('嗯', [], () => 0), 1);
  assert.equal(pickBubbleCount('今天下班路上碰到一个很久没见的同学，聊了半天，说他换了工作还搬了家', [], () => 0.999), 3);
  assert.match(bubbleHint(1), /只发一条/);
  assert.match(bubbleHint(2), /发两条/);
  assert.match(bubbleHint(3), /发三条/);
  assert.doesNotMatch(bubbleHint(2), /续忆|档案|检索|系统/);
});

test('each private turn gets one bubble hint in the prompt, rolled from that turn\'s message', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-bubble-'));
  t.after(() => rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }));
  const prefsPath = join(folder, 'prefs.json');
  const map = id => writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': id } }));
  map('b1');
  const sections = {}, hooks = [];
  apply({ on: (name, fn) => { if (name === 'agent/pre-step') hooks.push(fn); else sections[name] = fn; },
    systemPrompt: { context: spec => { sections[spec.name] = spec; } }, tools: { register: () => {} } },
  { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'memory.sqlite'),
    prefsPath, sessionSnapshot: false, bubbleRandom: () => 0 });
  sections['session/event']({ id: 'b1' }, { type: 'turn/start' });
  const hint = () => sections['xuyi-memory:bubbles'].text({ agent: { id: 'b1' } });
  assert.equal(hint(), '');                       // 还没看到这一轮的消息
  const message = user(1, '嗯').data;
  const decision = { kind: 'enter', messages: [message] };
  for (const hook of hooks) await hook({ agent: { id: 'b1' }, messages: [message], signal: new AbortController().signal }, async () => decision);
  assert.match(hint(), /只发一条/);               // bubbleRandom=0 → 一条
  const again = user(2, '再来').data;
  for (const hook of hooks) await hook({ agent: { id: 'b1' }, messages: [message, again], signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [message, again] }));
  assert.match(hint(), /只发一条/);               // 同一轮的后续步骤不重掷
  sections['session/event']({ id: 'b1' }, { type: 'turn/end' });
  assert.equal(hint(), '');
  // 群聊/未识别的会话不带提示
  sections['session/event']({ id: 'group' }, { type: 'turn/start' });
  assert.equal(sections['xuyi-memory:bubbles'].text({ agent: { id: 'group' } }), '');
});

test('typing delay follows the bubble length and stays within a human range', () => {
  for (const length of [0, 4, 12, 60, 400]) {
    for (const r of [0, 0.5, 0.999]) {
      const ms = typingDelay('字'.repeat(length), () => r);
      assert.ok(ms >= 600 && ms <= 1800, `${length}/${r} → ${ms}`);
    }
  }
  assert.ok(typingDelay('字'.repeat(20), () => 0.5) > typingDelay('嗯', () => 0.5));
});

test('rhythm waits only before the later bubbles of one reply, and fails quiet', async () => {
  const sent = [];
  class FakeBot { async sendMarkdown(target, content, opts) { sent.push([this.tag, target.msgId, content, opts]); return 'ok'; } }
  const sleeps = [];
  let clock = 1000;
  const options = { sleep: async ms => { sleeps.push(ms); clock += ms; }, random: () => 0.5, now: () => clock };
  const uninstall = installRhythm(FakeBot, options);
  assert.equal(typeof uninstall, 'function');
  assert.equal(installRhythm(FakeBot, options), null);            // 已包过，不叠加
  const bot = Object.assign(new FakeBot(), { tag: 'bot-1' });
  const reply = { scope: 'c2c', targetId: 'u', msgId: 'm1' };
  assert.equal(await bot.sendMarkdown(reply, '哈哈哈', { a: 1 }), 'ok');   // 第一条：不等
  assert.deepEqual(sleeps, []);
  await bot.sendMarkdown(reply, '你今天怎么这么早', undefined);              // 第二条：等
  await bot.sendMarkdown(reply, '吃饭没', undefined);                        // 第三条：再等
  assert.equal(sleeps.length, 2);
  assert.ok(sleeps[0] > sleeps[1]);                                         // 更长的那条等得更久
  assert.deepEqual(sent.map(row => row[0]), ['bot-1', 'bot-1', 'bot-1']);   // this 与参数原样传到
  assert.deepEqual(sent[0].slice(1), ['m1', '哈哈哈', { a: 1 }]);
  await bot.sendMarkdown({ ...reply, msgId: 'm2' }, '另一条回复的第一条', undefined);   // 换了一条回复：不等
  assert.equal(sleeps.length, 2);
  clock += 60_000;
  await bot.sendMarkdown(reply, '过了很久再发', undefined);                  // 隔很久：当成新的一轮
  assert.equal(sleeps.length, 2);
  uninstall();
  assert.equal(FakeBot.prototype.sendMarkdown.name, 'sendMarkdown');
  await bot.sendMarkdown(reply, '恢复后', undefined);
  await bot.sendMarkdown(reply, '不再等', undefined);
  assert.equal(sleeps.length, 2);                                           // 还原后不再延迟
  // SDK 结构变了（没有这个方法）→ 什么都不装
  assert.equal(installRhythm(class Other {}, options), null);
  assert.equal(installRhythm(undefined, options), null);
});

test('apply installs the rhythm on the SDK when present, removes it on dispose, and is optional', async t => {
  class FakeBot { async sendMarkdown() { return 1; } }
  const original = FakeBot.prototype.sendMarkdown;
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-rhythm-'));
  t.after(() => rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }));
  const make = extra => {
    const handlers = {};
    apply({ on: (name, fn) => { handlers[name] = fn; }, systemPrompt: { context: () => {} }, tools: { register: () => {} } },
      { projectDir: dirname(dirname(fileURLToPath(import.meta.url))), dbPath: join(folder, 'm.sqlite'),
        prefsPath: join(folder, 'p.json'), ...extra });
    return handlers;
  };
  const handlers = make({ sdkLoader: async () => ({ QQBot: FakeBot }) });
  await new Promise(done => setTimeout(done, 20));
  assert.notEqual(FakeBot.prototype.sendMarkdown, original);                // 已包上
  handlers.dispose();
  assert.equal(FakeBot.prototype.sendMarkdown, original);                   // 退出时还原
  const off = make({ bubbleDelay: false, sdkLoader: async () => ({ QQBot: FakeBot }) });
  await new Promise(done => setTimeout(done, 20));
  assert.equal(FakeBot.prototype.sendMarkdown, original);                   // 关掉就不装
  off.dispose();
  const missing = make({ sdkLoader: async () => { throw new Error('not installed'); } });  // 没装 SDK：安静跳过
  await new Promise(done => setTimeout(done, 20));
  missing.dispose();
});

// ---- 便利贴由宿主模型写 ----

const NOTE_SCOPE = 'qq:c2c:' + 'a'.repeat(64);

function noteFolder(t) {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-notes-'));
  t.after(() => rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }));
  return folder;
}

// 模拟看板写下的请求文件（字段与 xuyi/board_bridge.py 一致）。
function askForNotes(folder, { fingerprint = 'fp-1', facts, persona = '你叫小雅，说话短，爱损他。',
  at = new Date().toISOString(), scope = NOTE_SCOPE, prompt } = {}) {
  const dir = join(folder, 'board-bridge', noteScopeKey(scope));
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'request.json'), JSON.stringify({ scope, fingerprint, facts: facts ?? [
    { kind: 'recall', fact: '他刚问你「猫叫什么」，你想起来：猫叫汤圆' },
    { kind: 'plan', fact: '今天他和妈妈有约' },
  ], persona, ...(prompt === undefined ? {} : { prompt }), at }), 'utf8');
  return { root: join(folder, 'board-bridge'), dir, replyFile: join(dir, 'notes.json') };
}

function fakeModel(text) {
  const calls = [];
  return {
    calls,
    llm: {
      async *stream(options) {
        calls.push(options);
        yield { type: 'text-delta', text };
        yield { type: 'finish', reason: { kind: 'stop' } };
      },
    },
  };
}

test('board notes: the host model writes one sticky note per fact, in the configured persona', async t => {
  const folder = noteFolder(t);
  const { root, replyFile } = askForNotes(folder);
  const { llm, calls } = fakeModel('汤圆啊，你家那只猫叫汤圆，我可记着呢\n下午茶你到底还点不点，我可一直等着呢');
  const route = { provider: 'deepseek-official', model: 'deepseek-v4-flash' };
  assert.equal(await sweepBoardNotes({ dir: root, llm, route }), 1);
  const reply = JSON.parse(readFileSync(replyFile, 'utf8'));
  assert.equal(reply.fingerprint, 'fp-1');
  assert.deepEqual(reply.notes, [
    { kind: 'recall', text: '汤圆啊，你家那只猫叫汤圆，我可记着呢' },
    { kind: 'plan', text: '下午茶你到底还点不点，我可一直等着呢' },
  ]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].provider, 'deepseek-official');
  assert.equal(calls[0].model, 'deepseek-v4-flash');
  assert.equal(calls[0].system, '你叫小雅，说话短，爱损他。');   // 口吻来自用户实际配置的人设
  assert.match(calls[0].messages[0].content[0].text, /猫叫汤圆/);
  assert.match(calls[0].messages[0].content[0].text, /一张一行/);
});

test('board notes: once written for the same content they are never regenerated', async t => {
  const folder = noteFolder(t);
  const { root, replyFile } = askForNotes(folder);
  const first = fakeModel('汤圆啊，你家那只猫叫汤圆，我可记着呢\n下午茶你到底还点不点，我可一直等着呢');
  assert.equal(await sweepBoardNotes({ dir: root, llm: first.llm, route: { provider: 'p', model: 'm' } }), 1);
  const again = fakeModel('不该再写');
  assert.equal(await sweepBoardNotes({ dir: root, llm: again.llm, route: { provider: 'p', model: 'm' } }), 0);
  assert.equal(again.calls.length, 0);
  // 内容变了（指纹不同）才会再写一次。
  askForNotes(folder, { fingerprint: 'fp-2' });
  const changed = fakeModel('新的那张写长一点，别只写几个字\n另一张也得写够十五个字才行');
  assert.equal(await sweepBoardNotes({ dir: root, llm: changed.llm, route: { provider: 'p', model: 'm' } }), 1);
  assert.equal(JSON.parse(readFileSync(replyFile, 'utf8')).fingerprint, 'fp-2');
});

test('board notes: notes written by an older adapter version are rewritten', async t => {
  const folder = noteFolder(t);
  const { root, replyFile } = askForNotes(folder);
  writeFileSync(replyFile, JSON.stringify({ fingerprint: 'fp-1', at: new Date().toISOString(),
    notes: [{ kind: 'recall', text: '旧' }, { kind: 'plan', text: '旧' }] }));
  const { llm, calls } = fakeModel('汤圆啊，你家那只猫叫汤圆，我可记着呢\n下午茶你到底还点不点，我可一直等着呢');
  assert.equal(await sweepBoardNotes({ dir: root, llm, route: { provider: 'p', model: 'm' } }), 1);
  assert.equal(calls.length, 1);
  assert.equal(JSON.parse(readFileSync(replyFile, 'utf8')).writer, 2);
});

test('board notes: a board that stopped asking, or a model that writes badly, costs nothing', async t => {
  const folder = noteFolder(t);
  const stale = askForNotes(folder, { at: new Date(Date.now() - 60_000).toISOString() });
  const quiet = fakeModel('不该再写');
  assert.equal(await sweepBoardNotes({ dir: stale.root, llm: quiet.llm, route: { provider: 'p', model: 'm' } }), 0);
  assert.equal(quiet.calls.length, 0);

  const wrongCount = fakeModel('只有一张');
  const fresh = askForNotes(folder);
  assert.equal(await sweepBoardNotes({ dir: fresh.root, llm: wrongCount.llm, route: { provider: 'p', model: 'm' } }), 0);
  const failed = JSON.parse(readFileSync(fresh.replyFile, 'utf8'));
  assert.equal(failed.error, true);
  assert.equal(failed.fingerprint, 'fp-1');
  // 刚失败过：五分钟内不再花钱重试。
  assert.equal(await sweepBoardNotes({ dir: fresh.root, llm: fakeModel('x\ny').llm,
    route: { provider: 'p', model: 'm' } }), 0);
});

test('board notes: model chatter, numbering and over-long lines are cleaned or refused', () => {
  assert.deepEqual(parseNoteLines('1. 猫叫汤圆\n2、今天和妈妈有约', 2), ['猫叫汤圆', '今天和妈妈有约']);
  assert.deepEqual(parseNoteLines('「猫叫汤圆」\n- 今天和妈妈有约', 2), ['猫叫汤圆', '今天和妈妈有约']);
  assert.equal(parseNoteLines('好的，这是便利贴：\n猫叫汤圆\n和妈妈有约', 2), null);
  assert.equal(parseNoteLines('猫叫汤圆', 2), null);
  assert.equal(parseNoteLines(`${'很'.repeat(41)}\n另一张`, 2), null);
  // 提示词把事实编了号，但明确要求"不编号"。
  assert.match(notePrompt({ facts: [{ kind: 'recall', fact: '猫叫汤圆' }] }), /不编号/);
});

test('board notes: too-short output is asked again with a correction, then accepted', async t => {
  const folder = noteFolder(t);
  const { root, replyFile } = askForNotes(folder);
  const replies = ['汤圆啊，你家那只\n下午茶还点不点',
    '汤圆啊，你家那只猫叫汤圆，我可记着呢\n下午茶你到底还点不点，我可一直等着呢'];
  const calls = [];
  const llm = {
    async *stream(options) {
      calls.push(options);
      yield { type: 'text-delta', text: replies[Math.min(calls.length - 1, replies.length - 1)] };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  assert.equal(await sweepBoardNotes({ dir: root, llm, route: { provider: 'p', model: 'm' } }), 1);
  assert.equal(calls.length, 2);
  assert.match(calls[1].messages[0].content[0].text, /不合格/);
  assert.equal(JSON.parse(readFileSync(replyFile, 'utf8')).notes[0].text,
    '汤圆啊，你家那只猫叫汤圆，我可记着呢');
});

test('board notes: the prompt comes in the request, so wording changes need no rebuild', async t => {
  const folder = noteFolder(t);
  const prompt = '现在请你写便利贴。\n\n1. 他刚问你猫叫什么，你想起来：汤圆';
  const { root } = askForNotes(folder, { prompt });
  const { llm, calls } = fakeModel('汤圆啊，你家那只猫叫汤圆，我可记着呢\n下午茶你到底还点不点，我可一直等着呢');
  assert.equal(await sweepBoardNotes({ dir: root, llm, route: { provider: 'p', model: 'm' } }), 1);
  assert.equal(calls[0].messages[0].content[0].text, prompt);
});

test('diary: the host model writes paragraphs in the configured persona', async t => {
  const folder = noteFolder(t);
  const dir = join(folder, 'board-bridge', noteScopeKey(NOTE_SCOPE));
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'diary-2026-11-09.request.json'), JSON.stringify({
    kind: 'diary', scope: NOTE_SCOPE, date: '2026-11-09', fingerprint: 'd1',
    messages: [{ time: '16:01', role: 'user', text: '我家的猫叫年糕' }],
    persona: '你叫小雅，说话短。', prompt: '写日记', writer: 2, at: new Date().toISOString(),
  }));
  const calls = [];
  const llm = {
    async *stream(options) {
      calls.push(options);
      yield { type: 'text-delta', text: '今天他又提了那只猫，还说年糕粘人。\n\n我记住了，猫叫汤圆。\n\n明天想问问他下午茶到底点不点。' };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  assert.equal(await sweepBoardNotes({ dir: join(folder, 'board-bridge'), llm, route: { provider: 'p', model: 'm' } }), 1);
  const reply = JSON.parse(readFileSync(join(dir, 'diary-2026-11-09.reply.json'), 'utf8'));
  assert.equal(reply.writer, 2);
  assert.equal(reply.text.split('\n\n').length, 3);
  assert.equal(calls[0].system, '你叫小雅，说话短。');
  assert.equal(calls[0].messages[0].content[0].text, '写日记');
});

test('diary: markdown, chatter and too-short bodies are refused', () => {
  assert.equal(parseDiary('## 今天\n\n今天没什么事。'), null);
  assert.equal(parseDiary('好的，这是日记：\n\n今天他又提了猫，我记住了。'), null);
  assert.equal(parseDiary('太短了。'), null);
  const body = '今天他又提了那只猫，还说年糕粘人。\n\n我记住了，猫叫汤圆。';
  assert.equal(parseDiary(body), body);
});

test('board notes: route comes from the host default model, and a missing one stays silent', async () => {
  const ctx = name => ({ get: key => (key === name ? { currentSelection: () => ({ provider: 'p', model: 'm' }) } : undefined) });
  assert.deepEqual(resolveNoteRoute(ctx('agentDefaultModel')), { provider: 'p', model: 'm' });
  assert.deepEqual(resolveNoteRoute(ctx('nothing'), { provider: 'x', model: 'y' }), { provider: 'x', model: 'y' });
  // 聊天实际用的路由最准（从 request/header 学到），但显式配置优先。
  assert.deepEqual(resolveNoteRoute(ctx('agentDefaultModel'), undefined, { provider: 'chat', model: 'c' }),
    { provider: 'chat', model: 'c' });
  assert.deepEqual(resolveNoteRoute(ctx('agentDefaultModel'), { provider: 'x', model: 'y' },
    { provider: 'chat', model: 'c' }), { provider: 'x', model: 'y' });
  assert.equal(resolveNoteRoute({ get: () => undefined }), null);
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-notes-'));
  try {
    const { root } = askForNotes(folder);
    const { llm, calls } = fakeModel('a\nb');
    assert.equal(await sweepBoardNotes({ dir: root, llm, route: null }), 0);
    assert.equal(calls.length, 0);
  } finally {
    rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 });
  }
});

test('board notes: the adapter timer picks up an open board and stops on dispose', async t => {
  const folder = noteFolder(t);
  const { root, replyFile } = askForNotes(folder);
  const { llm, calls } = fakeModel('汤圆啊，你家那只猫叫汤圆，我可记着呢\n下午茶你到底还点不点，我可一直等着呢');
  const ctx = {
    get: key => key === 'llm' ? llm : key === 'agentDefaultModel'
      ? { currentSelection: () => ({ provider: 'deepseek-official', model: 'deepseek-flash' }) } : undefined,
  };
  const stop = startBoardNotes(ctx, { dir: root, interval: 10, route: () => resolveNoteRoute(ctx) });
  for (let attempt = 0; attempt < 100 && !existsSync(replyFile); attempt++) {
    await new Promise(done => setTimeout(done, 10));
  }
  stop();
  assert.equal(calls.length, 1);
  assert.equal(JSON.parse(readFileSync(replyFile, 'utf8')).notes.length, 2);
  assert.equal(calls[0].system, '你叫小雅，说话短，爱损他。');
  // 没有模型服务时不写、不炸。
  const quiet = startBoardNotes({ get: () => undefined }, { dir: root, interval: 10 });
  await new Promise(done => setTimeout(done, 30));
  quiet();
});

// ---- 装进 DSH 的 npm 包：没有项目目录时，用 DSH 自带的 Python 跑包里的 py/ ----

test('runtime: a registered dev project keeps priority; otherwise the package runs on DSH Python', t => {
  const home = mkdtempSync(join(tmpdir(), 'xuyi-rt-'));
  t.after(() => rmSync(home, { recursive: true }));
  const posix = path => path.split(String.fromCharCode(92)).join('/');
  for (const platform of ['win32', 'darwin']) {
    const pkg = join(home, 'pkg');
    const files = new Set([posix(join(pkg, 'py', 'xuyi', '__init__.py'))]);
    const exists = path => files.has(posix(path));
    const rt = (config = {}, env = {}) => resolveRuntime(config, { env, home, exists, platform, packageDir: pkg });
    // 新用户：没有任何项目，DSH 自带的 Python 也还没找到。
    let found = rt();
    assert.equal(found.mode, 'packaged');
    assert.equal(found.python, null);
    assert.equal(posix(found.dataDir), posix(join(home, '.xuyi', 'data')));
    assert.equal(posix(found.modelDir), posix(join(home, '.xuyi', 'models', 'bge-small-zh-q8')));
    files.add(posix(dshPython(home, platform)));
    found = rt();
    assert.equal(posix(found.python), posix(dshPython(home, platform)));
    assert.ok(found.env.PYTHONPATH.startsWith(join(pkg, 'py') + (platform === 'win32' ? ';' : ':')), found.env.PYTHONPATH);
    assert.equal(found.env.PYTHONUTF8, '1');
    assert.equal(found.env.PYTHONNOUSERSITE, '1');
    // 显式指定的解释器优先。
    assert.equal(rt({ python: '/x/py' }).python, '/x/py');
    assert.equal(rt({}, { XUYI_PYTHON: '/y/py' }).python, '/y/py');
    assert.equal(posix(rt({ dataDir: join(home, 'd') }).dataDir), posix(join(home, 'd')));
    // 开发机：登记过、带虚拟环境的项目照旧优先（这台测试机的现状不变）。
    const project = join(home, 'code', 'xuyi');
    files.add(posix(venvPython(project, platform)));
    found = rt({ projectDir: project });
    assert.equal(found.mode, 'project');
    assert.equal(posix(found.python), posix(venvPython(project, platform)));
    assert.equal(posix(found.dataDir), posix(join(project, 'data')));
    // 既没有项目也没有包内代码（直接从源码目录加载）：退回旧行为。
    files.clear();
    assert.equal(rt().mode, 'project');
  }
  assert.match(dshPython('/h', 'win32').split(String.fromCharCode(92)).join('/'),
    /\/h\/\.dsh\/dsh-runtimes\/dsh-primary-runtime\/dependencies\/python\/python\.exe$/);
});

test('packaged build archives and recalls on a bare interpreter with no project and no model', async t => {
  const repo = dirname(dirname(fileURLToPath(import.meta.url)));
  const { spawnSync } = process.getBuiltinModule('node:child_process');
  const venv = venvPython(repo);
  if (!existsSync(venv)) return t.skip('no project venv to build with');
  // 目标解释器：本机有 DSH 自带的 Python 就用它（真实目标），否则用虚拟环境的基础解释器。
  let bare = dshPython();
  if (!existsSync(bare)) {
    const cfg = /^home\s*=\s*(.+?)\s*$/m.exec(readFileSync(join(repo, '.venv', 'pyvenv.cfg'), 'utf8'));
    bare = cfg && join(cfg[1], process.platform === 'win32' ? 'python.exe' : 'python3');
  }
  if (!bare || !existsSync(bare)) return t.skip('no bare interpreter');
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-pack-'));
  t.after(() => rmSync(folder, { recursive: true, force: true, maxRetries: 10, retryDelay: 200 }));
  const stageDir = join(folder, 'stage');
  const built = spawnSync(venv, [join(repo, 'scripts', 'pack_adapter.py'), '--stage-only', '--stage-dir', stageDir],
    { encoding: 'utf8', env: { ...process.env, PYTHONUTF8: '1' } });
  assert.equal(built.status, 0, built.stderr);
  const home = join(folder, 'home');
  mkdirSync(home);
  const prefsPath = join(folder, 'prefs.json');
  writeFileSync(prefsPath, JSON.stringify({ sessionIds: { 'qqbot:bot:c2c:person': 's1' } }));
  const staged = await import(new URL('file:///' + join(stageDir, 'index.js').split(String.fromCharCode(92)).join('/')));
  const handlers = {};
  let tool;
  staged.apply({ on: (name, fn) => { handlers[name] = fn; }, systemPrompt: { context: () => {} },
    tools: { register: spec => { tool = spec; } } },
  { homeDir: home, python: bare, prefsPath, boardNotes: false, bubbleDelay: false, prepare: false });
  t.after(() => handlers.dispose());
  const send = (id, event) => handlers['session/event']({ id }, event);
  send('s1', { type: 'turn/start' });
  send('s1', user(1, '我家的猫叫年糕。'));
  assert.match(await tool.execute({ query: '猫' }, { agent: { id: 's1' } }), /年糕/);
  send('s1', { type: 'turn/end' });
  assert.ok(existsSync(join(home, '.xuyi', 'data', 'dsh-qq.sqlite')));
  assert.ok(!existsSync(join(home, '.xuyi', 'data', 'adapter-errors.log')),
    existsSync(join(home, '.xuyi', 'data', 'adapter-errors.log'))
      ? readFileSync(join(home, '.xuyi', 'data', 'adapter-errors.log'), 'utf8') : '');
});

// ---- 日记本放进 DSH：/xuyi/ 路由转发到后台看板进程 ----

function fakeRes() {
  const res = { status: 0, headers: {}, body: Buffer.alloc(0) };
  res.writeHead = (status, headers = {}) => { res.status = status; Object.assign(res.headers, headers); return res; };
  res.setHeader = (k, v) => { res.headers[k.toLowerCase()] = v; };
  res.write = chunk => { res.body = Buffer.concat([res.body, Buffer.from(chunk)]); return true; };
  res.end = chunk => { if (chunk) res.write(chunk); res.done?.(); };
  res.finished = new Promise(done => { res.done = done; });
  return res;
}

async function hit(handler, url, { method = 'GET', host = '127.0.0.1:3000' } = {}) {
  const res = fakeRes();
  await handler({ url, method, headers: { host } }, res);
  await Promise.race([res.finished, new Promise(done => setTimeout(done, 5000))]);
  return res;
}

test('diary route: forwards only the board pages to the board with its token', async t => {
  const http = process.getBuiltinModule('node:http');
  const seen = [];
  const board = http.createServer((req, res) => {
    seen.push(req.url);
    const headers = { 'content-type': req.url.startsWith('/?') ? 'text/html; charset=utf-8' : 'application/json' };
    if (req.url.startsWith('/export')) headers['content-disposition'] = 'attachment; filename="x.md"';
    res.writeHead(200, headers);
    res.end(req.url.startsWith('/?') ? '<html>日记</html>' : '{"ok":1}');
  });
  await new Promise(done => board.listen(0, '127.0.0.1', done));
  t.after(() => board.close());
  const url = `http://127.0.0.1:${board.address().port}`;
  const { createBoardRoute } = await import('./index.js');
  const handler = createBoardRoute(async () => ({ url, token: 'TOK' }));
  let res = await hit(handler, '/xuyi');
  assert.equal(res.status, 302);
  assert.equal(res.headers.Location ?? res.headers.location, '/xuyi/');
  res = await hit(handler, '/xuyi/');
  assert.equal(res.status, 200);
  assert.match(res.body.toString('utf8'), /日记/);
  res = await hit(handler, '/xuyi/api/board?scope=qq%3Ac2c%3Aabc&t=evil');
  assert.equal(res.status, 200);
  assert.ok(seen.includes('/api/board?scope=qq%3Ac2c%3Aabc&t=TOK'), seen.join(' '));
  res = await hit(handler, '/xuyi/export?scope=qq%3Ac2c%3Aabc&format=md');
  assert.match(String(res.headers['content-disposition']), /attachment/);
  // 「TA的记忆」页：备份、自检照常转发；导入会写档案，没带引导请求头就不转发。
  assert.equal((await hit(handler, '/xuyi/api/backup')).status, 200);
  assert.ok(seen.includes('/api/backup?t=TOK'), seen.join(' '));
  assert.equal((await hit(handler, '/xuyi/api/selfcheck?scope=qq%3Ac2c%3Aabc')).status, 200);
  const beforeRestore = seen.length;
  assert.equal((await post(handler, '/xuyi/api/restore', { format: 'x' }, { 'x-xuyi-setup': '' })).status, 403);
  assert.equal((await post(handler, '/xuyi/api/restore', { format: 'x' }, { 'content-type': 'text/plain' })).status, 415);
  assert.equal(seen.length, beforeRestore);
  assert.equal((await post(handler, '/xuyi/api/restore', { format: 'x' })).status, 200);
  assert.ok(seen.includes('/api/restore?t=TOK'), seen.join(' '));
  // 不转发：关看板、未知路径、穿越、外部 Host（防 DNS 重绑定）、写方法。
  const before = seen.length;
  assert.equal((await hit(handler, '/xuyi/api/shutdown')).status, 404);
  assert.equal((await hit(handler, '/xuyi/../etc')).status, 404);
  assert.equal((await hit(handler, '/xuyi/api/other')).status, 404);
  assert.equal((await hit(handler, '/xuyi/', { host: 'evil.example:3000' })).status, 403);
  assert.equal((await hit(handler, '/xuyi/', { method: 'POST' })).status, 405);
  assert.equal(seen.length, before);
  // 看板起不来：给一页说明，不抛。
  const broken = createBoardRoute(async () => { throw new Error('no python'); });
  res = await hit(broken, '/xuyi/');
  assert.equal(res.status, 503);
  assert.match(res.body.toString('utf8'), /日记本暂时打不开/);
});

test('diary route: the real Python board serves the page and data through DSH', async t => {
  const repo = dirname(dirname(fileURLToPath(import.meta.url)));
  if (!existsSync(venvPython(repo))) return t.skip('no project venv');
  const { spawnSync } = process.getBuiltinModule('node:child_process');
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-board-'));
  const db = join(folder, 'demo.sqlite');
  spawnSync(venvPython(repo), [join(repo, 'scripts', 'board_demo.py'), db], { cwd: repo });
  const { boardManager, createBoardRoute } = await import('./index.js');
  const runtime = resolveRuntime({ projectDir: repo });
  const manager = boardManager(runtime, db);
  t.after(async () => {
    await manager.stop();
    rmSync(folder, { recursive: true, force: true, maxRetries: 20, retryDelay: 250 });
  });
  const handler = createBoardRoute(manager.get);
  const page = await hit(handler, '/xuyi/');
  assert.equal(page.status, 200);
  assert.match(page.body.toString('utf8'), /日记本/);
  const people = JSON.parse((await hit(handler, '/xuyi/api/contacts')).body.toString('utf8'));
  assert.ok(Array.isArray(people) && people.length > 0);
  const font = await hit(handler, '/xuyi/font/ZCOOLKuaiLe.ttf');
  assert.equal(font.status, 200);
  assert.ok(font.body.length > 100000);
  // 同一个看板进程反复用，不是每次请求起一个。
  const first = await manager.get();
  assert.equal(await manager.get(), first);
});

function loadClient(extra = {}) {
  const source = readFileSync(join(dirname(fileURLToPath(import.meta.url)), 'client.js'), 'utf8');
  let loaded;
  const listeners = {};
  const store = {};
  const window = { __ModuleLoader__: { load: spec => { loaded = spec; } },
    addEventListener: (type, fn) => { (listeners[type] ??= []).push(fn); }, removeEventListener: () => {},
    sessionStorage: { getItem: key => store[key] ?? null, setItem: (key, value) => { store[key] = String(value); } },
    ...extra };
  const React = { createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useState: initial => [initial, () => {}], useEffect: () => {}, Fragment: 'Fragment' };
  new Function('window', source)(window);
  assert.equal(loaded.id, 'dsh-xuyi-memory');
  const mod = loaded.factory(name => name === 'react' ? React : { createPortal: node => node });
  const slotsOf = registered => ({ has: () => true, inject: (name, fn) => fn(),
    register: (spec, component) => { registered.push({ spec, component }); return () => {}; } });
  return { mod, listeners, slotsOf, source };
}

test('diary lives in the left sidebar next to QQ and plugins, not in the session header', () => {
  const { mod, listeners, slotsOf } = loadClient();
  assert.deepEqual(mod.inject, ['slots']);
  const registered = [];
  const selected = [];
  const layout = { selectPanel: id => selected.push(id) };
  mod.apply({ slots: slotsOf([]), inject: (deps, fn) => { assert.deepEqual(deps, ['layout']); fn({ slots: slotsOf(registered), layout }); } });
  const names = registered.map(r => r.spec.name).sort();
  assert.deepEqual(names, ['main', 'plugins.detail.actions', 'sidebar.panellist']);
  // 续忆自己的插件详情页上有「打开续忆」，别的插件的详情页上没有。
  const detail = registered.find(r => r.spec.name === 'plugins.detail.actions');
  const props = detail.spec.inject();
  const mine = detail.component({ ...props, subject: { kind: 'bundle', pkg: { name: 'dsh-xuyi-memory' } } });
  assert.match(JSON.stringify(mine), /打开续忆/);
  assert.equal(detail.component({ ...props, subject: { kind: 'bundle', pkg: { name: '@tencent-connect/dsh-qqbot' } } }), null);
  props.onOpen();
  assert.deepEqual(selected, ['xuyi-diary']);
  selected.length = 0;
  const entry = registered.find(r => r.spec.name === 'sidebar.panellist');
  const page = registered.find(r => r.spec.name === 'main');
  assert.equal(entry.spec.id, 'xuyi-diary');
  assert.equal(page.spec.key, 'xuyi-diary');
  assert.equal(entry.spec.label, '续忆');
  // 图标跟随侧栏配色（currentColor），宿主传 size。
  assert.match(JSON.stringify(entry.component({ size: 18 })), /currentColor/);
  // 主区域整页显示日记本。
  assert.match(JSON.stringify(page.component()), /"src":"\/xuyi\/"/);
  // 引导里「去扫码」：直接切到 QQ 机器人页。
  for (const fn of listeners.message ?? []) fn({ data: { type: 'xuyi-open-qq' }, source: null });
  assert.deepEqual(selected, ['im-qqbot']);
});

test('right after install: if setup is not finished, DSH switches to the 续忆 page once', async () => {
  const run = async (state, binding) => {
    const { mod, slotsOf } = loadClient({ fetch: async path => ({
      ok: true, json: async () => path === '/qqbot/im-qqbot/bind' ? binding : state,
    }) });
    const selected = [];
    mod.apply({ slots: slotsOf([]), inject: (deps, fn) => fn({ slots: slotsOf([]), layout: { selectPanel: id => selected.push(id) } }) });
    await new Promise(done => setTimeout(done, 1800));
    return selected;
  };
  const saved = { qq: { installed: true }, persona: { configured: true }, chatted: true,
    health: { recording: true, recall: true, ok: true } };
  // QQ 那边聊上了，但续忆没收到：还没算设置完，照样切过去让引导说清楚卡在哪。
  const results = await Promise.all([
    run({ qq: { installed: false }, persona: { configured: false }, chatted: false }, null),
    run(saved, { phase: 'bound' }),
    run({ ...saved, health: { recording: false, recall: false, ok: false } }, { phase: 'bound' }),
    run(saved, { phase: 'waiting' }),
  ]);
  assert.deepEqual(results, [['xuyi-diary'], [], ['xuyi-diary'], ['xuyi-diary']]);
});

test('the guide renders a new QQ QR code even when prior chats and a persona are saved', () => {
  const page = readFileSync(new URL('../xuyi/board.html', import.meta.url), 'utf8');
  const script = page.slice(page.indexOf('const WIZ_STEPS'), page.indexOf('function openWizard'));
  const image = {};
  const box = { querySelector: selector => selector === '.wiz-qr img' ? image : {}, querySelectorAll: () => [] };
  const document = { getElementById: id => id === 'wizStep' ? box : {} };
  const read = new Function('setupState', 'bindState', 'document', 'sessionStorage', 'renderPrepare',
    script + `return {
      next: wizardStep(),
      scanned: stepDone('scan'),
      render: () => renderWizard(true),
      confirmLegacyScan: () => { scanAcknowledged = true; return stepDone('scan'); },
    };`);
  const saved = { qq: { installed: true }, persona: { configured: true }, chatted: true,
    health: { recording: true, recall: true, ok: true } };
  const load = binding => read(saved, binding, document, { getItem: () => '1' }, () => {});
  for (const phase of ['starting', 'waiting', 'saving', 'failed']) {
    const state = load({ phase });
    assert.equal(state.next, 'scan');
    assert.equal(state.scanned, false);
    assert.equal(state.confirmLegacyScan(), false);
  }
  const qrSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>';
  const officialQr = 'data:image/svg+xml;base64,' + Buffer.from(qrSvg).toString('base64');
  load({ phase: 'waiting', qrSvg: officialQr }).render();
  assert.equal(image.src, officialQr);
  assert.equal(Buffer.from(image.src.split(',')[1], 'base64').toString('utf8'), qrSvg);
  assert.doesNotMatch(box.innerHTML, /data-ack/);
  load({ phase: 'waiting', qrSvg }).render();
  assert.equal(decodeURIComponent(image.src.split(',')[1]), qrSvg);
  assert.equal(load(null).next, 'scan');
  assert.equal(load(null).confirmLegacyScan(), true);
  assert.equal(load({ phase: 'bound' }).next, 'done');
});

test('the guide celebrates only after recall works and opens the memory tab from the finish screen', () => {
  const page = readFileSync(new URL('../xuyi/board.html', import.meta.url), 'utf8');
  const script = page.slice(page.indexOf('const WIZ_STEPS'), page.indexOf('function openWizard'));
  const intro = {}, progress = {}, finish = {};
  const box = { querySelector: selector => selector === '[data-finish]' ? finish : {}, querySelectorAll: () => [] };
  const document = { getElementById: id => ({ wizIntro: intro, wizProgress: progress, wizStep: box })[id] };
  const events = [];
  const render = new Function('setupState', 'bindState', 'document', 'closeWizard', 'showTab',
    script + 'return () => renderWizard();');
  const state = { qq: { installed: true }, persona: { configured: true }, health: { recording: true, recall: false } };
  const redraw = render(state, { phase: 'bound' }, document,
    () => events.push('closed'), tab => events.push(tab));
  redraw();
  assert.equal(intro.hidden, false);
  assert.equal(progress.hidden, false);
  assert.doesNotMatch(box.innerHTML, /恭喜|data-finish/);
  state.health.recall = true;
  redraw();
  assert.equal(intro.hidden, true);
  assert.equal(progress.hidden, true);
  assert.match(box.innerHTML, /信息予你无限，续忆重塑未来/);
  assert.match(box.innerHTML, /恭喜.*续忆能帮 DeepSeek 接上话.*data-finish>看看 TA 的记忆/);
  assert.match(box.innerHTML, /记忆保存在本机.*「备份」.*导出完整记忆/);
  finish.onclick();
  assert.deepEqual(events, ['closed', 'memory']);
});

test('persona details preview without applying and save edits only when confirmed', async () => {
  const page = readFileSync(new URL('../xuyi/board.html', import.meta.url), 'utf8');
  const script = page.slice(page.indexOf('function personaBlock'), page.indexOf('function hourBlock'));
  const prompt = '【你是谁】\n你是个喜欢收集明信片的朋友。';
  const preset = { name: 'postcard', label: '明信片朋友', description: '喜欢旅行', systemPrompt: prompt };
  const detail = { dataset: { detail: '0' }, setAttribute: (key, value) => { detail[key] = value; }, focus() {} };
  const textarea = { focus() {} };
  const elements = { textarea, label: {}, '[data-save-detail]': {}, '[data-close-detail]': {},
    '[data-group]': { value: 'mention' }, '[data-own]': {} };
  const editor = { hidden: true, querySelector: selector => elements[selector] };
  const box = { querySelector: selector => selector === '.persona-editor' ? editor : elements[selector],
    querySelectorAll: selector => selector === '[data-detail]' ? [detail] : [] };
  const posts = [], notices = [];
  let allowed = false, saved = 0;
  const run = new Function('setupState', 'bindState', 'esc', 'say', 'setupPost', 'refreshSetup', 'box', 'onSaved',
    script + 'personaBlock(box, "test", { onSaved });');
  run({ qq: { installed: true }, persona: { name: preset.name }, presets: [preset] },
    { phase: 'bound' }, String, (...args) => notices.push(args),
    async (path, body) => {
      posts.push([path, body]);
      return allowed ? { ok: true, data: { applied: '明信片朋友' } } : { ok: false, data: { error: '测试保存失败' } };
    }, async () => {}, box, () => { saved += 1; });
  assert.match(box.innerHTML, /查看详情/);
  detail.onclick();
  assert.equal(editor.hidden, false);
  assert.equal(textarea.value, prompt);
  assert.equal(detail['aria-expanded'], 'true');
  textarea.value = '还没保存的文字';
  elements['[data-group]'].value = 'all';
  elements['[data-group]'].onchange();
  elements['[data-close-detail]'].onclick();
  assert.equal(editor.hidden, true);
  assert.equal(detail['aria-expanded'], 'false');
  assert.deepEqual(posts, []);
  detail.onclick();
  assert.equal(textarea.value, prompt);
  textarea.value = '  ';
  await elements['[data-save-detail]'].onclick();
  assert.deepEqual(posts, []);
  const edited = prompt + '\n也喜欢散步。';
  textarea.value = edited;
  await elements['[data-save-detail]'].onclick();
  assert.equal(editor.hidden, false);
  assert.equal(textarea.value, edited);
  assert.deepEqual(notices.at(-1), ['testDetail', '测试保存失败', true]);
  assert.equal(saved, 0);
  allowed = true;
  await elements['[data-save-detail]'].onclick();
  assert.deepEqual(posts.at(-1), ['setup/persona', { text: edited, groupReply: 'all' }]);
  assert.equal(saved, 1);
  elements['[data-close-detail]'].onclick();
  detail.onclick();
  await elements['[data-save-detail]'].onclick();
  assert.deepEqual(posts.at(-1), ['setup/persona', { name: preset.name, groupReply: 'all' }]);
});

test('older hosts without a sidebar layout still get the diary as a header button', () => {
  const { mod, slotsOf } = loadClient();
  const registered = [];
  mod.apply({ slots: slotsOf(registered) });
  assert.equal(registered.length, 1);
  assert.equal(registered[0].spec.name, 'conversation.session.header.actions');
  assert.match(JSON.stringify(registered[0].component()), /续忆/);
});

// ---- 首次引导：/xuyi/setup/*（读状态、写人设、装 QQ 插件） ----

async function post(handler, url, body, headers = {}) {
  const res = fakeRes();
  const { Readable } = process.getBuiltinModule('node:stream');
  const req = Readable.from([Buffer.from(JSON.stringify(body))]);
  Object.assign(req, { url, method: 'POST', headers: { host: '127.0.0.1:3000', 'content-type': 'application/json',
    'x-xuyi-setup': '1', ...headers } });
  await handler(req, res);
  await Promise.race([res.finished, new Promise(done => setTimeout(done, 5000))]);
  return res;
}

test('setup route: reads state, applies a persona and installs QQ only on an explicit same-origin request', async () => {
  const { createBoardRoute } = await import('./index.js');
  const calls = [];
  const setup = {
    state: async () => ({ persona: { configured: false }, qq: { installed: false } }),
    persona: async body => { calls.push(['persona', body]); return { applied: '温柔' }; },
    installQq: async () => { calls.push(['qq']); return { installed: true }; },
  };
  const handler = createBoardRoute(async () => { throw new Error('board not needed'); }, setup);
  const state = await hit(handler, '/xuyi/setup/state');
  assert.equal(state.status, 200);
  assert.equal(JSON.parse(state.body.toString('utf8')).qq.installed, false);
  let res = await post(handler, '/xuyi/setup/persona', { name: 'wenrou' });
  assert.equal(res.status, 200);
  assert.deepEqual(calls.at(-1), ['persona', { name: 'wenrou' }]);
  res = await post(handler, '/xuyi/setup/install-qq', {});
  assert.equal(res.status, 200);
  assert.deepEqual(calls.at(-1), ['qq']);
  // 别的网页发不了：缺自定义头、不是 JSON、外部 Host、GET 写接口，都不调用。
  const before = calls.length;
  assert.equal((await post(handler, '/xuyi/setup/persona', { name: 'x' }, { 'x-xuyi-setup': '' })).status, 403);
  assert.equal((await post(handler, '/xuyi/setup/persona', { name: 'x' }, { 'content-type': 'text/plain' })).status, 415);
  assert.equal((await post(handler, '/xuyi/setup/persona', { name: 'x' }, { host: 'evil.example' })).status, 403);
  assert.equal((await hit(handler, '/xuyi/setup/install-qq')).status, 405);
  assert.equal((await post(handler, '/xuyi/api/board', {})).status, 405);
  assert.equal(calls.length, before);
  // 写人设失败时把原因交给页面，不抛。
  const failing = createBoardRoute(async () => ({}), { ...setup, persona: async () => { throw new Error('补丁格式异常'); } });
  res = await post(failing, '/xuyi/setup/persona', { name: 'wenrou' });
  assert.equal(res.status, 400);
  assert.match(res.body.toString('utf8'), /补丁格式异常/);
});

test('setup: QQ status and install go through the host plugin manager', async t => {
  const { createSetup } = await import('./index.js');
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-setup-'));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  writeFileSync(join(folder, 'package.json'), JSON.stringify({ dependencies: {} }));
  const installs = [];
  let bundles = [];
  const pluginManager = {
    listBundles: async () => bundles,
    installBundle: async (spec, options) => { installs.push([spec, options]); bundles = [{ name: spec, enabled: true }]; return { application: 'applied' }; },
  };
  const core = async args => args[0] === 'setup-state'
    ? { persona: { configured: false }, presets: [{ name: 'wenrou', label: '温柔', description: 'd' }] }
    : { applied: args[1], args };
  const setup = createSetup({ profileDir: folder, getPluginManager: () => pluginManager, core, prefsPath: join(folder, 'none.json') });
  let state = await setup.state();
  assert.equal(state.qq.installed, false);
  assert.equal(state.presets[0].name, 'wenrou');
  assert.equal(state.chatted, false);
  // 没装 QQ 插件时写人设不新建条目（交给 Python 报"请先配置 QQ"）。
  let applied = await setup.persona({ name: 'wenrou' });
  assert.ok(!applied.args.includes('--create'));
  await setup.installQq();
  assert.deepEqual(installs, [['@tencent-connect/dsh-qqbot', { enabled: true }]]);
  state = await setup.state();
  assert.equal(state.qq.installed, true);
  applied = await setup.persona({ name: 'wenrou' });
  assert.ok(applied.args.includes('--create'));
  assert.ok(applied.args.includes(folder));
  assert.ok(!applied.args.includes('--group-reply'));               // 没选就不动宿主的群聊设置
  applied = await setup.persona({ name: 'wenrou', groupReply: 'all' });
  assert.deepEqual(applied.args.slice(applied.args.indexOf('--group-reply'), applied.args.indexOf('--group-reply') + 2),
    ['--group-reply', 'all']);
  applied = await setup.persona({ name: 'wenrou', groupReply: '--profile' });   // 只认两个值，别的不进命令行
  assert.ok(!applied.args.includes('--group-reply'));
  // 自己写的人设走临时文件，不进命令行；空的拒绝。
  applied = await setup.persona({ text: '你叫阿桃。' });
  assert.ok(applied.args.includes('--file'));
  await assert.rejects(setup.persona({ text: '   ' }));
  await assert.rejects(setup.persona({ name: '../x' }));
  // 宿主没有插件管理服务时：读 profile 的 package.json，安装给出明确提示。
  const bare = createSetup({ profileDir: folder, getPluginManager: () => undefined, core, prefsPath: join(folder, 'none.json') });
  assert.equal((await bare.state()).qq.installed, false);
  await assert.rejects(bare.installQq(), /插件页/);
});

test('without the layout service, going to scan falls back to the QQ plugin details page', () => {
  const { source } = loadClient();
  assert.match(source, /pluginNavigation/);
  assert.match(source, /openBundle\("@tencent-connect\/dsh-qqbot"\)/);
});

// ---- 首次运行准备：包模式下后台装语义依赖、下量化模型 ----

test('packaged runtime puts the prepared site on PYTHONPATH and points at the quantized model', () => {
  const home = '/h';
  const posix = path => path.split(String.fromCharCode(92)).join('/');
  const files = new Set([posix(join('/pkg', 'py', 'xuyi', '__init__.py'))]);
  const rt = resolveRuntime({}, { env: {}, home, exists: path => files.has(posix(path)), platform: 'win32', packageDir: '/pkg' });
  assert.equal(rt.mode, 'packaged');
  const parts = rt.env.PYTHONPATH.split(';');
  assert.equal(posix(parts[0]), posix(join('/pkg', 'py')));
  assert.equal(posix(parts[1]), posix(join(home, '.xuyi', 'py-site')));
  assert.equal(posix(rt.modelDir), posix(join(home, '.xuyi', 'models', 'bge-small-zh-q8')));
  assert.equal(posix(rt.siteDir), posix(join(home, '.xuyi', 'py-site')));
  assert.equal(rt.macSemantic, null);
  assert.equal(rt.home, home);
});

test('packaged macOS selects an isolated semantic runtime for the host architecture', () => {
  const home = '/h', pkg = '/pkg';
  for (const [arch, pythonArch] of [['arm64', 'arm64'], ['x64', 'x86_64']]) {
    const options = { env: {}, home, platform: 'darwin', arch, packageDir: pkg,
      exists: path => [join(pkg, 'py', 'xuyi', '__init__.py'), dshPython(home, 'darwin')].includes(path) };
    const rt = resolveRuntime({}, options);
    const root = join(home, '.xuyi', 'semantic-runtime', `20261110-${pythonArch}`);
    assert.equal(rt.python, dshPython(home, 'darwin'));
    assert.deepEqual(rt.macSemantic, { python: join(root, 'python', 'bin', 'python3.12'),
      pythonPath: [join(pkg, 'py'), join(root, 'site')].join(':') });
  }
});

test('packaged startup verifies existing assets and ignores stale progress', async t => {
  const cwd = mkdtempSync(join(tmpdir(), 'xuyi-prepare-'));
  t.after(() => rmSync(cwd, { recursive: true }));
  const { EventEmitter } = await import('node:events');
  const { maybePrepare, prepareStateOf } = await import('./index.js');
  const launched = [];
  const child = new EventEmitter();
  const launch = (python, args, options) => { launched.push({ python, args, options }); return child; };
  const dataDir = join(cwd, 'custom');
  const modelDir = join(cwd, 'model');
  const siteDir = join(cwd, 'site');
  const runtime = { mode: 'packaged', python: '/py', env: { A: '1' }, cwd: dataDir, modelDir, siteDir, dataDir, home: cwd };
  for (const dir of [dataDir, modelDir, join(siteDir, 'onnxruntime')]) mkdirSync(dir, { recursive: true });
  writeFileSync(join(modelDir, 'source.json'), '{}');
  const progressPath = join(dataDir, 'prepare.json');
  writeFileSync(progressPath, JSON.stringify({ stage: 'done', percent: 100, backend: 'mac-standalone' }));
  const packaged = { ...runtime };
  assert.equal(maybePrepare(packaged, { launch }), true);
  assert.deepEqual(launched[0].args, ['-m', 'xuyi', 'prepare', '--home', cwd,
    '--progress-file', progressPath, '--run-id', packaged.prepareRun.id]);
  assert.equal(launched[0].python, '/py');
  assert.deepEqual(prepareStateOf(packaged), { stage: 'waiting', percent: 0, ready: false });
  const writeProgress = progress => writeFileSync(progressPath, JSON.stringify({ ...progress, run_id: packaged.prepareRun.id }));
  writeProgress({ stage: 'verify', percent: 95, backend: 'mac-standalone' });
  assert.equal(prepareStateOf(packaged).ready, false);
  assert.equal(prepareStateOf(packaged).backend, undefined);
  writeProgress({ stage: 'done', percent: 100, backend: 'mac-standalone' });
  assert.deepEqual(prepareStateOf(packaged), {
    stage: 'done', percent: 100, error: undefined, ready: true, backend: 'mac-standalone',
  });
  writeProgress({ stage: 'failed', percent: 0, error: 'macOS 拒绝当前 Python 加载语义检索依赖' });
  assert.equal(prepareStateOf(packaged).ready, false);
  assert.match(prepareStateOf(packaged).error, /macOS 拒绝/);
  writeProgress({ stage: 'verify', percent: 95 });
  child.emit('exit', 1);
  assert.equal(prepareStateOf(packaged).stage, 'failed');
  assert.equal(maybePrepare(packaged, { launch }), false);
  assert.equal(maybePrepare({ ...runtime, mode: 'project' }, { launch }), false);
  const missing = { ...runtime, python: null };
  assert.equal(maybePrepare(missing, { launch }), false);
  assert.equal(prepareStateOf(missing).stage, 'failed');
  assert.equal(maybePrepare(runtime, { launch, enabled: false }), false);
  assert.equal(launched.length, 1);
});

test('setup state carries the first-run prepare progress', async t => {
  const { createSetup } = await import('./index.js');
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-prep-'));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  writeFileSync(join(folder, 'package.json'), '{}');
  const setup = createSetup({ profileDir: folder, prefsPath: join(folder, 'none.json'), getPluginManager: () => undefined,
    core: async () => ({ persona: { configured: true }, presets: [] }),
    prepareState: () => ({ stage: 'model', percent: 62, ready: false }) });
  const state = await setup.state();
  assert.deepEqual(state.prepare, { stage: 'model', percent: 62, ready: false });
});

test('diary: over 500 characters is refused, and the model gets one chance to shorten it', async t => {
  const long = ('今天他说了好多话，我一句一句都记着。'.repeat(8) + '\n\n').repeat(4).trim();
  assert.ok(Array.from(long.replace(/\s/g, '')).length > 500);
  assert.equal(parseDiary(long), null);
  const fine = '今天他又提了那只猫，还说年糕粘人。\n\n我记住了，猫叫汤圆。';
  assert.equal(parseDiary(fine, 500), fine);
  // 请求里带的上限优先（更紧的上限也照办）。
  assert.equal(parseDiary(fine, 15), null);

  const folder = noteFolder(t);
  const dir = join(folder, 'board-bridge', noteScopeKey(NOTE_SCOPE));
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, 'diary-2026-11-09.request.json'), JSON.stringify({
    kind: 'diary', scope: NOTE_SCOPE, date: '2026-11-09', fingerprint: 'd2', max_chars: 500,
    messages: [{ time: '16:01', role: 'user', text: '我家的猫叫年糕' }],
    persona: '你叫小雅。', prompt: '写日记', writer: 2, at: new Date().toISOString(),
  }));
  const prompts = [];
  const llm = {
    async *stream(options) {
      prompts.push(options.messages[0].content[0].text);
      yield { type: 'text-delta', text: prompts.length === 1 ? long : fine };
      yield { type: 'finish', reason: { kind: 'stop' } };
    },
  };
  assert.equal(await sweepBoardNotes({ dir: join(folder, 'board-bridge'), llm, route: { provider: 'p', model: 'm' } }), 1);
  assert.equal(prompts.length, 2);
  assert.match(prompts[1], /500 字以内/);
  const reply = JSON.parse(readFileSync(join(dir, 'diary-2026-11-09.reply.json'), 'utf8'));
  assert.equal(reply.text, fine);
  assert.equal(reply.writer, 2);
});

test('writer version comes from the request, so a Python-side bump never loops before DSH restarts', async t => {
  // 真机踩过：Python 改动立刻生效、JS 要重启才生效；两边各自维护版本号时，旧适配器写回的
  // 版本被新 Python 判为过时，每次打开日记本都"正在写"、每次都白调一次模型。
  const folder = noteFolder(t);
  const dir = join(folder, 'board-bridge', noteScopeKey(NOTE_SCOPE));
  mkdirSync(dir, { recursive: true });
  const ask = writer => writeFileSync(join(dir, 'diary-2026-11-09.request.json'), JSON.stringify({
    kind: 'diary', scope: NOTE_SCOPE, date: '2026-11-09', fingerprint: 'd9', max_chars: 500,
    messages: [{ time: '16:01', role: 'user', text: '我家的猫叫年糕' }],
    persona: '你叫小雅。', prompt: '写日记', writer, at: new Date().toISOString(),
  }));
  let calls = 0;
  const llm = { async *stream() { calls++; yield { type: 'text-delta', text: '今天他又提了那只猫，还说年糕粘人。\n\n我记住了，猫叫汤圆。' };
    yield { type: 'finish', reason: { kind: 'stop' } }; } };
  const sweep = () => sweepBoardNotes({ dir: join(folder, 'board-bridge'), llm, route: { provider: 'p', model: 'm' } });
  ask(7);   // 比适配器里写死的版本还新
  assert.equal(await sweep(), 1);
  const reply = JSON.parse(readFileSync(join(dir, 'diary-2026-11-09.reply.json'), 'utf8'));
  assert.equal(reply.writer, 7);   // 照请求的版本写回，Python 才会认
  assert.equal(await sweep(), 0);  // 同一个请求不再重写
  assert.equal(calls, 1);
});

// ---- 到点写日记（默认晚上十点，用户可在设置里改） ----

test('diary clock asks Python soon after start and then every few minutes, and stops on dispose', async () => {
  const { startDiaryClock } = await import('./index.js');
  // 等到条件成立为止，而不是睡固定时长：机器忙时定时器会晚到，固定 120ms 偶尔不够。
  const until = async (ok, ms = 3000) => {
    const end = Date.now() + ms;
    while (!ok() && Date.now() < end) await new Promise(done => setTimeout(done, 5));
  };
  let runs = 0;
  const stop = startDiaryClock(async () => { runs++; }, { firstDelay: 10, interval: 20 });
  await until(() => runs >= 3);
  stop();
  const seen = runs;
  assert.ok(seen >= 3, `ran ${seen} times`);
  await new Promise(done => setTimeout(done, 60));
  assert.equal(runs, seen);
  // 出错不影响后续（Python 起不来、档案被占用都只是这一轮不写）。
  let tries = 0;
  const stop2 = startDiaryClock(async () => { tries++; throw new Error('boom'); }, { firstDelay: 5, interval: 10 });
  await until(() => tries >= 2);
  stop2();
  assert.ok(tries >= 2);
});

test('setup: the diary hour is chosen in the setup panel and validated on both sides', async () => {
  const { createBoardRoute, createSetup } = await import('./index.js');
  const calls = [];
  const setup = createSetup({ profileDir: tmpdir(), prefsPath: join(tmpdir(), 'none.json'), getPluginManager: () => undefined,
    core: async args => { calls.push(args); return { diary_hour: Number(args.at(-1)) }; } });
  assert.deepEqual(await setup.diaryHour({ hour: 21 }), { diary_hour: 21 });
  assert.deepEqual(calls.at(-1), ['diary-hour', '--set', '21']);
  for (const bad of [17, 24, '22', 21.5, null]) await assert.rejects(setup.diaryHour({ hour: bad }));
  const handler = createBoardRoute(async () => ({}), setup);
  const res = await post(handler, '/xuyi/setup/diary-hour', { hour: 20 });
  assert.equal(res.status, 200);
  assert.deepEqual(calls.at(-1), ['diary-hour', '--set', '20']);
  assert.equal((await post(handler, '/xuyi/setup/diary-hour', { hour: 20 }, { 'x-xuyi-setup': '' })).status, 403);
});

// ---- 热加载：开发时改 logic.js 不用退出 DSH ----

test('hot logic: a changed file replaces the behaviour and disposes the old one; a broken file keeps the last good version', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-hot-'));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const file = join(folder, 'logic.js');
  const source = version => `export function create() { return { version: ${version},
    disposed: false, start() {}, dispose() { this.disposed = true; } }; }
// ${'v'.repeat(version * 5)}\n`;
  writeFileSync(file, source(1));
  const warned = [];
  const booted = { version: 0, disposed: false, start() {}, dispose() { this.disposed = true; } };
  const loader = createLogicLoader({ file, boot: () => booted,
    warn: message => warned.push(message), throttleMs: 0, now: () => 0 });
  loader.start();
  assert.equal(loader.current.version, 0, 'boot uses the packaged copy until the file changes');

  await loader.reload();                      // 文件没变：不重载
  assert.equal(loader.current.version, 0);
  assert.equal(booted.disposed, false);

  writeFileSync(file, source(2));
  await loader.reload();
  assert.equal(loader.current.version, 2);
  assert.equal(loader.current.disposed, false);
  assert.equal(booted.disposed, true, 'the replaced instance is stopped, so its timers do not pile up');

  const good = loader.current;
  writeFileSync(file, 'export function create( { return 3 }\n');   // 语法坏掉
  await loader.reload();
  assert.equal(loader.current, good, 'a broken edit must not take the plugin down');
  assert.equal(good.disposed, false);
  assert.equal(warned.length, 1);
  assert.match(warned[0], /热加载失败/);
});

test('hot logic: check() is throttled and swaps in the new module once the file stamp changes', async () => {
  let stamp = 1;
  let loaded = 0;
  const loader = createLogicLoader({ file: 'unused', boot: () => ({ version: 0, start() {}, dispose() {} }),
    throttleMs: 0, now: () => 1, stat: () => ({ mtimeMs: stamp, size: 1 }),
    load: async () => ({ create: () => ({ version: ++loaded, start() {}, dispose() {} }) }),
    warn: () => {} });
  loader.start();
  assert.equal(loader.current.version, 0);
  stamp = 2;
  loader.check();                       // 打开日记本、调用工具都会走这里
  await new Promise(done => setTimeout(done, 20));
  assert.equal(loader.current.version, 1);
  loader.check();                            // 文件没再变：不重复 import
  await new Promise(done => setTimeout(done, 20));
  assert.equal(loaded, 1);
});

test('hot logic: a host loader that rejects query URLs still reloads through a copied file name', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-fallback-'));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const file = join(folder, 'logic.js');
  const pkg = join(folder, 'pkg');
  mkdirSync(pkg);
  const source = version => `export function create() { return { version: ${version}, start() {}, dispose() {} }; }
// ${'x'.repeat(version * 4)}\n`;
  writeFileSync(file, source(1));
  const rejected = [];
  const loader = createLogicLoader({ file, boot: () => ({ version: 0, start() {}, dispose() {} }),
    fallbackDir: pkg, throttleMs: 0, now: () => 1, warn: () => {},
    load: async spec => {
      if (spec.includes('?v=')) { rejected.push(spec); throw new Error('加载器不认查询串'); }
      return import(spec);
    } });
  loader.start();
  writeFileSync(file, source(2));
  await loader.reload();
  assert.equal(loader.current.version, 2, 'the copy is a fresh module URL, so it is evaluated');
  assert.equal(rejected.length, 1);
  assert.deepEqual(readdirSync(pkg), [], 'the temporary copy is removed');
});

test('hot logic: a swapped module is created with the same context, config and host as boot', async t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-args-'));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const file = join(folder, 'logic.js');
  const source = tag => `export function create() { return { tag: '${tag}' }; }
// ${'y'.repeat(tag.length * 10 + tag.charCodeAt(1))}\n`;
  writeFileSync(file, source('v1'));
  const boot = () => ({ tag: 'boot', start() {}, dispose() {} });
  const loader = createLogicLoader({ file, boot, createArgs: ['CTX', 'CFG', 'HOST'],
    throttleMs: 0, now: () => 1, warn: () => {},
    load: async () => ({ create: (...args) => ({ args, start() {}, dispose() {} }) }) });
  loader.start();
  writeFileSync(file, source('v2'));
  await loader.reload();
  assert.deepEqual(loader.current.args, ['CTX', 'CFG', 'HOST'],
    'a swapped module must get the same three arguments, not just the host');
});

test('hot logic: the dev project file wins, the packaged copy is the fallback', t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-dev-'));
  t.after(() => rmSync(folder, { recursive: true, force: true }));
  const project = join(folder, 'proj');
  mkdirSync(join(project, 'dsh-adapter'), { recursive: true });
  writeFileSync(join(project, 'dsh-adapter', 'logic.js'), 'export function create() {}\n');
  assert.equal(logicFileOf({ projectDir: project }, { home: folder }), join(project, 'dsh-adapter', 'logic.js'));
  const missing = join(folder, 'nope');
  assert.equal(logicFileOf({ projectDir: missing }, { home: folder, fallback: folder }), join(folder, 'logic.js'));
});


// ---- 群聊（L1：归档 + 工具查询；不自动注入） ----

test('group: a message with history becomes several archived lines, each with its member id', () => {
  const text = '[Chat history begins]\n'
    + '[纸鸢 (11111111111111111111111111111111)] 桌游定在周六\n'
    + '[木槿 (22222222222222222222222222222222)] 我带棋盘\n'
    + '[Chat history ends]\n[Current message]\n'
    + '[纸鸢 (11111111111111111111111111111111)] <@33333333333333333333333333333333> (@you)';
  const lines = parseGroupLines(text);
  assert.deepEqual(lines.map(line => line.text), ['桌游定在周六', '我带棋盘']);
  assert.deepEqual(lines.map(line => line.sender_id),
    ['11111111111111111111111111111111', '22222222222222222222222222222222']);
});

test('group: archived rows carry the group scope and per-line event ids', () => {
  const event = { type: 'user/message', surfaceOp: 'append', seq: 7, time: Date.now(),
    data: { role: 'user', source: { kind: 'user' },
      content: [{ type: 'text', text: '[纸鸢 (11111111111111111111111111111111)] 我带纸牌\n[纸鸢 (11111111111111111111111111111111)] 记得带水' }] } };
  const rows = archiveGroupEvents('abc123', 'sess', event);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].scope, 'qq:group:abc123');
  assert.equal(rows[0].event_id, 'dsh:sess:7:0');
  assert.equal(rows[1].event_id, 'dsh:sess:7:1');
  assert.equal(rows[0].sender_id, '11111111111111111111111111111111');
});

test('group: session keys resolve to group scope, private ones stay c2c', t => {
  const folder = mkdtempSync(join(tmpdir(), 'xuyi-group-'));
  t.after(() => rmSync(folder, { recursive: true }));
  const prefs = join(folder, 'prefs.json');
  writeFileSync(prefs, JSON.stringify({ sessionIds: {
    'qqbot:fake-bot:c2c:ABC': 'priv', 'qqbot:fake-bot:group:DEF': 'room' } }));
  assert.equal(resolveScope(prefs, 'priv').kind, 'c2c');
  assert.equal(resolveScope(prefs, 'room').kind, 'group');
  assert.equal(resolveScope(prefs, 'room').id, 'DEF');
});


test('group: her own replies are archived too', () => {
  const event = { type: 'assistant/message', surfaceOp: 'append', seq: 9, time: Date.now(),
    data: { message: { role: 'assistant', content: [{ type: 'text', text: '好，周六见。' }] } } };
  const rows = archiveGroupEvents('abc123', 'sess', event);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].role, 'assistant');
  assert.equal(rows[0].sender_id, 'assistant');
  assert.equal(rows[0].scope, 'qq:group:abc123');
  assert.equal(rows[0].event_id, 'dsh:sess:9');
});
