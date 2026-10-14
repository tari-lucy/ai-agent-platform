import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { laboratoryReports } from '../src/shared/validation.ts';
import {
  CATALOGUE, REAL_PHRASE, compare, createCommand, eventCommand, eventProblem, isLive, labOpen, latestPerScenario, parseSummary, sortReports,
  startProblem, stopCommand, titleOf,
} from '../src/ui/laboratory/model.ts';
import { startLaboratory } from '../src/shared/owner-commands.ts';
import type { LaboratoryReport, RunSession, Scope } from '../src/shared/contracts.ts';

const work: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const test_: Scope = { ...work, persona_id: 'beta', memory_space_id: 'beta-test', memory_mode: 'test' };
const T = (minute: number) => `2026-10-14T00:${String(minute).padStart(2, '0')}:00Z`;
const base = { schema_version: '1.0', scope: test_, created_at: T(1), updated_at: T(1), version: 1 };
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const summary = (verdict = 'passed', checks = 9, failed = 0, seed = 'lab-seed', digest = 'abc123def456') => `MOCK ${verdict}: ${checks} checks, ${failed} failed; seed ${seed}; digest ${digest}`;
const report = (name: string, over: object = {}): LaboratoryReport => ({ ...base, id: name, scenario_id: 'api-cutoff', scenario_version: 1, operation_id: 'lab-' + name,
  status: 'passed', report_artifact: null, summary: summary(), ...over } as unknown as LaboratoryReport);
const control = { scope: { ...work, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' }, version: 17 } as RunSession;
const selected = (scenario_id = 'api-cutoff', version = 2) => ({ scenario_id, version, title: 'Synthetic', tags: [] });
const start = (over: object = {}) => ({ scenario: 'api-cutoff', seed: 'lab-seed', real: false, typed: '', acknowledged: false, ...over });

test('the catalogue has the twelve reference scenarios with readable titles and unknown ids stay as they are', () => {
  assert.equal(CATALOGUE.length, 12);
  assert.equal(new Set(CATALOGUE.map((entry) => entry.id)).size, 12);
  assert.equal(titleOf('api-cutoff'), 'Обрыв API');
  assert.equal(titleOf('first-meeting-new-session'), 'Знакомство → новая сессия');
  assert.equal(titleOf('unknown-scenario'), 'unknown-scenario');
});
test('the runner summary is parsed exactly and anything else is not guessed', () => {
  assert.deepEqual(parseSummary(summary('failed', 12, 2, 'seed-7', 'deadbeef0001')), { kind: 'mock', verdict: 'failed', checks: 12, failed: 2, seed: 'seed-7', digest: 'deadbeef0001' });
  for (const text of ['', 'passed', 'REAL passed: 1 checks, 0 failed; seed s; digest abcdef', 'MOCK passed: x checks, 0 failed; seed s; digest abcdef',
    'MOCK passed: 1 checks, 0 failed; seed s; digest XYZ', 'MOCK passed: 1 checks, 0 failed; seed ; digest abcdef0', 'prefix MOCK passed: 1 checks, 0 failed; seed s; digest abcdef0']) {
    assert.equal(parseSummary(text), null, text);
  }
  assert.equal(parseSummary(summary('passed', 1, 0, 'a b; c', 'abcdef0'))?.seed, 'a b; c');
});
test('live runs, newest report first and the latest report per scenario', () => {
  assert.equal(isLive(report('a', { status: 'pending' })), true);
  assert.equal(isLive(report('a', { status: 'running' })), true);
  for (const status of ['passed', 'failed', 'cancelled', 'unknown']) assert.equal(isLive(report('a', { status })), false, status);
  const rows = [report('old', { updated_at: T(2) }), report('new', { updated_at: T(9) }), report('other', { scenario_id: 'same-nickname', updated_at: T(5) })];
  assert.deepEqual(sortReports(rows).map((r) => r.id), ['new', 'other', 'old']);
  const latest = latestPerScenario(rows);
  assert.deepEqual([latest.get('api-cutoff')!.id, latest.get('same-nickname')!.id, latest.size], ['new', 'other', 2]);
});
test('a replay with the same scenario and seed must give the same digest', () => {
  const a = report('a'); const same = report('b'); const other = report('c', { summary: summary('passed', 9, 0, 'lab-seed', 'ffffffffffff') });
  assert.equal(compare(a, same).verdict, 'same');
  assert.equal(compare(a, other).verdict, 'differs');
  assert.equal(compare(a, report('d', { summary: summary('passed', 9, 0, 'seed-2') })).verdict, 'not-comparable');
  assert.match(compare(a, report('d', { summary: summary('passed', 9, 0, 'seed-2') })).text, /не повтор/);
  assert.equal(compare(a, report('e', { scenario_id: 'same-nickname' })).verdict, 'not-comparable');
  assert.equal(compare(a, report('f', { scenario_version: 2 })).verdict, 'not-comparable');
  assert.equal(compare(a, report('g', { summary: 'garbled' })).verdict, 'not-comparable');
  assert.match(compare(a, report('g', { summary: 'garbled' })).text, /не распознана/);
});
test('the laboratory opens only in test memory', () => {
  assert.equal(labOpen(test_), true);
  assert.equal(labOpen(work), false);
  assert.match(startProblem(work, start(), false)!, /только в тестовой памяти/);
  assert.match(eventProblem(work, 'привет')!, /тестовую память/);
});
test('a start needs a catalogue scenario, a valid seed and no run in progress', () => {
  assert.equal(startProblem(test_, start(), false), null);
  assert.match(startProblem(test_, start({ scenario: '' }), false)!, /каталога/);
  assert.match(startProblem(test_, start({ scenario: 'not-in-catalogue' }), false)!, /каталога/);
  for (const seed of ['', ' ', 'a b', '-x', 'я', 'x'.repeat(65)]) assert.match(startProblem(test_, start({ seed }), false)!, /Seed/, seed);
  assert.equal(startProblem(test_, start({ seed: 'x'.repeat(64) }), false), null);
  assert.match(startProblem(test_, start(), true)!, /ещё идёт/);
});
test('a real API run needs both an acknowledgement and the typed phrase', () => {
  assert.equal(startProblem(test_, start({ real: true, acknowledged: true, typed: REAL_PHRASE }), false), null);
  assert.match(startProblem(test_, start({ real: true }), false)!, /платный/);
  assert.match(startProblem(test_, start({ real: true, acknowledged: true }), false)!, /платный/);
  assert.match(startProblem(test_, start({ real: true, typed: REAL_PHRASE }), false)!, /платный/);
  assert.match(startProblem(test_, start({ real: true, acknowledged: true, typed: 'реальный api' }), false)!, /платный/);
  assert.equal(startProblem(test_, start({ real: false, typed: 'whatever' }), false), null); // mock never asks
});
test('canonical start pins run CAS, backend scenario version and a separate TEST scope', () => {
  const run = startLaboratory(control, selected(), test_, 'lab-seed', 'req-1');
  assert.deepEqual([run.type, run.target, run.expected_version, run.scope], ['laboratory.start', 'laboratory', 17, control.scope]);
  assert.deepEqual(run.arguments.data, { scenario_id: 'api-cutoff', scenario_version: 2, lab_scope: test_, seed: 'lab-seed', mode: 'mock' });
  const halt = stopCommand(test_, report('y', { version: 7, operation_id: 'lab-y' }), 'req-2');
  assert.deepEqual([halt.type, halt.expected_version, halt.arguments.data], ['laboratory.stop', 7, { operation_id: 'lab-y' }]);
  assert.deepEqual(eventCommand(test_, 'вымышленное событие', 'req-3').arguments.data, { text: 'вымышленное событие' });
});

test('creating the test area names only the persona and is addressed from the work scope', () => {
  const create = createCommand(work, 'req-4');
  assert.deepEqual([create.type, create.scope.memory_mode, create.arguments.data], ['laboratory.create', 'work', { persona_id: 'alpha' }]);
});
test('an event must be non-empty and bounded', () => {
  assert.equal(eventProblem(test_, 'привет'), null);
  assert.match(eventProblem(test_, '   ')!, /Введите/);
  assert.match(eventProblem(test_, 'я'.repeat(2001))!, /2000/);
  assert.equal(eventProblem(test_, 'я'.repeat(2000)), null);
});
test('laboratory reports of work memory or with impossible values are rejected', () => {
  assert.throws(() => laboratoryReports(page([{ ...report('a'), scope: work }]), work)); // a report lives only in test memory
  assert.throws(() => laboratoryReports(page([{ ...report('a'), scope: { ...test_, persona_id: 'other' } }]), test_));
  assert.throws(() => laboratoryReports(page([report('a', { status: 'green' })]), test_));
  assert.throws(() => laboratoryReports(page([report('a', { scenario_version: 0 })]), test_));
  assert.throws(() => laboratoryReports(page([report('a', { summary: 'x'.repeat(501) })]), test_));
  assert.throws(() => laboratoryReports(page([report('a', { operation_id: 'bad id' })]), test_));
  assert.throws(() => laboratoryReports(page([report('a', { report_artifact: { artifact_id: 'bad id' } })]), test_));
  assert.equal(laboratoryReports(page([report('a')]), test_).items.length, 1);
  assert.equal(laboratoryReports(page([report('a', { report_artifact: { artifact_id: 'art-1' } })]), test_).items.length, 1);
});
test('laboratory reads use the allowlisted resource, the test partition and a validated cursor', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.laboratoryReports(test_, undefined, 'cur-1');
  assert.equal(new URL(paths[0], 'http://x').pathname, '/api/v1/laboratory-reports');
  assert.equal(new URL(paths[0], 'http://x').searchParams.get('memory_mode'), 'test');
  assert.equal(new URL(paths[0], 'http://x').searchParams.get('cursor'), 'cur-1');
  await assert.rejects(api.laboratoryReports(test_, undefined, 'a&b'), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  assert.equal(paths.length, 1);
});
test('the summary must match to its very end and the digest must be hexadecimal', () => {
  assert.equal(parseSummary(summary() + ' lorem'), null);
  assert.equal(parseSummary(summary() + '\nsecond line'), null);
  assert.equal(parseSummary('MOCK passed: 1 checks, 0 failed; seed s; digest ghijkl'), null);
  assert.equal(parseSummary('MOCK passed: 1 checks, 0 failed; seed s; digest abcdef12345g'), null);
  assert.equal(parseSummary('MOCK passed: 1 checks, 0 failed; seed s; digest abc'), null); // too short to be a digest
  assert.notEqual(parseSummary('MOCK passed: 1 checks, 0 failed; seed s; digest abcdef'), null);
});
test('a start command carries the chosen scenario and seed, whatever they are', () => {
  const data = startLaboratory(control, selected('same-nickname'), test_, 'seed-42', 'r').arguments.data as Record<string, unknown>;
  assert.deepEqual([data.scenario_id, data.seed], ['same-nickname', 'seed-42']);
  const other = startLaboratory(control, selected('deletion-is-final'), test_, 'x.y:z', 'r').arguments.data as Record<string, unknown>;
  assert.deepEqual([other.scenario_id, other.seed], ['deletion-is-final', 'x.y:z']);
});
