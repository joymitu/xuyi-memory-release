import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { isMemoryQuestion } from './logic.js';

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
