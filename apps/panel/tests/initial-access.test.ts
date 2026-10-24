import test from 'node:test';
import assert from 'node:assert/strict';
import { initialAccessStatus } from '../src/shared/validation.ts';
import { accessBlocked, accessExecute, accessPreview, DAY } from '../src/ui/workspace/model.ts';
import type { ConsequencePlan, InitialAccessStatus, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const other: Scope = { ...scope, memory_space_id: 'alpha-test', memory_mode: 'test' };
const member = (id: string, available = false) => ({ capability_id: id, summary: 'Возможность ' + id, areas: ['skills'], available,
  expires_at: available ? '2026-10-31T00:00:00Z' : null });
const status = (over: Partial<InitialAccessStatus> = {}): InitialAccessStatus => ({
  scope, configured: true, ready: true, max_duration_seconds: 7 * DAY,
  members: [member('workplace.knowledge'), member('workplace.skills', true)], ...over,
} as InitialAccessStatus);

test('initial access status is accepted only for the selected area and bounded members', () => {
  assert.equal(initialAccessStatus(status(), scope).members.length, 2);
  assert.throws(() => initialAccessStatus(status(), other));
  assert.throws(() => initialAccessStatus(status({ members: Array.from({ length: 9 }, (_, i) => member('c' + i)) } as never), scope));
  assert.throws(() => initialAccessStatus({ ...status(), members: [{ ...member('x'), areas: ['secrets'] }] }, scope));
  assert.throws(() => initialAccessStatus({ ...status(), max_duration_seconds: -1 }, scope));
});

test('one plan for the set: duration in seconds, fixed target, plan pinned on execute', () => {
  const preview = accessPreview(scope, 3, 'Только своё.', 'r-1');
  assert.equal(preview.type, 'workplace.access.preview');
  assert.equal(preview.target, 'initial-access');
  assert.deepEqual(preview.arguments, { schema_id: 'workplace.access-args', data: { duration_seconds: 3 * DAY, constraints: 'Только своё.' } });
  const plan = { id: 'plan-1', version: 1 } as ConsequencePlan;
  assert.deepEqual(accessExecute(scope, plan, 'r-2').arguments, { plan_id: 'plan-1', plan_version: 1 });
});

test('preparing is closed when the set is not configured, not ready or the term exceeds the ceiling', () => {
  assert.equal(accessBlocked(status(), 7), null);
  assert.match(accessBlocked(status(), 8)!, /потолка/);
  assert.match(accessBlocked(status(), 0)!, /потолка/);
  assert.match(accessBlocked(status({ configured: false }), 1)!, /не настроен/);
  assert.match(accessBlocked(status({ ready: false }), 1)!, /готовы к работе/);
  assert.ok(accessBlocked(undefined, 1));
});
