// Chat rhythm: a pause before the 2nd and 3rd bubble of one reply, like a person typing.
//
// The official QQ plugin sends every bubble back to back (`for chunk … await bot.sendMarkdown`)
// and has no interval setting. Its bot is an instance of the QQ SDK's `QQBot`, so the only way to add
// a pause without editing third-party files is to wrap `QQBot.prototype.sendMarkdown` at runtime.
// That depends on the SDK's internals, so every step here fails quiet: when the method is missing or
// already wrapped nothing is installed, and bubbles go out exactly as before.

const PATCHED = Symbol.for('xuyi.rhythm.sendMarkdown');
const BURST_MS = 30_000;       // bubbles of one reply are sent within moments; this is generous
const REMEMBER = 500;

// Roughly how long a person takes to type this bubble, with some jitter.
export function typingDelay(content, random = Math.random) {
  const length = Array.from(String(content ?? '')).length;
  const base = 450 + length * 90;
  const jitter = 0.75 + random() * 0.5;
  return Math.round(Math.min(1800, Math.max(600, base * jitter)));
}

export function installRhythm(QQBot, { sleep = ms => new Promise(done => setTimeout(done, ms)),
  random = Math.random, now = Date.now } = {}) {
  const proto = QQBot?.prototype;
  if (!proto || typeof proto.sendMarkdown !== 'function' || proto.sendMarkdown[PATCHED]) return null;
  const original = proto.sendMarkdown;
  const lastBubble = new Map();   // reply (scope:target:msgId) → when its previous bubble went out
  const wrapped = async function sendMarkdown(target, content, opts) {
    const key = `${target?.scope}:${target?.targetId}:${target?.msgId ?? ''}`;
    const previous = lastBubble.get(key);
    if (previous !== undefined && now() - previous < BURST_MS && String(content ?? '').trim()) {
      try { await sleep(typingDelay(content, random)); } catch { /* a failed pause must not lose the message */ }
    }
    lastBubble.set(key, now());
    if (lastBubble.size > REMEMBER) lastBubble.delete(lastBubble.keys().next().value);
    return original.call(this, target, content, opts);
  };
  wrapped[PATCHED] = true;
  proto.sendMarkdown = wrapped;
  return () => { if (proto.sendMarkdown === wrapped) proto.sendMarkdown = original; };
}
