import test from 'node:test';
import assert from 'node:assert/strict';
import { loadStartMode, saveStartMode } from '../src/ui/workspace/model.ts';

function memory(initial: Record<string, string> = {}) {
  const data = { ...initial };
  return {
    getItem: (key: string) => data[key] ?? null,
    setItem: (key: string, value: string) => { data[key] = value; },
  };
}

test('the start mode defaults to observation and a chosen autonomous mode is kept', () => {
  const store = memory();
  assert.equal(loadStartMode(store), 'observation');
  saveStartMode(store, 'autonomous');
  assert.equal(loadStartMode(store), 'autonomous');
  saveStartMode(store, 'observation');
  assert.equal(loadStartMode(store), 'observation');
});

test('a broken or missing storage never breaks the screen', () => {
  assert.equal(loadStartMode(undefined), 'observation');
  assert.equal(loadStartMode(memory({ 'ai-panel.start-autonomy': 'something else' })), 'observation');
  const broken = {
    getItem: () => { throw new Error('blocked'); },
    setItem: () => { throw new Error('blocked'); },
  };
  assert.equal(loadStartMode(broken), 'observation');
  assert.doesNotThrow(() => saveStartMode(broken, 'autonomous'));
});
