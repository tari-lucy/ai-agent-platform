import test from 'node:test';
import assert from 'node:assert/strict';
import { harnessStatus } from '../src/shared/validation.ts';
import type { Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const base = { scope, enabled: true, ready: true, url: 'http://127.0.0.1:4096/', agent_name: 'Alpha', calls_today: 0 };

test('live connections are optional and validated one by one', () => {
  assert.deepEqual(harnessStatus(base, scope).live ?? [], []);
  const live = [{ name: 'avatar', state: 'on', detail: '' }, { name: 'twitch', state: 'down', detail: 'нет связи' }];
  assert.equal(harnessStatus({ ...base, live }, scope).live?.length, 2);
});

test('an unknown link, state or oversized detail is refused', () => {
  for (const bad of [
    [{ name: 'camera', state: 'on', detail: '' }],
    [{ name: 'obs', state: 'broken', detail: '' }],
    [{ name: 'obs', state: 'on', detail: 'x'.repeat(121) }],
    'not a list',
    Array.from({ length: 9 }, () => ({ name: 'obs', state: 'on', detail: '' })),
  ]) assert.throws(() => harnessStatus({ ...base, live: bad }, scope));
});
