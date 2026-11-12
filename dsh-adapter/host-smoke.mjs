import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

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
  await ctx.plugin(Xuyi, { projectDir: project, dbPath: db, prefsPath: prefs, autoRecall: process.env.XUYI_SMOKE_MANUAL !== '1' });
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
  const waitFor = async count => {
    for (let attempt = 0; attempt < 30; attempt++) {
      const child = spawnSync(join(project, process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python'), ['-c',
        'import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute("select count(*) from events").fetchone()[0])',
        db], {encoding:'utf8'});
      if (Number(child.stdout.trim()) >= count) return;
      await new Promise(resolve => setTimeout(resolve, 35));
    }
    throw new Error('宿主原始消息没有归档');
  };
  const before = await create('before');
  link(before.id);
  await send(before, '我的猫叫甲号。');
  await waitFor(2);
  await send(before, '更正一下，我的猫叫乙号。');
  await waitFor(4);
  await new Promise(resolve => setTimeout(resolve, 40));
  const after = await create('after');
  link(after.id);
  await send(after, '宠物叫什么？');
  assert.equal(model.requests.length, 3);
  const newest = request => request.messages
    .filter(message => message.source?.kind === 'runtime-context').at(-1)?.content
    .map(block => block.text).join('\n') ?? '';
  const snapshot = newest(model.requests.at(-1));
  const hasEvidence = snapshot.includes('unverified_current_highlight');
  const mode = process.env.XUYI_SMOKE_MANUAL === '1' ? 'manual' : 'auto';
  assert.equal(hasEvidence, mode === 'auto');
  if (hasEvidence) {
    assert.match(snapshot, /更正一下，我的猫叫乙号/);
    assert.doesNotMatch(snapshot, /甲号/);
  }
  await waitFor(6);
  await send(after, '晚安。');
  assert.doesNotMatch(newest(model.requests.at(-1)), /unverified_current_highlight/);
  await waitFor(8);
  console.log(JSON.stringify({ mode, hasEvidence, evidenceBytes: hasEvidence ? Buffer.byteLength(snapshot) : 0,
    firstQuestionCalls: 1, irrelevantNewEvidence: false, archivedUserAndAssistantEvents: 8 }));
} finally {
  await ctx.fiber.dispose();
  rmSync(dir, { recursive: true, force: true });
}
