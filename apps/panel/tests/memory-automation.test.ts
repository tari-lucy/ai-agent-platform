import test from 'node:test';
import assert from 'node:assert/strict';
import type { Episode, MemoryCycle, PrivateApiStatus, RunSession, Scope, StateChange } from '../src/shared/contracts.ts';
import { automationCondition, backlog, backlogNote, continuation, continuationNote, cycleOutcomes, cycleVerdict, newestCycles } from '../src/ui/memory/automation.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'work', memory_mode: 'work' };
const run = { scope, state: 'ACTIVE', activity: 'OPEN', autonomy: 'autonomous' } as RunSession;
const budget = { enabled: true, memory_processing: true, attempts_remaining: 30, memory_calls_used: 0, memory_daily_calls: 8 } as PrivateApiStatus;
const condition = (r: RunSession | undefined = run, b: PrivateApiStatus | undefined = budget, error = false) => automationCondition(scope, r, b, error).code;

test('memory availability distinguishes a known eligible run from disabled, missing and stale status', () => {
  assert.equal(condition(), 'eligible');
  assert.equal(automationCondition(scope, undefined, budget, false).code, 'no-run');
  assert.equal(condition(run, budget, true), 'unavailable');
  assert.equal(automationCondition(scope, run, undefined, false).code, 'unavailable');
  assert.equal(condition(run, { ...budget, memory_processing: false }), 'disabled');
  assert.equal(condition({ ...run, scope: { ...scope, persona_id: 'other' } }), 'no-run');
  assert.equal(automationCondition({ ...scope, memory_mode: 'test' }, run, budget, false).code, 'test');
});
test('STOP, observation, closed activity and both budget ceilings explain why no automatic work can start', () => {
  assert.equal(condition({ ...run, state: 'STOPPED' }), 'stopped');
  assert.equal(condition({ ...run, autonomy: 'observation' }), 'restricted');
  assert.equal(condition({ ...run, activity: 'PAUSED' } as RunSession), 'restricted');
  assert.equal(condition(run, { ...budget, attempts_remaining: 0 }), 'budget');
  assert.equal(condition(run, { ...budget, memory_calls_used: 8 }), 'budget');
  assert.equal(condition(run, { ...budget, memory_calls_used: undefined }), 'unavailable');
});
test('latest received cycle is chosen by time rather than opaque ID and without mutating the cache', () => {
  const rows = [{ id: 'z', updated_at: '2026-10-14T00:00:00Z' }, { id: 'a', updated_at: '2026-10-15T00:00:00Z' }] as MemoryCycle[];
  assert.equal(newestCycles(rows)[0].id, 'a');
  assert.equal(rows[0].id, 'z');
});
test('outcome counts belong to the exact cycle; unloaded records stay unknown rather than being declared missing', () => {
  const cycle = { record_ids: ['claim-a', 'summary-a', 'change-a', 'missing', 'claim-a'] } as MemoryCycle;
  assert.deepEqual(cycleOutcomes(cycle, { claims: [{ id: 'claim-a' }, { id: 'claim-a' }, { id: 'other' }], summaries: [{ id: 'summary-a' }], changes: [{ id: 'change-a' }] }),
    { claims: 1, summaries: 1, changes: 1, unclassified: 1, total: 4 });
  assert.equal(cycleOutcomes(cycle, { claims: [], summaries: [], changes: [] }).unclassified, 4);
});

const at = (day: number, hour = 12) => `2026-10-${String(day).padStart(2, '0')}T${String(hour).padStart(2, '0')}:00:00Z`;
const cyc = (id: string, status: string, episodes: string[], created: string, over: object = {}): MemoryCycle =>
  ({ id, scope, created_at: created, updated_at: created, version: 1, episode_ids: episodes, source_ids: ['s'], input_digest: 'a'.repeat(64),
    stage: status === 'completed' ? 'done' : status === 'queued' ? 'queued' : 'consolidate', status, record_ids: [], error_code: status === 'failed' || status === 'unknown' ? 'CAPABILITY_UNAVAILABLE' : null, ...over } as unknown as MemoryCycle);
const ep = (id: string, created: string): Episode => ({ id, scope, created_at: created } as unknown as Episode);

test('the backlog keeps nothing-to-do, waiting, today, continuing and unknown apart', () => {
  const now = Date.parse(at(19, 10));
  assert.equal(backlogNote(backlog([], [], now, true)), 'Необработанного опыта нет: обрабатывать нечего, это не ошибка.');
  const rows = [cyc('done', 'completed', ['e1'], at(17)), cyc('broken', 'failed', ['e2'], at(18)), cyc('lost', 'unknown', ['e3'], at(18))];
  const episodes = [ep('e1', at(16)), ep('e2', at(17)), ep('e3', at(17)), ep('e4', at(18)), ep('e5', at(18)), ep('e6', at(19, 9))];
  const view = backlog(episodes, rows, now, false);
  assert.deepEqual(view, { waiting: 2, today: 1, continuing: 1, unknown: 1, complete: false });
  const note = backlogNote(view);
  assert.match(note, /не менее 2 \(за раз — до 10\)/);
  assert.match(note, /Порций, ждущих продолжения после сбоя: 1/);
  assert.match(note, /неизвестным исходом: 1 — они не повторяются/);
});

test('only a failed cycle continues, once, from its saved stage; unknown never does', () => {
  const failed = cyc('a', 'failed', ['e1', 'e2'], at(18));
  assert.equal(continuation(failed, [failed]), 'will-continue');
  assert.match(continuationNote(failed, [failed]) ?? '', /с этапа сводки/);
  const resumed = cyc('b', 'running', ['e2', 'e1'], at(18, 13));
  assert.equal(continuation(failed, [failed, resumed]), 'continued');
  const third = [failed, cyc('b', 'failed', ['e1', 'e2'], at(18, 13)), cyc('c', 'failed', ['e1', 'e2'], at(18, 14))];
  assert.equal(continuation(third[2], third), 'limit');
  assert.equal(continuation(cyc('u', 'unknown', ['e1'], at(18)), []), 'unknown');
  assert.equal(continuationNote(cyc('ok', 'completed', ['e1'], at(18)), []), null);
});

test('a completed cycle says whether a change was applied, rejected or the character kept', () => {
  const change = (id: string, status: string) => ({ id, status } as unknown as StateChange);
  const done = cyc('d', 'completed', ['e1'], at(18), { record_ids: ['ch-1'] });
  assert.equal(cycleVerdict(done, [change('ch-1', 'APPLIED')]), 'applied');
  assert.equal(cycleVerdict(done, [change('ch-1', 'REJECTED')]), 'rejected');
  assert.equal(cycleVerdict(done, []), 'kept');
  assert.equal(cycleVerdict(cyc('f', 'failed', ['e1'], at(18)), []), null);
});
