import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isAboutMe, isMemoryQuestion } from './logic.js';

const fixture = join(dirname(dirname(fileURLToPath(import.meta.url))), 'tests', 'fixtures', 'memory_questions.json');

test('every question a family can answer passes the auto-recall gate', () => {
  const { questions } = JSON.parse(readFileSync(fixture, 'utf8'));
  const missed = questions.filter(question => !isMemoryQuestion(question));
  assert.deepEqual(missed, []);
});

test('plain chat does not trigger auto recall', () => {
  for (const text of ['今天好累', '哈哈哈笑死', '我最爱的水果是芒果', '周末去爬山了']) {
    assert.equal(isMemoryQuestion(text), false, text);
  }
});

test('questions about the user themself pass the about-me gate', () => {
  for (const text of ['我老公叫啥？', '我多高？', '我还抽烟吗？', '我是做什么工作的？', '我的猫都叫什么？',
    '我明天要去看牙吗？', '这周末我要干嘛？', '你还记得我老家是哪的吗', '我在读什么书？']) {
    assert.equal(isAboutMe(text), true, text);
  }
});

test('asking for help, asking her, or plain chat is not an about-me question', () => {
  for (const text of ['我该怎么办？', '给我推荐点提神的饮料呗', '你最喜欢什么零食？', '我们去哪玩？', '今天好累',
    '我今天好累啊，你在干嘛呢？', '我刚到家，你吃饭了吗', '你觉得我穿什么好？', '我最爱的水果是芒果']) {
    assert.equal(isAboutMe(text), false, text);
  }
});

test('colloquial self words and a named pet or friend pass the about-me gate', () => {
  for (const text of ['俺老家是哪的来着？', '咱家狗叫啥', '泡泡还好吗？', '阿花最近怎么样']) {
    assert.equal(isAboutMe(text), true, text);
  }
  for (const text of ['天气怎么样？', '你最近还好吗', '我们最近怎么样了']) {
    assert.equal(isAboutMe(text), false, text);
  }
});
