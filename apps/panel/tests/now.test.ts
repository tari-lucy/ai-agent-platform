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

test('persona text answers are validated: whitelisted ids, version keys, and the id asked for', async () => {
  const { personaTexts, personaText, personaTextRevision } = await import('../src/shared/validation.ts');
  const scope = { workspace_id: 'w', persona_id: 'p', memory_space_id: 'p-work', memory_mode: 'work' } as const;
  const list = { scope, enabled: true, checked_at: 'now', items: [{ id: 'file:GOALS.md', group: 'plans', title: 'Цели', size: 10, updated_at: 't', writable: true }] };
  assert.equal(personaTexts(list, scope).items?.length, 1);
  assert.throws(() => personaTexts({ ...list, items: [{ ...list.items[0], id: 'file:../x.md' }] }, scope));
  assert.throws(() => personaTexts({ ...list, items: [{ ...list.items[0], group: 'other' }] }, scope));
  const body = { scope, id: 'file:playbook:live.md', title: 'live', content: 'x', version: 'a'.repeat(64), updated_at: 't', writable: true, effect: 'e', history: [{ rev: 'file:playbook:live.md@20261114T120000Z', at: 't', size: 3 }] };
  assert.equal(personaText(body, scope, 'file:playbook:live.md').version.length, 64);
  assert.equal(personaText({ ...body, version: 'new' }, scope, 'file:playbook:live.md').version, 'new');
  assert.throws(() => personaText({ ...body, version: 'zzz' }, scope, 'file:playbook:live.md'));
  assert.throws(() => personaText(body, scope, 'file:GOALS.md')); // the answer must be the text asked for
  assert.throws(() => personaText({ ...body, history: [{ rev: 'bad rev!', at: 't', size: 1 }] }, scope, 'file:playbook:live.md'));
  const rev = 'guide:AGENTS.md@20261114T120000Z';
  assert.equal(personaTextRevision({ scope, rev, id: 'guide:AGENTS.md', at: 't', content: 'c' }, scope, rev).content, 'c');
  assert.throws(() => personaTextRevision({ scope, rev, id: 'file:x.py', at: 't', content: 'c' }, scope, rev));
});

test('the plan card says asleep or awake, when the window opens, and what was used', async () => {
  const { planCard, tokens } = await import('../src/ui/now/model.ts');
  const now = Date.parse('2026-11-17T22:00:00Z');
  const base = { scope: { workspace_id: 'w', persona_id: 'p', memory_space_id: 'p-work', memory_mode: 'work' } as const, enabled: true, checked_at: 'now' };
  assert.equal(planCard(undefined, now), null);
  assert.equal(planCard(base as never, now), null); // nothing known: nothing is invented
  assert.equal(tokens(950), '950');
  assert.equal(tokens(48_000), '48 тыс');
  assert.equal(tokens(3_120_000), '3,1 млн');
  const use = { window: 3_100_000, day: 5_400_000, week: 21_000_000, since: '2026-11-10T00:00:00Z', last_limit_at: '2026-11-17T19:00:00Z', last_limit_tokens: 3_400_000 };
  const awake = planCard({ ...base, plan_use: use } as never, now)!;
  assert.equal(awake.asleep, false);
  assert.equal(awake.tone, 'ok');
  assert.match(awake.lines.join(' '), /за 5 часов 3,1 млн, за сутки 5,4 млн, за неделю 21,0 млн/);
  assert.match(awake.lines.join(' '), /после 3,4 млн за 5 часов/);
  const asleep = planCard({ ...base, plan_use: use, sleep: { until: '2026-11-18T01:20:00Z', since: '2026-11-17T21:00:00Z', source: 'headers' } } as never, now)!;
  assert.equal(asleep.asleep, true);
  assert.equal(asleep.tone, 'wait');
  assert.match(asleep.headline, /Спит: лимит подписки исчерпан, ещё 3 ч 20 мин/);
  assert.match(asleep.lines[0], /проснётся сама/);
  const guess = planCard({ ...base, sleep: { until: '2026-11-17T22:30:00Z', since: '2026-11-17T22:00:00Z', source: 'default' } } as never, now)!;
  assert.match(guess.lines[0], /не назвал/); // a guess is said to be one
});

test('the home picture accepts the sleep and the plan use and refuses broken ones', async () => {
  const { harnessNow } = await import('../src/shared/validation.ts');
  const scope = { workspace_id: 'w', persona_id: 'p', memory_space_id: 'p-work', memory_mode: 'work' } as const;
  const ok = { scope, enabled: true, checked_at: 'now' };
  const sleep = { until: '2026-11-18T01:20:00Z', since: '2026-11-17T21:00:00Z', source: 'body' };
  const use = { window: 1, day: 2, week: 3, since: null, last_limit_at: null, last_limit_tokens: null };
  assert.equal(harnessNow({ ...ok, sleep, plan_use: use }, scope).enabled, true);
  assert.equal(harnessNow({ ...ok, sleep: null, plan_use: null }, scope).enabled, true);
  assert.throws(() => harnessNow({ ...ok, sleep: { ...sleep, source: 'guess' } }, scope));
  assert.throws(() => harnessNow({ ...ok, sleep: { since: 'x', source: 'body' } }, scope));
  assert.throws(() => harnessNow({ ...ok, plan_use: { ...use, window: -1 } }, scope));
  assert.throws(() => harnessNow({ ...ok, plan_use: { ...use, week: 1.5 } }, scope));
});
