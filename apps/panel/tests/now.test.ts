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

test('the launch mode defaults to autonomous (as START always was) and remembers the operator', async () => {
  const { loadAutonomy, saveAutonomy, planLine } = await import('../src/ui/now/model.ts');
  const memory = new Map<string, string>();
  const storage = { getItem: (key: string) => memory.get(key) ?? null, setItem: (key: string, value: string) => void memory.set(key, value) };
  assert.equal(loadAutonomy(storage), 'autonomous');
  saveAutonomy(storage, 'observation');
  assert.equal(loadAutonomy(storage), 'observation');
  assert.equal(loadAutonomy(undefined), 'autonomous');
  const task = (goal: string) => ({ goal }) as never;
  assert.deepEqual(planLine([task('a'), task('b'), task('c'), task('d')]), { now: 'a', next: 'b', more: 2 });
  assert.deepEqual(planLine([]), { now: null, next: null, more: 0 });
});

test('traits carry the last applied movement; the funnel and the ribbon count honestly', async () => {
  const { traits, funnel, ribbon } = await import('../src/ui/now/model.ts');
  const state = { values: { schema_id: 's', schema_version: '1', data: { values: { display_name: 'Опечатка', curiosity: 0.75, caution: 0.6 } } } } as never;
  const change = (property: string, before: number, after: number, status: string, updated_at: string) =>
    ({ status, updated_at, patch: { data: { property, before, after } } }) as never;
  const rows = traits(state, [change('curiosity', 0.7, 0.75, 'APPLIED', '2026-11-10T00:00:00Z'), change('caution', 0.5, 0.6, 'REJECTED', '2026-11-11T00:00:00Z')]);
  assert.deepEqual(rows.map((row) => row.key), ['curiosity', 'caution']); // the name is text: not a trait
  assert.equal(rows[0].delta, 0.05);
  assert.equal(rows[1].delta, null); // a rejected change moved nothing
  assert.deepEqual(traits(undefined, []), []);
  const opp = (status: string) => ({ status }) as never;
  assert.deepEqual(funnel([opp('OFFERED'), opp('SCREENED'), opp('ACCEPTED'), opp('RESOLVED'), opp('BLOCKED'), opp('EXPIRED')]),
    { proposed: 2, accepted: 1, lived: 1, stopped: 2 });
  const view = ribbon([{ hour: 'a', said: 2, heard: 1 }, { hour: 'b' }] as never);
  assert.equal(view.peak, 3); assert.equal(view.busy, 1);
  assert.equal(ribbon(undefined).peak, 1);
});

test('the live picture validates activities, hours and picture names', async () => {
  const { harnessNow, harnessPicture } = await import('../src/shared/validation.ts');
  const scope = { workspace_id: 'w', persona_id: 'p', memory_space_id: 'p-work', memory_mode: 'work' } as const;
  const base = { scope, enabled: true, checked_at: 'now' };
  const ok = { ...base, activities: [{ title: 'Строю дом', state: 'doing', detail: 'д' }], hours: [{ hour: 'h', said: 1 }],
    pictures: [{ id: 'art--a.jpg', kind: 'art', at: 't', caption: 'c' }], days: [{ hour: 'd', said: 2 }],
    abilities: [{ title: 'Говорить', state: 'on', detail: 'д', tools: ['speak'] }] };
  assert.equal(harnessNow(ok, scope).activities?.length, 1);
  assert.throws(() => harnessNow({ ...ok, activities: [{ title: 'x', state: 'weird' }] }, scope));
  assert.throws(() => harnessNow({ ...ok, activities: [{ title: '', state: 'doing' }] }, scope));
  assert.throws(() => harnessNow({ ...ok, hours: [{ hour: 'h', said: -1 }] }, scope));
  assert.throws(() => harnessNow({ ...ok, pictures: [{ id: '../a.jpg', kind: 'art', at: 't' }] }, scope));
  assert.throws(() => harnessNow({ ...ok, pictures: [{ id: 'art--a.jpg', kind: 'other', at: 't' }] }, scope));
  assert.throws(() => harnessNow({ ...ok, abilities: [{ title: 'x', state: 'maybe' }] }, scope));
  assert.throws(() => harnessNow({ ...ok, days: [{ hour: 'd', said: -3 }] }, scope));
  const picture = { scope, id: 'shot--a.jpg', media_type: 'image/jpeg', data: 'AAAA' };
  assert.equal(harnessPicture(picture, scope, 'shot--a.jpg').id, 'shot--a.jpg');
  assert.throws(() => harnessPicture({ ...picture, data: 'not base64!' }, scope, 'shot--a.jpg'));
  assert.throws(() => harnessPicture(picture, scope, 'shot--b.jpg')); // the answer must be the picture asked for
  assert.throws(() => harnessPicture({ ...picture, media_type: 'text/html' }, scope, 'shot--a.jpg'));
});
