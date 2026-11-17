import test from 'node:test';
import assert from 'node:assert/strict';
import { ago, attention, budgetLine, gmSummary, headline, plan } from '../src/ui/now/model.ts';
import type { RunSession, Ticket, WorkTask } from '../src/shared/contracts.ts';

const run = { state: 'ACTIVE', activity: 'OPEN', gm_paused: false, unconfirmed_resource_ids: [] } as unknown as RunSession;
const at = (minutes: number) => new Date(Date.UTC(2026, 10, 14, 12, 0) - minutes * 60_000).toISOString();

test('the headline is one honest word per run state', () => {
  assert.equal(headline(run).text, 'Работает');
  assert.equal(headline({ ...run, state: 'STOPPED' }).tone, 'off');
  assert.equal(headline(undefined).tone, 'warn');
  assert.equal(headline({ ...run, activity: 'PAUSED' }).text, 'На паузе');
});

test('attention lists only what waits for the operator', () => {
  assert.deepEqual(attention(run, [], undefined, []), []);
  const ticket = { state: 'waiting_reply', next_turn: 'operator' } as Ticket;
  const mine = attention(run, [ticket, { ...ticket, next_turn: 'agent' } as Ticket], undefined, []);
  assert.equal(mine.length, 1);
  assert.match(mine[0].text, /: 1$/);
  assert.equal(attention({ ...run, state: 'FAULTED' }, [], undefined, [])[0].tone, 'warn');
});

test('the plan puts the active work first and keeps the last finished one apart', () => {
  const task = (id: string, state: WorkTask['state'], minutes: number) => ({ id, goal: id, state, updated_at: at(minutes) }) as WorkTask;
  const result = plan([task('a', 'open', 5), task('b', 'active', 50), task('c', 'done', 1), task('d', 'abandoned', 1)]);
  assert.deepEqual(result.open.map((row) => row.id), ['b', 'a']);
  assert.equal(result.done?.id, 'c');
});

test('budget and time lines never invent a value', () => {
  assert.equal(budgetLine(undefined), null);
  const harness = { enabled: true, ready: true, spent_today: 3.5, daily_budget: 4, currency: 'RUB', calls_today: 12 } as never;
  assert.equal(budgetLine(harness)?.tone, 'wait');
  assert.equal(budgetLine({ ...(harness as object), spent_today: 4.2 } as never)?.tone, 'warn');
  assert.equal(ago(null, 0), '—');
  assert.equal(ago(at(90), Date.UTC(2026, 10, 14, 12, 0)), '2 ч назад');
  assert.equal(gmSummary(undefined, [], undefined).latest, null);
});

test('the live picture is validated line by line and must belong to the viewed partition', async () => {
  const { harnessNow } = await import('../src/shared/validation.ts');
  const scope = { workspace_id: 'w', persona_id: 'p', memory_space_id: 'p-work', memory_mode: 'work' } as const;
  const good = { scope, enabled: true, checked_at: 'now', doing: 'идёт', lines: [{ at: 'a', kind: 'said', text: 'привет' }] };
  assert.equal(harnessNow(good, scope).lines?.length, 1);
  assert.throws(() => harnessNow({ ...good, lines: [{ at: 'a', kind: 'weird', text: 'x' }] }, scope));
  assert.throws(() => harnessNow({ ...good, scope: { ...scope, persona_id: 'other' } }, scope));
});
