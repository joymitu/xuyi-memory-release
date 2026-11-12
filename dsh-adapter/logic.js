import { createHash } from 'node:crypto';
import { execFile, spawn } from 'node:child_process';
import { request as httpRequest } from 'node:http';
import { appendFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { installRhythm } from './rhythm.js';


const PEER = /^qqbot:[^:]+:c2c:[^:]+$/;
const toolBudget = 1600;
const autoBudget = 900;
// Windows 上启动 Python 约 2 倍于 macOS（实测单次自动召回约 230ms），给足余量。
const defaultAutoDeadline = process.platform === 'win32' ? 900 : 450;
const writeWait = 1500;
const snapshotBudget = 800;
// Keep in sync with the family QUERY regexes in xuyi/highlights.py: name, preference, appointment.
// Diet (allergies/restrictions) fires on any eating or drinking turn; mirrors _DietFamily.QUERY.
const dietQuestion = /过敏|忌口|(?:能|可以)不?(?:能)?(?:吃|喝)|不能(?:吃|喝)|(?:吃|喝)(?:点|些)?(?:什么|啥)|推荐.{0,6}(?:吃|喝|菜|外卖|餐|饭|零食|水果|甜品)|外卖|(?:早|午|晚)(?:饭|餐)|早点|夜宵|做(?:什么|啥)菜/;
const memoryQuestion = /叫(?:我)?(?:什么|啥|哪个)|(?:名字|昵称|称呼)(?:是)?(?:什么|啥|哪个)|记得.*(?:名字|昵称|称呼)|(?:怎么|如何)(?:叫|称呼)我|最(?:爱|喜欢|讨厌)(?:[^。？！?]*?)?(?:什么|啥)|(?:爱|喜欢|讨厌)(?:[喝吃玩看听用穿去])(?:[^。？！?]{0,6})?(?:什么|啥)|还(?:在|再)?(?:[喝吃玩用去])(?:[^。？！?]{0,8})?(?:吗|么)|戒了(?:吗|没有)|(?:有什么|啥)(?:约定|安排|计划)|(?:约定|安排|赴约|约会|邀约|见面|碰面)(?:[^。？！?]{0,12})?(?:吗|么|啥|什么|哪)|还(?:要|得)?(?:去|见|来)(?:不)?(?:吗|么)|见不见|去不去|(?:最近|接下来)?(?:有什么|啥)(?:打算|计划)|(?:要|想|打算|准备)(?:做|干|办)的?什么(?:来着)?|准备得?(?:怎么样|如何|咋样)|我(?:今天|昨天|这几天|最近)?(?:心情|感觉)(?:怎么样|如何|咋样)|我(?:开不开心|难不难过|累不累|烦不烦)|你还?记得我(?:今天|昨天)?(?:心情|感觉|状态)吗/;
const nameChange = /别叫|不要叫|改叫|改名|取个名字|起个名字/;

function userText(message, acceptAny = false) {
  if (message?.role !== 'user' || message.source?.kind !== 'user'
      || message.content?.length !== 1 || message.content[0]?.type !== 'text') return '';
  const raw = message.content[0].text;
  if (typeof raw !== 'string' || /[\r\n\[\]]/.test(raw)) return '';
  const query = raw.trim();
  if (Array.from(query).length > 120 || nameChange.test(query)) return '';
  return acceptAny ? query
    : (memoryQuestion.test(query) || dietQuestion.test(query) ? query : '');
}

// How many chat bubbles to send this turn. Language models are poor dice: left alone they settle on
// one habit (real chat: "两条" every time). So the adapter rolls it — shaped by how much the other
// person said and how worked up they are — and hands the result to the model as a one-line hint.
export function pickBubbleCount(text, recent = [], random = Math.random) {
  const clean = String(text ?? '').replace(/\[[^\]]*\]/g, '').replace(/\s+/g, '');
  const length = Array.from(clean).length;
  let weights = length <= 3 ? [0.72, 0.24, 0.04]
    : length <= 10 ? [0.5, 0.4, 0.1]
    : length <= 30 ? [0.36, 0.44, 0.2]
    : [0.26, 0.44, 0.3];
  if (/[！!]|哈哈|啊啊|救命|离谱|绝了|气死|烦死/.test(clean)) {
    weights = [Math.max(0.02, weights[0] - 0.1), weights[1], weights[2] + 0.1];
  }
  const last = recent.at(-1);
  if (recent.length >= 2 && last === recent.at(-2)) weights[last - 1] = 0;   // never three times running
  const total = weights.reduce((sum, w) => sum + w, 0);
  let roll = random() * total;
  for (let i = 0; i < weights.length; i++) {
    roll -= weights[i];
    if (roll < 0 && weights[i] > 0) return i + 1;
  }
  return weights.findLastIndex(w => w > 0) + 1;
}

export function bubbleHint(count) {
  const words = { 1: '这一轮只发一条', 2: '这一轮发两条', 3: '这一轮发三条' };
  return `${words[count]}，每条一行。他问的事要是一条说不清，就按需要来。`;
}

function checkedContext(result, budget) {
  if (typeof result.context !== 'string' || Buffer.byteLength(result.context) > budget) {
    throw new Error('续忆返回的回忆区超出限制或格式错误');
  }
  return result.context;
}

export function resolvePeer(file, sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) return null;
  try {
    const mapping = JSON.parse(readFileSync(file, 'utf8')).sessionIds;
    if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) return null;
    const matches = Object.entries(mapping).filter(([, id]) => id === sessionId);
    if (matches.length !== 1 || !PEER.test(matches[0][0])) return null;
    return createHash('sha256').update(matches[0][0]).digest('hex');
  } catch {
    return null;
  }
}

const GROUP_KEY = /^qqbot:[^:]+:group:([^:]+)$/;

// 会话 key → {kind, hash, id}；私聊 c2c、群聊 group。hash 是整个 key 的 sha256（和原来一致）。
export function resolveScope(file, sessionId) {
  if (typeof sessionId !== 'string' || !sessionId) return null;
  try {
    const mapping = JSON.parse(readFileSync(file, 'utf8')).sessionIds;
    if (!mapping || typeof mapping !== 'object' || Array.isArray(mapping)) return null;
    const matches = Object.entries(mapping).filter(([, id]) => id === sessionId);
    if (matches.length !== 1) return null;
    const key = matches[0][0];
    const group = GROUP_KEY.exec(key);
    const kind = group ? 'group' : PEER.test(key) ? 'c2c' : null;
    if (!kind) return null;
    return { kind, hash: createHash('sha256').update(key).digest('hex'),
             id: group ? group[1] : key.split(':').pop() };
  } catch {
    return null;
  }
}

// 群消息在宿主里是一整段文本：历史每行 "[显示名 (成员OpenID)] 内容"，最后是当前消息。
const GROUP_LINE = /^\[([^\]]+?) \(([0-9A-Fa-f]{16,})\)\]\s*([\s\S]*)$/;

export function parseGroupLines(text) {
  const out = [];
  for (const raw of String(text ?? '').split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || /^\[Chat history (begins|ends)\]$/.test(line) || /^\[Current message\]$/.test(line)) continue;
    const match = GROUP_LINE.exec(line);
    if (!match) continue;
    const said = match[3].replace(/<@[^>]+>/g, '').replace(/\(\s*@you\s*\)/g, '').trim();
    if (!said) continue;
    out.push({ sender_id: match[2], sender_name: match[1], text: said });
  }
  return out;
}

export function archiveGroupEvents(hash, sessionId, event) {
  if (!hash || !Number.isSafeInteger(event?.seq) || event.seq < 0
      || !Number.isFinite(event.time) || Math.abs(event.time) > 8.64e15) return [];
  const assistant = event.type === 'assistant/message';
  const message = assistant ? event.data?.message : event.data;
  if (event.type !== 'user/message' && !assistant) return [];
  if (event.surfaceOp !== undefined && event.surfaceOp !== 'append') return [];
  if (message?.role !== (assistant ? 'assistant' : 'user') || !Array.isArray(message.content)) return [];
  if (!assistant && message.source?.kind !== 'user') return [];
  const text = message.content.filter(block => block?.type === 'text' && typeof block.text === 'string')
    .map(block => block.text).join('\n');
  if (assistant) {
    // 她在群里说过的话也要进档：群里查"她之前怎么说的"才有原文。
    if (!text.trim()) return [];
    return [{
      scope: 'qq:group:' + hash,
      event_id: 'dsh:' + sessionId + ':' + event.seq,
      sender_id: 'assistant',
      role: 'assistant',
      origin: 'assistant',
      recorded_at: new Date(event.time).toISOString(),
      text,
    }];
  }
  const lines = parseGroupLines(text);
  if (!lines.length && text.trim()) {
    // 不是标准 "[名字 (OpenID)]" 格式时，至少把这句话存下来，不丢内容。
    lines.push({ sender_id: 'unknown', text: text.trim() });
  }
  return lines.map((line, index) => ({
    scope: 'qq:group:' + hash,
    event_id: 'dsh:' + sessionId + ':' + event.seq + ':' + index,
    sender_id: line.sender_id,
    role: 'user',
    origin: 'quoted',
    recorded_at: new Date(event.time).toISOString(),
    text: line.text,
  }));
}

export function archiveEvent(peer, sessionId, event) {
  if (!peer || !Number.isSafeInteger(event?.seq) || event.seq < 0
      || !Number.isFinite(event.time) || Math.abs(event.time) > 8.64e15) return null;
  const role = event.type === 'user/message' ? 'user'
    : event.type === 'assistant/message' ? 'assistant' : null;
  if (!role || (event.surfaceOp !== undefined && event.surfaceOp !== 'append')
      || event.data?.surfaceOp || event.data?.interrupted) return null;
  const message = role === 'user' ? event.data : event.data?.message;
  if (message?.role !== role || !Array.isArray(message.content)) return null;
  if (role === 'user' && message.source?.kind !== 'user') return null;
  const text = message.content.filter(block => block?.type === 'text' && typeof block.text === 'string')
    .map(block => block.text).join('\n');
  if (!text.trim()) return null;
  return {
    scope: 'qq:c2c:' + peer,
    event_id: 'dsh:' + sessionId + ':' + event.seq,
    sender_id: role === 'user' ? peer : 'assistant',
    role,
    // Tencent assembles quotes, transcriptions and the current message into one text block.
    origin: role === 'user' ? 'quoted' : 'assistant',
    recorded_at: new Date(event.time).toISOString(),
    text,
  };
}

// 宿主配置通常不给 projectDir：依次取 配置 → XUYI_PROJECT_DIR → `xuyi register` 写的登记文件
// → macOS 旧默认路径；用第一个真有虚拟环境的，都没有时退回第一个给出的。
export function findProjectDir(config = {}, { env = process.env, home = homedir(),
  exists = existsSync, platform = process.platform } = {}) {
  let registered;
  try { registered = JSON.parse(readFileSync(join(home, '.xuyi', 'project.json'), 'utf8')).projectDir; } catch {}
  const candidates = [config.projectDir, env.XUYI_PROJECT_DIR, registered,
    join(home, 'Documents', 'Codex', 'xuyi-memory')].filter(dir => typeof dir === 'string' && dir.trim());
  const found = candidates.find(dir => exists(venvPython(resolve(dir), platform)));
  return resolve(found ?? candidates[0]);
}

export function venvPython(projectDir, platform = process.platform) {
  return join(projectDir, platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
}

// DSH 安装时自带一份 Python（runtime.json 里的 "python"），放在用户主目录的 .dsh 下。
// Windows 布局已在 0.2.2-rc.1 真机核对；macOS 的 bin/python3 是按惯例推断，未在真机核对。
export function dshPython(home = homedir(), platform = process.platform) {
  const base = join(home, '.dsh', 'dsh-runtimes', 'dsh-primary-runtime', 'dependencies', 'python');
  return platform === 'win32' ? join(base, 'python.exe') : join(base, 'bin', 'python3');
}

const packageDir = dirname(fileURLToPath(import.meta.url));

// 怎么跑 Python 核心：
// - 开发模式：找到带虚拟环境的项目（配置/环境变量/登记文件/旧默认路径）——照旧，数据在项目 data/。
// - 包模式：从 npm 装进 DSH 时没有项目，用包里的 py/ + DSH 自带的 Python，数据在 ~/.xuyi/data。
export function resolveRuntime(config = {}, { env = process.env, home = homedir(), exists = existsSync,
  platform = process.platform, packageDir: pkg = packageDir } = {}) {
  let registered;
  try { registered = JSON.parse(readFileSync(join(home, '.xuyi', 'project.json'), 'utf8')).projectDir; } catch {}
  const candidates = [config.projectDir, env.XUYI_PROJECT_DIR, registered,
    join(home, 'Documents', 'Codex', 'xuyi-memory')].filter(dir => typeof dir === 'string' && dir.trim());
  const found = candidates.find(dir => exists(venvPython(resolve(dir), platform)));
  const codeDir = join(pkg, 'py');
  if (!found && exists(join(codeDir, 'xuyi', '__init__.py'))) {
    const python = [config.python, env.XUYI_PYTHON].find(p => typeof p === 'string' && p.trim())
      ?? (exists(dshPython(home, platform)) ? dshPython(home, platform) : null);
    const dataDir = resolve(config.dataDir ?? join(home, '.xuyi', 'data'));
    // 语义依赖由 `xuyi prepare` 装进 ~/.xuyi/py-site，量化模型下到 ~/.xuyi/models（见 xuyi/prepare.py）。
    const siteDir = join(home, '.xuyi', 'py-site');
    return { mode: 'packaged', python, dataDir, cwd: dataDir, siteDir, home,
      modelDir: join(home, '.xuyi', 'models', 'bge-small-zh-q8'),
      env: { ...env, PYTHONPATH: [codeDir, siteDir].join(platform === 'win32' ? ';' : ':'),
        PYTHONUTF8: '1', PYTHONNOUSERSITE: '1' } };
  }
  const projectDir = resolve(found ?? candidates[0]);
  return { mode: 'project', projectDir, python: venvPython(projectDir, platform), env,
    dataDir: resolve(config.dataDir ?? join(projectDir, 'data')), cwd: projectDir,
    modelDir: join(projectDir, 'models', 'bge-small-zh') };
}

// Why a core call failed, for diagnosis: command name, error code and the tail of
// Python's stderr only — never the message text, which travels on stdin.
export function recordFailure(dataDir, command, error, stderr, at = new Date()) {
  try {
    const dir = dataDir;
    mkdirSync(dir, { recursive: true });
    const detail = String(stderr ?? '').trim().split(/\r?\n/).slice(-3).join(' | ').slice(0, 400);
    appendFileSync(join(dir, 'adapter-errors.log'),
      `${at.toISOString()} ${command} code=${error?.code ?? '?'} signal=${error?.signal ?? ''} `
      + `killed=${!!error?.killed} ${String(error?.message ?? '').split(/\r?\n/)[0].slice(0, 200)} :: ${detail}\n`);
  } catch {
    // Diagnostics must never break a chat turn.
  }
}

// Windows 虚拟环境的 python.exe 只是个启动器，由它再去找基础解释器。真机上由开始菜单启动的
// DSH 里这个启动器报 "No Python at '"C:\…'"（路径多一个引号），而在命令行里同一个文件能跑。
// 兜底：启动器失败时，改用 pyvenv.cfg 里记录的基础解释器，并把项目与虚拟环境的包目录放进
// PYTHONPATH——不经过启动器，效果相同。
export function fallbackInterpreter(projectDir, platform = process.platform, read = readFileSync) {
  if (platform !== 'win32') return null;
  try {
    const cfg = read(join(projectDir, '.venv', 'pyvenv.cfg'), 'utf8');
    const home = /^home\s*=\s*(.+?)\s*$/m.exec(cfg)?.[1]?.replace(/^"+|"+$/g, '');
    if (!home) return null;
    return { python: join(home, 'python.exe'),
      pythonPath: [projectDir, join(projectDir, '.venv', 'Lib', 'site-packages')].join(';') };
  } catch {
    return null;
  }
}

function run(python, env, cwd, dbPath, args, input, signal) {
  return new Promise((accept, reject) => {
    const child = execFile(python, ['-m', 'xuyi', '--db', dbPath, ...args],
      { cwd, env, timeout: 15000, maxBuffer: 256 * 1024, signal }, (error, stdout, stderr) => {
        if (error) return reject(Object.assign(error, { stderrText: stderr }));
        try { accept(JSON.parse(stdout)); }
        catch { reject(Object.assign(new Error('续忆本地进程返回格式无效'), { format: true })); }
      });
    child.stdin.on('error', () => {});
    child.stdin.end(input);
  });
}

async function callCore(runtime, dbPath, args, input, signal) {
  if (!runtime.python) {
    recordFailure(runtime.dataDir, args[0], { code: 'NO_PYTHON',
      message: '没找到 Python：DSH 自带的运行时不在预期位置，可用插件配置 python 指定' }, '');
    throw new Error('续忆本地进程调用失败');
  }
  try {
    mkdirSync(runtime.cwd, { recursive: true });
    return await run(runtime.python, runtime.env, runtime.cwd, dbPath, args, input, signal);
  } catch (first) {
    if (first.name === 'AbortError' || signal?.aborted) throw new Error('续忆本地进程调用失败');
    if (first.format) throw new Error(first.message);
    recordFailure(runtime.dataDir, args[0], first, first.stderrText);
    const fallback = runtime.mode === 'project' ? fallbackInterpreter(runtime.projectDir) : null;
    if (!fallback) throw new Error('续忆本地进程调用失败');
    try {
      return await run(fallback.python, { ...process.env, PYTHONPATH: fallback.pythonPath, PYTHONUTF8: '1' },
        runtime.cwd, dbPath, args, input, signal);
    } catch (second) {
      if (second.name !== 'AbortError') recordFailure(runtime.dataDir, args[0] + '(fallback)', second, second.stderrText);
      throw new Error(second.format ? second.message : '续忆本地进程调用失败');
    }
  }
}

// ---- 便利贴：由宿主模型按用户实际配置的人设来写 ----
// 看板是单独的本机网页，自己调不了模型；它把"要写哪几条"写成请求文件，这里看到后
// 调用宿主模型，用那个人设的口吻写成一两张小纸条。只在看板打开且内容变化时生成一次，
// 结果写回同一个目录缓存；没有宿主、失败或超时都只是让看板继续用模板。
const noteFreshMs = 30000;
const noteBackoffMs = 300000;
const noteFactLimit = 8;
// 请求没带版本时的兜底；正常以请求里的 writer 为准（版本只在 xuyi/board_bridge.py 定）。
const noteWriter = 2;

export function readJson(file) {
  try { return JSON.parse(readFileSync(file, 'utf8')); } catch { return null; }
}

function writeJson(file, data) {
  mkdirSync(dirname(file), { recursive: true });
  const tmp = `${file}.tmp`;
  writeFileSync(tmp, JSON.stringify(data), 'utf8');
  renameSync(tmp, file);   // replaces an existing file on Windows and POSIX
}

export function noteScopeKey(scope) {
  return createHash('sha256').update(scope).digest('hex').slice(0, 16);
}

export function readNoteRequests(dir) {
  let entries;
  try { entries = readdirSync(dir, { withFileTypes: true }); } catch { return []; }
  const out = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    const folder = join(dir, entry.name);
    let names;
    try {
      names = readdirSync(folder).filter(name => name === 'request.json'
        || /^diary-\d{4}-\d{2}-\d{2}\.request\.json$/.test(name));
    } catch { continue; }
    for (const name of names) {
      const request = readJson(join(folder, name));
      if (!request || typeof request !== 'object') continue;
      if (typeof request.scope !== 'string' || !request.scope.startsWith('qq:c2c:')) continue;
      if (typeof request.fingerprint !== 'string' || typeof request.persona !== 'string'
          || !request.persona.trim()) continue;
      if (!Number.isFinite(Date.parse(request.at))) continue;
      if (request.kind === 'diary') {
        if (!Array.isArray(request.messages) || typeof request.date !== 'string') continue;
      } else if (!Array.isArray(request.facts)) continue;
      out.push({ ...request, kind: request.kind === 'diary' ? 'diary' : 'notes', folder,
        replyFile: join(folder, name === 'request.json' ? 'notes.json'
          : name.replace('.request.json', '.reply.json')) });
    }
  }
  return out;
}

export function noteFacts(request) {
  return (request.facts ?? []).filter(fact => fact && typeof fact.fact === 'string' && fact.fact.trim()
    && typeof fact.kind === 'string').slice(0, noteFactLimit);
}

export function notePrompt(request) {
  const facts = noteFacts(request);
  const lines = facts.map((fact, i) => `${i + 1}. ${fact.fact.trim()}`).join('\n');
  return `现在请你写便利贴。下面 ${facts.length} 条是你刚想起来的事。用你平时说话的口吻，一条写成一张便利贴上的短句。\n`
    + '规矩：一条一张，一张一行。每张 15 到 35 个字——写清「他说的哪件事」加上「你的反应或答案」，'
    + '别只写一个词、一个名字或半句话；便利贴不是发消息，可以比聊天时多说几个字。'
    + '只写纸条上的话，不编号、不加引号、不解释、不寒暄。\n\n'
    + lines;
}

// 模型偶尔会加编号、引号或寒暄；能收拾的收拾，收拾不成"一事实一行"就整批作废（退回模板）。
export function parseNoteLines(text, count) {
  if (typeof text !== 'string') return null;
  const lines = text.split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)
    .map(line => line.replace(/^\s*(?:\d+\s*[.、)．]|[-*•·])\s*/, '')
      .replace(/^[「『"“]/, '').replace(/[」』"”]$/, '').trim())
    .filter(Boolean);
  if (lines.length !== count) return null;
  if (lines.some(line => Array.from(line).length > 40)) return null;
  if (lines.some(line => /^(?:便利贴|以下是|好的|当然|这是|明白)/.test(line))) return null;
  return lines;
}

export async function askForNotes(llm, route, request, { maxTokens = 400, signal, extra = '' } = {}) {
  // 提示词由 Python 侧随请求下发（改语气不用重启 DSH）；旧请求没有就按事实现场拼。
  const prompt = (typeof request.prompt === 'string' && request.prompt.trim())
    ? request.prompt : notePrompt(request);
  let text = '';
  let finish;
  for await (const chunk of llm.stream({
    provider: route.provider,
    model: route.model,
    system: request.persona,
    messages: [{ role: 'user', content: [{ type: 'text', text: prompt + extra }] }],
    maxTokens,
    signal,
  })) {
    if (chunk?.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text;
    else if (chunk?.type === 'finish') finish = chunk.reason;
  }
  if (finish && finish.kind !== 'stop' && finish.kind !== 'max-tokens') {
    throw new Error('模型没有正常写完便利贴');
  }
  return text;
}

// 日记正文：按天的原话交给人格写，分段、只挑有内容的，不编。与便利贴共用同一个桥和缓存规则。
// 请求没带版本时的兜底；正常以请求里的 writer 为准（版本只在 xuyi/board_bridge.py 定）。
const diaryWriter = 2;
const diaryMaxChars = 500;

export async function askForDiary(llm, route, request, { maxTokens = 600, signal, extra = '' } = {}) {
  let text = '';
  let finish;
  for await (const chunk of llm.stream({
    provider: route.provider,
    model: route.model,
    system: request.persona,
    messages: [{ role: 'user', content: [{ type: 'text', text: request.prompt + extra }] }],
    maxTokens,
    signal,
  })) {
    if (chunk?.type === 'text-delta' && typeof chunk.text === 'string') text += chunk.text;
    else if (chunk?.type === 'finish') finish = chunk.reason;
  }
  if (finish && finish.kind !== 'stop' && finish.kind !== 'max-tokens') {
    throw new Error('模型没有正常写完日记');
  }
  return text;
}

// 日记正文的字数（不算空白），与提示词里的"不超过 500 字"同一口径。
export function diaryChars(text) {
  return Array.from(String(text ?? '').replace(/\s/g, '')).length;
}

export function parseDiary(text, maxChars = diaryMaxChars) {
  if (typeof text !== 'string') return null;
  const body = text.trim().replace(/\r\n/g, '\n');
  if (body.length < 20 || diaryChars(body) > maxChars) return null;
  if (/^\s*(?:[#*>]|-\s|\d+\.\s)/m.test(body) || body.includes('**') || body.includes('```')) return null;
  if (/(作为|以下是|好的[，,]|当然可以|这篇日记)/.test(body)) return null;
  const paragraphs = body.split(/\n\s*\n/).map(part => part.trim()).filter(Boolean);
  if (paragraphs.length < 1 || paragraphs.length > 6) return null;
  if (paragraphs.some(part => Array.from(part).length < 10)) return null;
  return paragraphs.join('\n\n');
}

// 看一眼桥目录：该写的写、该跳过的跳过。返回这次真正写成了几张。
export async function sweepBoardNotes({ dir, llm, route, now = Date.now(), maxAge = noteFreshMs,
  backoff = noteBackoffMs, maxTokens = 400, timeoutMs = 12000, signal } = {}) {
  if (!dir || !llm || !route) return 0;
  let written = 0;
  for (const request of readNoteRequests(dir)) {
    const diary = request.kind === 'diary';
    const facts = noteFacts(request);
    if (!diary && !facts.length) continue;
    const replyFile = request.replyFile;
    const reply = readJson(replyFile);
    // 写法版本以请求为准（Python 定）：Python 改动立刻生效、这里要重启 DSH 才生效，
    // 各自维护版本号时旧适配器写回的版本会被判过时，每次打开都重写（真机踩过）。
    const writer = Number.isInteger(request.writer) ? request.writer : (diary ? diaryWriter : noteWriter);
    if (reply?.fingerprint === request.fingerprint) {
      if (!reply.error && reply.writer === writer) continue;   // already written for this exact content
      if (reply.error && now - Date.parse(reply.at ?? 0) < backoff) continue;   // failed recently: wait
    }
    if (now - Date.parse(request.at) > maxAge) continue;  // board stopped asking
    // 写一整天的日记比写便利贴慢，给足时间；其余仍是 12 秒。
    const callTimeout = diary ? Math.max(timeoutMs, 45000) : timeoutMs;
    const timeout = AbortSignal.timeout(callTimeout);
    const callSignal = signal ? AbortSignal.any([signal, timeout]) : timeout;
    try {
      if (diary) {
        const cap = Number.isFinite(request.max_chars) ? Math.min(request.max_chars, diaryMaxChars) : diaryMaxChars;
        const write = extra => askForDiary(llm, route, request,
          { maxTokens: Math.max(maxTokens, 1200), signal: callSignal, extra });
        let draft = await write('');
        let body = parseDiary(draft, cap);
        if (!body) {
          // 写超了或格式不对：带着更正意见再要一次；仍然不行这一天先不出日记，下次打开再试。
          const over = diaryChars(draft) > cap;
          draft = await write(over
            ? `\n\n（刚才那版有 ${diaryChars(draft)} 字，太长了：删到 ${cap} 字以内，只留最有内容的事，格式要求不变。）`
            : '\n\n（刚才那版不合格式：只写日记正文，分段、段与段之间空一行，不要标题、列表、引号或开场白。）');
          body = parseDiary(draft, cap);
        }
        if (!body) throw new Error(diaryChars(draft) > cap ? '日记写得太长' : '日记正文不合规矩');
        writeJson(replyFile, { fingerprint: request.fingerprint, writer, text: body,
          model: route.model, at: new Date(now).toISOString() });
      } else {
        // 太短或不合"一事实一行"时，带着更正意见再要一次；仍然不行就用模板（并记住失败）。
        const attempt = async extra => parseNoteLines(
          await askForNotes(llm, route, request, { maxTokens, signal: callSignal, extra }), facts.length);
        let lines = await attempt('');
        if (!lines || lines.some(line => Array.from(line).length < 12)) {
          lines = await attempt('\n\n（刚才那版不合格：每张要 15 到 35 个字，把话说完整，别只写一个词；仍然一条一行。）');
        }
        if (!lines) throw new Error('模型写的便利贴不合规矩');
        if (lines.some(line => Array.from(line).length < 8)) throw new Error('模型写的便利贴太短');
        writeJson(replyFile, { fingerprint: request.fingerprint, writer,
          notes: facts.map((fact, i) => ({ kind: fact.kind, text: lines[i] })),
          model: route.model, at: new Date(now).toISOString() });
      }
      written++;
    } catch (error) {
      if (signal?.aborted) return written;   // 插件卸载中，不留下失败记录
      writeJson(replyFile, { fingerprint: request.fingerprint, writer, error: true,
        reason: String(error?.message ?? error).slice(0, 200),
        at: new Date(now).toISOString() });
    }
  }
  return written;
}

function routeOf(candidate) {
  return candidate && typeof candidate.provider === 'string' && typeof candidate.model === 'string'
    ? { provider: candidate.provider, model: candidate.model } : null;
}

// 写便利贴用哪条模型路由：显式配置 → 这轮聊天实际用的（从 request/header 学到，最准）
// → 宿主默认模型。三者都没有就安静地不写，看板继续用模板。
export function resolveNoteRoute(ctx, configured, learned) {
  const explicit = routeOf(configured) ?? routeOf(learned);
  if (explicit) return explicit;
  try {
    const selection = ctx?.get?.('agentDefaultModel')?.currentSelection?.();
    return routeOf(selection);
  } catch {
    return null;
  }
}

export function startBoardNotes(ctx, { dir, interval = 2000, route, maxTokens = 400,
  timeoutMs = 12000, warn = () => {} } = {}) {
  const lifetime = new AbortController();
  let running = false;
  let warned = false;
  const tick = async () => {
    if (running || lifetime.signal.aborted) return;
    running = true;
    try {
      const requests = readNoteRequests(dir);
      if (!requests.length) return;
      const llm = ctx?.get?.('llm');
      const chosen = typeof route === 'function' ? route() : route;
      if (!llm || !chosen) {
        if (!warned) { warned = true; warn('看板想请宿主模型写便利贴，但拿不到模型路由；先用模板'); }
        return;
      }
      await sweepBoardNotes({ dir, llm, route: chosen, maxTokens, timeoutMs, signal: lifetime.signal });
    } catch {
      // 便利贴写不成不影响聊天；下一轮再看。
    } finally {
      running = false;
    }
  };
  const timer = setInterval(tick, interval);
  timer.unref?.();
  tick();
  return () => { lifetime.abort(); clearInterval(timer); };
}

// ---- 日记本放进 DSH：会话页头的「日记本」打开 /xuyi/，这里转发给后台的 Python 看板 ----
// 看板进程由适配器在第一次打开时拉起（--announce 告诉我们端口和口令），之后一直复用；
// 口令只在两个进程之间传，页面里没有。DSH 退出时看板凭 --parent-pid 自己退出。

const boardPaths = new Set(['/', '/font/ZCOOLKuaiLe.ttf', '/api/contacts', '/api/board', '/api/diary', '/export', '/api/override',
  '/api/backup', '/api/selfcheck', '/api/restore']);
// 导入备份（换电脑带走）会往档案里写：和引导写接口一样要自定义头 + JSON，且体积上限单独放宽。
const boardWrites = new Set(['/api/override', '/api/restore']);
const restoreLimit = 64 * 1024 * 1024;
const loopbackHost = /^(?:127\.0\.0\.1|localhost|\[::1\])(?::\d+)?$/;
const passHeaders = ['content-type', 'content-length', 'cache-control', 'content-disposition', 'x-content-type-options'];

function startBoardProcess(python, env, cwd, dbPath, timeoutMs = 20000) {
  return new Promise((accept, reject) => {
    let child;
    try {
      mkdirSync(cwd, { recursive: true });
      child = spawn(python, ['-m', 'xuyi', '--db', dbPath, 'board', '--announce',
        '--parent-pid', String(process.pid)], { cwd, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true });
    } catch (error) { reject(error); return; }
    let out = '', err = '';
    const timer = setTimeout(() => { child.kill(); reject(new Error('看板启动超时')); }, timeoutMs);
    child.stderr.on('data', chunk => { err = (err + chunk).slice(-2000); });
    child.stdout.on('data', chunk => {
      out += chunk;
      const line = out.split(/\r?\n/)[0];
      if (!out.includes('\n')) return;
      clearTimeout(timer);
      try {
        const info = JSON.parse(line);
        if (typeof info.url !== 'string' || typeof info.token !== 'string') throw new Error('bad');
        accept({ url: info.url, token: info.token, child });
      } catch { child.kill(); reject(new Error('看板没有给出地址')); }
    });
    child.on('error', error => { clearTimeout(timer); reject(error); });
    child.on('exit', code => { clearTimeout(timer); reject(Object.assign(new Error(`看板进程退出（${code}）`), { stderrText: err })); });
  });
}

function askBoard(board, path) {
  return new Promise(done => {
    const req = httpRequest(board.url + path, { timeout: 3000 }, res => { res.resume(); res.on('end', done); });
    req.on('error', () => done());
    req.on('timeout', () => { req.destroy(); done(); });
    req.end();
  });
}

// 一个看板进程，懒启动、复用；进程没了下次打开时重启。
export function boardManager(runtime, dbPath) {
  let current = null;
  const launch = async () => {
    const attempts = [[runtime.python, runtime.env]];
    const fallback = runtime.mode === 'project' ? fallbackInterpreter(runtime.projectDir) : null;
    if (fallback) attempts.push([fallback.python, { ...process.env, PYTHONPATH: fallback.pythonPath, PYTHONUTF8: '1' }]);
    let last;
    for (const [python, env] of attempts) {
      if (!python) continue;
      try { return await startBoardProcess(python, env, runtime.cwd, dbPath); }
      catch (error) { last = error; recordFailure(runtime.dataDir, 'board', error, error.stderrText); }
    }
    throw last ?? new Error('没找到 Python');
  };
  const get = () => {
    if (!current) {
      current = launch().then(board => {
        board.child.on('exit', () => { if (current === promise) current = null; });
        return board;
      });
      const promise = current;
      current.catch(() => { if (current === promise) current = null; });
    }
    return current;
  };
  const stop = async () => {
    const running = current;
    current = null;
    if (!running) return;
    try {
      const board = await running;
      await askBoard(board, `/api/shutdown?t=${encodeURIComponent(board.token)}`);
      if (board.child.exitCode === null) {
        await new Promise(done => { const timer = setTimeout(done, 3000); board.child.once('exit', () => { clearTimeout(timer); done(); }); });
      }
      if (board.child.exitCode === null) board.child.kill();
    } catch { /* 没起来就不用关 */ }
  };
  return { get, stop };
}

export const unavailablePage = '<!doctype html><meta charset="utf-8"><title>日记本</title>'
  + '<body style="font:15px/1.8 system-ui,sans-serif;padding:48px;color:#555;background:#f4f1ea">'
  + '<h2 style="font-weight:500">日记本暂时打不开</h2><p>续忆的本地程序没能启动。可以关掉再打开一次；'
  + '还是不行的话，原因记在 ~/.xuyi/data/adapter-errors.log。</p></body>';

// ---- 首次运行准备：包模式下后台装语义依赖、下量化中文模型（约 42MB，xuyi/prepare.py） ----
// 没准备好之前照样能用（自动召回本来就是词法，工具查询也退回词法）。每次 DSH 启动最多跑一次。
export function maybePrepare(runtime, { exists = existsSync, launch = spawn, enabled = true } = {}) {
  if (!enabled || runtime.mode !== 'packaged' || !runtime.python) return false;
  if (exists(join(runtime.modelDir, 'source.json')) && exists(join(runtime.siteDir, 'onnxruntime'))) return false;
  try {
    mkdirSync(runtime.cwd, { recursive: true });
    // 明确带上 home：不能让 Python 自己猜（测试或配置了 homeDir 时会落到真实用户目录）。
    const child = launch(runtime.python, ['-m', 'xuyi', 'prepare', '--home', runtime.home],
      { cwd: runtime.cwd, env: runtime.env, stdio: 'ignore', windowsHide: true });
    child?.on?.('error', () => {});
    child?.unref?.();
    return true;
  } catch {
    return false;
  }
}

function prepareStateOf(runtime) {
  if (runtime.mode !== 'packaged') return { stage: 'done', percent: 100, ready: true, bundled: true };
  const ready = existsSync(join(runtime.modelDir, 'source.json')) && existsSync(join(runtime.siteDir, 'onnxruntime'));
  const progress = readJson(join(runtime.dataDir, 'prepare.json')) ?? {};
  return { stage: ready ? 'done' : progress.stage ?? 'waiting', percent: ready ? 100 : progress.percent ?? 0,
    error: ready ? undefined : progress.error, ready };
}

// ---- 首次引导：选人设、装/打开 QQ 插件、开聊（日记本里的「设置」面板调用） ----
const QQ_BUNDLE = '@tencent-connect/dsh-qqbot';

function readBody(req, limit = 64 * 1024) {
  return new Promise((accept, reject) => {
    let size = 0;
    const chunks = [];
    req.on('data', chunk => {
      size += chunk.length;
      if (size > limit) { reject(Object.assign(new Error('请求太大'), { status: 413 })); req.destroy?.(); return; }
      chunks.push(Buffer.from(chunk));
    });
    req.on('end', () => accept(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

// 装在 profile 里时，包在 <profile>/node_modules/dsh-xuyi-memory。
export function profileDirOf(pkg = packageDir, home = homedir()) {
  return dirname(pkg).endsWith('node_modules') ? dirname(dirname(pkg)) : join(home, '.dsh', 'profiles', 'desktop');
}

export function createSetup({ profileDir, getPluginManager, core, prefsPath, prepareState = () => null }) {
  const qqStatus = async () => {
    const manager = getPluginManager?.();
    if (typeof manager?.listBundles === 'function') {
      try {
        const found = (await manager.listBundles()).find(bundle => bundle?.name === QQ_BUNDLE);
        return { installed: Boolean(found && found.installed !== false && found.availability !== 'missing'),
          enabled: Boolean(found?.enabled), canInstall: typeof manager.installBundle === 'function' };
      } catch { /* 退回读文件 */ }
    }
    const deps = readJson(join(profileDir, 'package.json'))?.dependencies ?? {};
    return { installed: Object.hasOwn(deps, QQ_BUNDLE), enabled: Object.hasOwn(deps, QQ_BUNDLE), canInstall: false };
  };
  const chatted = () => {
    const ids = readJson(prefsPath)?.sessionIds;
    return Boolean(ids && typeof ids === 'object' && Object.keys(ids).some(key => PEER.test(key)));
  };
  return {
    async state() {
      const [found, qq] = await Promise.all([core(['setup-state', '--profile', profileDir]), qqStatus()]);
      return { ...found, qq, chatted: chatted(), prepare: prepareState() };
    },
    async persona(body) {
      const qq = await qqStatus();
      const tail = ['--apply', '--profile', profileDir, ...(qq.installed ? ['--create'] : []),
        // 群里怎么回：只认这两个值，其它一律不改宿主设置。
        ...(['mention', 'all'].includes(body?.groupReply) ? ['--group-reply', body.groupReply] : [])];
      if (typeof body?.name === 'string') {
        if (!/^[a-z0-9_-]{1,40}$/i.test(body.name)) throw new Error('人设名不对');
        return core(['persona', body.name, ...tail]);
      }
      const text = typeof body?.text === 'string' ? body.text.trim() : '';
      if (!text || text.length > 4000) throw new Error('人设要写 1 到 4000 字');
      const folder = mkdtempSync(join(tmpdir(), 'xuyi-persona-'));
      const file = join(folder, 'persona.txt');
      try {
        writeFileSync(file, text, 'utf8');
        return await core(['persona', '--file', file, ...tail]);
      } finally { rmSync(folder, { recursive: true, force: true }); }
    },
    async diaryHour(body) {
      const hour = body?.hour;
      if (!Number.isInteger(hour) || hour < 18 || hour > 23) throw new Error('写日记时间只能选 18–23 点');
      return core(['diary-hour', '--set', String(hour)]);
    },
    async installQq() {
      const manager = getPluginManager?.();
      if (typeof manager?.installBundle !== 'function') {
        throw new Error(`这个版本的 DSH 不能从这里安装：请打开左侧插件页，点「添加插件」，填 ${QQ_BUNDLE}`);
      }
      const result = await manager.installBundle(QQ_BUNDLE, { enabled: true });
      return { installed: true, application: result?.application ?? null, restart: result?.application === 'restart-required' };
    },
  };
}

const setupWrites = new Set(['/setup/persona', '/setup/install-qq', '/setup/diary-hour']);

// ---- 到点写日记：DSH 开着时每隔几分钟问一次 Python（xuyi diary-due），该不该写、几点写由它按用户设置判断 ----
export function startDiaryClock(run, { firstDelay = 15000, interval = 5 * 60 * 1000 } = {}) {
  let stopped = false;
  let timer = null;
  const tick = async () => {
    if (stopped) return;
    try { await run(); } catch { /* 这一轮不写，下一轮再试；失败原因由 callCore 记日志 */ }
    if (!stopped) { timer = setTimeout(tick, interval); timer.unref?.(); }
  };
  timer = setTimeout(tick, firstDelay);
  timer.unref?.();
  return () => { stopped = true; clearTimeout(timer); };
}

export function createBoardRoute(getBoard, setup = null) {
  return async (req, res) => {
    const reply = (status, body, type = 'text/plain; charset=utf-8', extra = {}) => {
      res.writeHead(status, { 'Content-Type': type, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff', ...extra });
      res.end(body);
    };
    const json = (status, data) => reply(status, JSON.stringify(data), 'application/json; charset=utf-8');
    if (!loopbackHost.test(String(req.headers?.host ?? ''))) return reply(403, '只允许本机打开日记本');
    const raw = String(req.url ?? '');
    let parsed;
    try { parsed = new URL(raw, 'http://local'); } catch { return reply(404, '没有这个页面'); }
    if (raw.split('?')[0].includes('..') || !parsed.pathname.startsWith('/xuyi')) return reply(404, '没有这个页面');
    const rest = parsed.pathname.slice('/xuyi'.length);
    if (setup && setupWrites.has(rest)) {
      if (req.method !== 'POST') return reply(405, '只收 POST', undefined, { Allow: 'POST' });
      // 别的网页能发表单，但带不了自定义头、也不会被我们答复预检：这两条挡住跨站提交。
      if (req.headers['x-xuyi-setup'] !== '1') return reply(403, '缺少引导请求头');
      if (!/^application\/json(?:\s*;|$)/i.test(String(req.headers['content-type'] ?? ''))) return reply(415, '要 JSON');
      try {
        const body = JSON.parse((await readBody(req)) || '{}');
        const action = rest === '/setup/persona' ? setup.persona(body)
          : rest === '/setup/diary-hour' ? setup.diaryHour(body) : setup.installQq();
        return json(200, await action);
      } catch (error) {
        return json(error?.status ?? 400, { error: String(error?.message ?? error).split(/\r?\n/)[0].slice(0, 300) });
      }
    }
    if (req.method !== 'GET' && req.method !== 'HEAD'
        && !(req.method === 'POST' && boardWrites.has(rest))) return reply(405, '只读', undefined, { Allow: 'GET, HEAD, POST' });
    if (req.method === 'POST' && rest === '/api/restore') {
      if (req.headers['x-xuyi-setup'] !== '1') return reply(403, '缺少引导请求头');
      if (!/^application\/json(?:\s*;|$)/i.test(String(req.headers['content-type'] ?? ''))) return reply(415, '要 JSON');
    }
    if (setup && rest === '/setup/state') {
      try { return json(200, await setup.state()); }
      catch (error) { return json(500, { error: String(error?.message ?? error).slice(0, 300) }); }
    }
    if (parsed.pathname === '/xuyi') return reply(302, '', undefined, { Location: '/xuyi/' });
    if (!boardPaths.has(rest)) return reply(404, '没有这个页面');
    let board;
    try { board = await getBoard(); }
    catch { return reply(503, unavailablePage, 'text/html; charset=utf-8'); }
    const query = new URLSearchParams(parsed.search);
    query.delete('t');
    if (rest !== '/font/ZCOOLKuaiLe.ttf') query.set('t', board.token);
    const target = board.url + rest + (query.size ? '?' + query : '');
    const payload = req.method === 'POST' ? await readBody(req, rest === '/api/restore' ? restoreLimit : 4096) : undefined;
    await new Promise(done => {
      const requestHeaders = req.method === 'POST'
        ? { 'Content-Type': String(req.headers['content-type'] ?? 'application/json') } : undefined;
      const upstream = httpRequest(target, { method: req.method, timeout: 30000,
        headers: requestHeaders }, answer => {
        const headers = {};
        for (const name of passHeaders) if (answer.headers[name] !== undefined) headers[name] = answer.headers[name];
        res.writeHead(answer.statusCode ?? 502, headers);
        answer.on('data', chunk => res.write(chunk));
        answer.on('end', () => { res.end(); done(); });
        answer.on('error', () => { res.end(); done(); });
      });
      upstream.on('timeout', () => upstream.destroy(new Error('timeout')));
      upstream.on('error', () => { reply(503, unavailablePage, 'text/html; charset=utf-8'); done(); });
      upstream.end(payload);
    });
  };
}

export function create(ctx, config = {}, host) {
  const { runtime, dbPath, prefs, peerOf, routeFile, lifetime, state, warn } = host;
  const scopeOf = id => {
    const found = resolveScope(prefs, id);
    if (!found) return null;
    return (found.kind === 'group' ? 'qq:group:' : 'qq:c2c:') + found.hash;
  };
  const autoDeadline = Number.isFinite(config.autoDeadline) ? config.autoDeadline : defaultAutoDeadline;
  const random = typeof config.bubbleRandom === 'function' ? config.bubbleRandom : Math.random;
  const board = config.diary === false ? null : boardManager(runtime, dbPath);
  const setup = board ? createSetup({ profileDir: resolve(config.profileDir ?? profileDirOf()), prefsPath: prefs,
    prepareState: () => prepareStateOf(runtime),
    getPluginManager: () => { try { return ctx.get?.('pluginManager'); } catch { return undefined; } },
    core: args => callCore(runtime, dbPath, args, undefined, lifetime.signal) }) : null;
  const route = board ? createBoardRoute(board.get, setup) : null;
  let stopBoardNotes = null;
  let stopClock = null;

  const tickClock = () => lifetime.signal.aborted ? undefined
    : callCore(runtime, dbPath, ['diary-due'], undefined, lifetime.signal);

  function start() {
    if (config.diaryClock !== false) {
      // 到了用户设的钟点（默认晚上十点）把当天写成日记并定稿；DSH 没开错过的，补写前一天。
      stopClock = startDiaryClock(tickClock);
    }
    if (config.boardNotes !== false) {
      // 看板请求落在数据库旁边；空目录时只是每两秒 stat 一下。
      stopBoardNotes = startBoardNotes(ctx, {
        dir: join(dirname(dbPath), 'board-bridge'),
        interval: Number.isFinite(config.boardNotesInterval) ? config.boardNotesInterval : 2000,
        route: () => resolveNoteRoute(ctx, config.boardNotesModel, state.learnedRoute),
        maxTokens: Number.isFinite(config.noteMaxTokens) ? config.noteMaxTokens : 400,
        timeoutMs: Number.isFinite(config.noteTimeoutMs) ? config.noteTimeoutMs : 12000,
        warn,
      });
    }
  }

  function dispose() {
    stopClock?.();
    stopClock = null;
    stopBoardNotes?.();
    stopBoardNotes = null;
    board?.stop();
  }

  function handleRoute(req, res) {
    return route ? route(req, res) : undefined;
  }

  function onSessionEvent(session, event) {
    const scope = scopeOf(session?.id);
    if (event?.type === 'turn/end') state.turns.delete(session?.id);
    if (!scope || lifetime.signal.aborted) return;
    if (event.type === 'request/header') {
      const chosen = routeOf(event.data?.header?.config);
      if (chosen && (state.learnedRoute?.provider !== chosen.provider || state.learnedRoute?.model !== chosen.model)) {
        state.learnedRoute = chosen;
        try { writeJson(routeFile, { ...chosen, at: new Date().toISOString() }); } catch { /* 只是提示 */ }
      }
    }
    if (event.type === 'turn/start') {
      // One state snapshot per session (on by default; sessionSnapshot:false turns it off).
      // Real chat showed why: after a new session she greeted "好久不见" minutes after the
      // previous one ended. Eligibility survives until a
      // pre-step actually attempts it, so a write backlog cannot swallow it.
      state.turns.set(session.id, { autoUsed: false, toolCalls: 0, peer: scope,
        snapshot: config.sessionSnapshot !== false && !state.snapshotted.has(session.id)
          && !scope.startsWith('qq:group:') });
    }
    // 群聊：一条消息里带好几行历史，拆成多行各自归档（发送人各自署名）。
    const rows = scope.startsWith('qq:group:')
      ? archiveGroupEvents(scope.slice('qq:group:'.length), session.id, event)
      : [archiveEvent(scope.slice('qq:c2c:'.length), session.id, event)].filter(Boolean);
    if (!rows.length) return;
    if (state.queueSize >= 128) {
      warn('续忆写入积压，跳过一条会话事件');
      return;
    }
    state.queueSize++;
    state.pending = state.pending.then(async () => {
      if (!lifetime.signal.aborted) await callCore(runtime, dbPath, ['ingest', '-'], JSON.stringify(rows), lifetime.signal);
    }).catch(() => {
      if (!lifetime.signal.aborted) warn('续忆未能保存一条会话事件');
    }).finally(() => state.queueSize--);
  }

  function bubbleText(agent) {
    const turn = state.turns.get(agent?.id);
    return turn?.bubbles && !lifetime.signal.aborted && scopeOf(agent.id) === turn.peer ? bubbleHint(turn.bubbles) : '';
  }

  async function bubblePreStep({ agent, messages }, next) {
    const decision = await next();
    const turn = state.turns.get(agent?.id);
    if (decision.kind !== 'enter' || !turn || turn.bubbles || lifetime.signal.aborted) return decision;
    const mine = (messages ?? []).filter(message => message?.source?.kind === 'user').at(-1);
    const text = Array.isArray(mine?.content)
      ? mine.content.filter(block => block?.type === 'text').map(block => block.text).join('\n') : '';
    const recent = state.picked.get(agent.id) ?? [];
    turn.bubbles = pickBubbleCount(text, recent, random);
    state.picked.set(agent.id, [...recent, turn.bubbles].slice(-4));
    return decision;
  }

  function recallText(agent) {
    const turn = state.turns.get(agent?.id);
    if (!turn?.autoContext || lifetime.signal.aborted || scopeOf(agent.id) !== turn.peer) return '';
    // 开场快照只是背景（上次聊到哪），不占本轮查询：新会话第一句就是问题时，模型仍要能查。
    if (!turn.autoSnapshot) turn.autoUsed = true;   // 问句触发的自动重点算本轮一次查询
    return turn.autoContext;
  }

  async function recallPreStep({ agent, messages, signal }, next) {
    // DSH assembles prompt sections before this hook and refreshes runtime contexts after it.
    const decision = await next();
    const sessionId = agent?.id;
    const turn = state.turns.get(sessionId);
    if (decision.kind !== 'enter' || !turn || turn.toolCalls > 0 || turn.autoUsed || turn.autoContext
        || lifetime.signal.aborted || signal.aborted) return decision;
    // L1：群里多人说话，自动重点/开场快照容易串；群聊只保留「查得到」（工具），不自动注入。
    if (turn.peer.startsWith('qq:group:')) return decision;
    // The host enters this hook right after emitting the user message, so its own write is
    // usually still in flight. Wait for it (bounded); skipping here meant never auto-recalling.
    if (state.queueSize > 0) {
      let timer;
      const drained = await Promise.race([state.pending.then(() => true),
        new Promise(done => { timer = setTimeout(done, writeWait, false); })]);
      clearTimeout(timer);
      if (!drained || state.queueSize > 0 || state.turns.get(sessionId) !== turn
          || lifetime.signal.aborted || signal.aborted) return decision;
    }
    const scope = scopeOf(sessionId);
    if (!scope || turn.peer !== scope) return decision;
    const users = messages.filter(message => message.source?.kind === 'user');
    if (users.length !== 1 || !decision.messages.includes(users[0])) return decision;
    const query = userText(users[0], config.autoRelated === true);
    if (!query && !turn.snapshot) return decision;
    turn.snapshot = false;
    if (!query) state.snapshotted.add(sessionId);
    const lookupSignal = AbortSignal.any([signal, lifetime.signal, AbortSignal.timeout(autoDeadline)]);
    try {
      const args = query
        ? ['recall', scope, '--highlight-only', '--budget', String(autoBudget),
           ...(config.autoRelated === true ? ['--related'] : []), '--', query]
        : ['snapshot', scope, '--budget', String(snapshotBudget), '--session', String(sessionId)];
      const budget = query ? autoBudget : snapshotBudget;
      const result = await callCore(runtime, dbPath, args, undefined, lookupSignal);
      if (!lookupSignal.aborted && state.turns.get(sessionId) === turn && scopeOf(sessionId) === scope) {
        turn.autoContext = checkedContext(result, budget);
        turn.autoSnapshot = !query;
      }
    } catch {
      // Leave the manual lookup available if the automatic read fails or times out.
    }
    return decision;
  }

  function isPrivatePeer(agent) {
    // 名字保留：私聊和群聊都算「已识别的 QQ 会话」。
    return Boolean(scopeOf(agent?.id));
  }

  async function toolExecute(args, exec) {
    const sessionId = exec?.agent?.id;
    const scope = scopeOf(sessionId);
    const turn = state.turns.get(sessionId);
    if (!scope || !turn || turn.peer !== scope || lifetime.signal.aborted) return '续忆只在已识别的 QQ 会话轮次开放。';
    // 每轮最多查 maxLookups 次（默认 2，自动重点也算一次）：真机上模型查完常想换个关键词再查，
    // 一次就拦会让它只能凭第一次的结果答。快照不算。
    const maxLookups = Math.min(3, Math.max(1, Number.isInteger(config.maxLookups) ? config.maxLookups : 2));
    if ((turn.autoUsed ? 1 : 0) + turn.toolCalls >= maxLookups) return `本轮已经查过${maxLookups === 1 ? '一' : maxLookups === 2 ? '两' : '三'}次续忆。`;
    const query = typeof args?.query === 'string' ? args.query.trim() : '';
    if (!query || Array.from(query).length > 120) return '请使用 1 到 120 字的查询。';
    turn.toolCalls++;
    const signal = exec.signal ? AbortSignal.any([exec.signal, lifetime.signal]) : lifetime.signal;
    try {
      await state.pending;
      if (state.turns.get(sessionId) !== turn || scopeOf(sessionId) !== scope || signal.aborted) return '会话已切换或查询已取消。';
      // 模型只在真的在本机时才用；缺了就词法（Python 侧也会兜底：缺依赖时同样退回词法）。
      const model = existsSync(join(runtime.modelDir, 'model.onnx')) ? ['--model', runtime.modelDir] : [];
      const result = await callCore(runtime, dbPath, ['recall', scope, '--budget',
        String(toolBudget), '--focus', ...model, '--', query], undefined, signal);
      if (state.turns.get(sessionId) !== turn || scopeOf(sessionId) !== scope || signal.aborted) return '会话已切换或查询已取消。';
      return checkedContext(result, toolBudget) || '这次查询没有找到可用原文。';
    } catch {
      return '续忆查询暂时不可用。';
    }
  }

  return { start, dispose, handleRoute, onSessionEvent, bubbleText, bubblePreStep,
    recallText, recallPreStep, isPrivatePeer, toolExecute, tickClock };
}
