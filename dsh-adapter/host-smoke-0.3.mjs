import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// 0.3 host-runtime smoke: three-family auto recall plus the session-start
// snapshot across an automatic session rotation (the host never relies on
// a manual /new; rotating the mapped session id models it).
const [packages, project] = process.argv.slice(2);
const load = name => import(join(packages, name, 'lib/index.js'));
const { Context } = await load('cordis');
const { default: Llm, LlmAdapter, createUserMessage } = await load('dsh-llm');
const { default: Sessions, SessionId } = await load('dsh-session');
const { default: Projections } = await load('dsh-session-projection');
const { default: SystemPrompt } = await load('dsh-system-prompt');
const { default: Tools } = await load('dsh-tools');
const NativeTools = await load('dsh-tool-executor-native');
const { default: Agents, installModelSelection } = await load('dsh-agent');
const { default: Loop } = await load('dsh-agent-loop');
const Xuyi = await import(join(project, 'dsh-adapter/index.js'));

class Recorder extends LlmAdapter {
  requests = [];
  async resolveModel(provider, model) {
    return { provider, id: model, name: model };
  }
  async *stream(request) {
    this.requests.push(request);
    const text = '本地模拟回复';
    yield { type: 'block-start', index: 0, blockType: 'text' };
    yield { type: 'text-delta', index: 0, text };
    yield { type: 'block-end', index: 0, block: { type: 'text', text } };
    yield { type: 'finish', reason: { kind: 'stop' } };
  }
}
const dir = mkdtempSync(join(tmpdir(), 'xuyi-host-'));
const ctx = new Context();
try {
  for (const plugin of [Llm, Sessions, Projections, SystemPrompt, Tools, NativeTools, Agents]) await ctx.plugin(plugin);
  await ctx.plugin(Loop, { agents: [] });
  const db = join(dir, 'archive.sqlite');
  const prefs = join(dir, 'prefs.json');
  const link = id => writeFileSync(prefs, JSON.stringify({ sessionIds: { 'qqbot:test:c2c:sample': id } }));
  await ctx.plugin(Xuyi, { projectDir: project, dbPath: db, prefsPath: prefs, sessionSnapshot: true });
  const model = new Recorder();
  ctx.llm.registerAdapter(['offline'], model);
  const create = async id => {
    const agent = await ctx.agentLoop.create(SessionId(id), { provider: 'offline', model: 'offline' });
    installModelSelection(agent.ctx);
    return agent;
  };
  const send = async (agent, text) => {
    agent.followup(createUserMessage({ content: [{ type: 'text', text }], source: { kind: 'user' } }));
    await agent.whenIdle();
  };
  const contextOf = request => request.messages
    .filter(message => message.source?.kind === 'runtime-context').at(-1)?.content
    .map(block => block.text).join('\n') ?? '';
  const settle = ms => new Promise(resolve => setTimeout(resolve, ms));
  const waitFor = async count => {
    for (let attempt = 0; attempt < 40; attempt++) {
      const child = spawnSync(join(project, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python'), ['-c',
        'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("select count(*) from events").fetchone()[0])',
        db], { encoding: 'utf8' });
      if (Number(child.stdout.trim()) >= count) return;
      await new Promise(resolve => setTimeout(resolve, 35));
    }
    throw new Error('宿主原始消息没有归档');
  };

  // Round one: statements land in the archive (as in a live chat).
  const live = await create('session-one');
  link(live.id);
  await send(live, '我本来约好周六见小林。');
  await send(live, '周六见小林的约定取消了。');
  await send(live, '从今天起，我最喜欢的饮料改成茶了。');
  await send(live, '我下周要考试。');
  await waitFor(8);
  await settle(150);

  // Automatic rotation: a fresh session id, same contact mapping.
  const rotated = await create('session-two');
  link(rotated.id);
  await send(rotated, '在吗');
  const greetingContext = contextOf(model.requests.at(-1));
  assert.match(greetingContext, /unverified_current_highlight/, '新会话首轮应有状态快照');
  assert.match(greetingContext, /已取消/);
  assert.match(greetingContext, /改成茶/);
  assert.match(greetingContext, /考试/, '开放环应进入快照');
  assert.ok(Buffer.byteLength(greetingContext) <= 800 + 130, '快照注入应贴近预算');
  await waitFor(8);
  await settle(120);

  // Second turn of the rotated session: targeted question recall.
  await send(rotated, '和小林的安排还要赴约吗？');
  const cancelContext = contextOf(model.requests.at(-1));
  assert.match(cancelContext, /已取消/);
  assert.match(cancelContext, /周六见小林/, '取消候选应带上被取消约定的原话');
  await waitFor(10);
  await settle(120);

  await send(rotated, '我现在最爱喝什么？');
  const prefContext = contextOf(model.requests.at(-1));
  assert.match(prefContext, /改成茶/);
  await waitFor(12);
  await settle(120);

  // Irrelevant message: zero new injection.
  await send(rotated, '今天天气怎么样？');
  assert.doesNotMatch(contextOf(model.requests.at(-1)), /unverified_current_highlight/);
  await waitFor(14);

  console.log(JSON.stringify({
    snapshotOnRotation: true, snapshotBytes: Buffer.byteLength(greetingContext),
    cancelCarriesBothSources: true, preferenceRecall: true, irrelevantSilent: true,
    archivedEvents: 16,
  }));
} finally {
  await ctx.fiber.dispose();
  rmSync(dir, { recursive: true, force: true });
}
