import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { apiLimits, costCalculations, llmAssignments, llmAttempts, tariffs } from '../src/shared/validation.ts';
import { addMoney, compareMoney, isMoney } from '../src/ui/llm/money.ts';
import {
  backdated, connectionResolver, costPerDay, emptyDraft, gaps, groupTariffs, historyRows, noFilters, perDay, rateText, recalcExecute,
  recalcPreview, recalcProblem, retries, roleRows, select, shares, summarise, tariffCommand, usageComplete, usageLabel, validTariff,
} from '../src/ui/llm/model.ts';
import type { ConsequencePlan, CostCalculation, LLMAssignment, LLMAttempt, Scope, Tariff } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number, day = 14) => `2026-10-${day}T00:${String(minute).padStart(2, '0')}:00Z`;
const NOW = Date.parse(T(30));
const base = { schema_version: '1.0', scope, created_at: T(1), updated_at: T(1), version: 1 };
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const usage = (over: object = {}) => ({ input_tokens: 1000, output_tokens: 500, cached_input_tokens: null, reasoning_output_tokens: null, source: 'provider', completeness: 'complete', ...over });
const attempt = (name: string, over: object = {}): LLMAttempt => ({ ...base, id: 'a-' + name, operation_id: 'op-' + name, attempt_id: name, role_id: 'character', assignment_id: 'asg-1',
  assignment_version: 1, requested_model_id: 'model-x', reported_model_id: null, status: 'completed', started_at: T(2), completed_at: T(3), usage: usage(), tariff_id: 't1',
  tariff_version: 1, ...over } as unknown as LLMAttempt);
const cost = (name: string, amount: string | null, currency: string | null = 'USD', completeness = 'complete'): CostCalculation => ({ ...base, id: 'c-' + name, attempt_id: name,
  tariff_id: 't1', tariff_version: 1, amount, currency, completeness, provenance: 'provider_usage' } as unknown as CostCalculation);
const assignment = (over: object = {}): LLMAssignment => ({ ...base, id: 'asg-1', role_id: 'character', connection_id: 'conn-1', model_id: 'model-x', parameters: { schema_id: 'p', data: {} },
  required_capabilities: [], capability_verification: 'verified', fallback_assignment_id: null, ...over } as unknown as LLMAssignment);
const tariff = (over: object = {}): Tariff => ({ ...base, id: 't1', connection_id: 'conn-1', model_id: 'model-x', unit_tokens: 1000000, currency: 'USD', input_rate: '2', output_rate: '6',
  cached_input_rate: null, reasoning_output_rate: null, effective_at: T(0), ...over } as unknown as Tariff);
const plan = (over: object = {}): ConsequencePlan => ({ ...base, id: 'plan-1', operation_type: 'llm.recalculate', consequences: 'Будут пересчитаны 12 расчётов.',
  dependencies: [{ resource_id: 't1', version: 1 }, { resource_id: 'a-x', version: 1 }], expires_at: T(50), ...over } as unknown as ConsequencePlan);
const connectionOf = connectionResolver([assignment()]);

test('money is exact: no floating point drift, no mixing, strict format', () => {
  assert.equal(addMoney('0.1', '0.2'), '0.3');
  assert.equal(addMoney('0', '0'), '0');
  assert.equal(addMoney('1.50', '1.50'), '3');
  assert.equal(addMoney('0.000001', '0.000002'), '0.000003');
  assert.equal(addMoney('999999999999999999.99', '0.01'), '1000000000000000000');
  assert.equal(compareMoney('0.10', '0.1'), 0);
  assert.equal(compareMoney('2', '10'), -1);
  assert.equal(compareMoney('10.5', '10.49'), 1);
  for (const bad of ['', '-1', '1e3', '1,5', '01', '1.', '.5', 'abc', ' 1']) { assert.equal(isMoney(bad), false, bad); assert.throws(() => addMoney(bad, '1')); }
  assert.equal(isMoney('1'.repeat(65)), false);
});
test('unknown cost is never zero and currencies never mix', () => {
  const rows = [attempt('a'), attempt('b'), attempt('c'), attempt('d')];
  const out = summarise(rows, [cost('a', '0.012'), cost('b', '0.5', 'EUR'), cost('c', null, null, 'unknown')], connectionOf);
  assert.deepEqual(out.cost.map((row) => [row.currency, row.amount]), [['EUR', '0.5'], ['USD', '0.012']]);
  assert.equal(out.unknownCost, 2); // c is unknown, d has no calculation at all
  assert.equal(out.complete, false);
  assert.equal(summarise([attempt('a')], [cost('a', '0.1', 'USD', 'partial')], connectionOf).cost[0].partial, 1);
  assert.equal(summarise([attempt('a')], [cost('a', '0.1', 'USD', 'partial')], connectionOf).complete, false);
  assert.equal(summarise([attempt('a')], [cost('a', '0.1')], connectionOf).complete, true);
  // a calculation that says "complete" but carries no amount cannot make a free request
  assert.equal(summarise([attempt('a')], [cost('a', null, 'USD', 'complete')], connectionOf).unknownCost, 1);
  assert.equal(summarise([attempt('a')], [cost('a', '0.1', null, 'complete')], connectionOf).unknownCost, 1);
});
test('tokens: subsets sit beside parents, unknown and partial usage are counted apart, never as zero', () => {
  const rows = [attempt('a', { usage: usage({ input_tokens: 100, output_tokens: 50, cached_input_tokens: 40, reasoning_output_tokens: 10 }) }),
    attempt('b', { usage: usage({ input_tokens: null, output_tokens: null, source: 'unknown', completeness: 'unknown' }), status: 'unknown' }),
    attempt('c', { usage: usage({ input_tokens: 7, output_tokens: null, completeness: 'partial' }), status: 'incomplete' })];
  const { totals } = summarise(rows, [], connectionOf);
  assert.deepEqual([totals.input, totals.output, totals.cached, totals.reasoning], [107, 50, 40, 10]);
  assert.deepEqual([totals.unknownUsage, totals.partialUsage], [1, 1]);
  assert.equal(usageComplete(totals), false);
  assert.equal(usageComplete(summarise([attempt('a')], [], connectionOf).totals), true);
  assert.deepEqual([totals.byStatus.unknown, totals.byStatus.incomplete, totals.byStatus.completed], [1, 1, 1]);
  assert.equal(usageComplete(summarise([attempt('l', { usage: usage({ source: 'local_estimate' }) })], [], connectionOf).totals), true);
});
test('a retry is another paid attempt of the same operation; a repeated receipt is not', () => {
  const rows = [attempt('a1', { operation_id: 'op-1' }), attempt('a2', { operation_id: 'op-1' }), attempt('b1', { operation_id: 'op-2' })];
  const { totals, byRole } = summarise(rows, [], connectionOf);
  assert.deepEqual([totals.attempts, totals.operations, retries(totals)], [3, 2, 1]);
  assert.deepEqual([byRole.character.attempts, byRole.character.operations], [3, 2]);
});
test('groups count each operation once per group', () => {
  const rows = [attempt('a1', { operation_id: 'op-1', role_id: 'gm' }), attempt('a2', { operation_id: 'op-1', role_id: 'character' })];
  const out = summarise(rows, [], connectionOf);
  assert.deepEqual([out.byRole.gm.operations, out.byRole.character.operations, out.totals.operations], [1, 1, 1]);
});
test('filters by period, role, model (as reported) and connection of the pinned assignment', () => {
  const a = attempt('a', { started_at: T(2, 13) });
  const b = attempt('b', { started_at: T(2, 14), role_id: 'gm', reported_model_id: 'model-y' });
  const c = attempt('c', { started_at: T(2, 15), assignment_id: 'asg-2' });
  const resolve = connectionResolver([assignment(), assignment({ id: 'asg-2', connection_id: 'conn-2' })]);
  const all = [a, b, c];
  assert.equal(select(all, noFilters, resolve).length, 3);
  assert.deepEqual(select(all, { ...noFilters, since: '2026-10-14' }, resolve).map((x) => x.attempt_id), ['b', 'c']);
  assert.deepEqual(select(all, { ...noFilters, until: '2026-10-14' }, resolve).map((x) => x.attempt_id), ['a', 'b']);
  assert.deepEqual(select(all, { ...noFilters, role: 'gm' }, resolve).map((x) => x.attempt_id), ['b']);
  assert.deepEqual(select(all, { ...noFilters, model: 'model-y' }, resolve).map((x) => x.attempt_id), ['b']);
  assert.deepEqual(select(all, { ...noFilters, model: 'model-x' }, resolve).map((x) => x.attempt_id), ['a', 'c']);
  assert.deepEqual(select(all, { ...noFilters, connection: 'conn-2' }, resolve).map((x) => x.attempt_id), ['c']);
  assert.equal(connectionResolver([])(a), 'unknown');
});
test('gaps name everything that is missing so a known sum never looks like a full bill', () => {
  const rows = [attempt('a'), attempt('b', { usage: usage({ source: 'unknown', completeness: 'unknown', input_tokens: null, output_tokens: null }) }), attempt('c', { usage: usage({ completeness: 'partial' }) })];
  const out = summarise(rows, [cost('a', '1'), cost('c', '2', 'USD', 'partial')], connectionOf);
  const lines = gaps(out, false);
  assert.equal(lines.length, 5);
  assert.match(lines[0], /не вся история/);
  assert.match(lines[1], /неизвестен у 1/);
  assert.match(lines[2], /неполный у 1/);
  assert.match(lines[3], /Цена неизвестна у 1/);
  assert.match(lines[4], /USD: 1 расчётов частичные/);
  assert.deepEqual(gaps(summarise([attempt('a')], [cost('a', '1')], connectionOf), true), []);
});
test('history shows routing, latency, pinned tariff and honest unknowns', () => {
  const rows = historyRows([attempt('a', { reported_model_id: 'model-y', started_at: T(2), completed_at: T(3) }),
    attempt('b', { started_at: T(5), completed_at: null, tariff_id: null, tariff_version: null, usage: usage({ source: 'unknown', completeness: 'unknown', input_tokens: null, output_tokens: null }) })],
  [cost('a', '0.5', 'EUR', 'partial')], connectionOf);
  assert.deepEqual(rows.map((row) => row.id), ['b', 'a']); // newest first
  const [b, a] = rows;
  assert.deepEqual([a.routed, a.reported, a.latencyMs, a.cost, a.tariff], [true, 'model-y', 60000, '0.5 EUR (частично)', 't1 v1']);
  assert.deepEqual([b.routed, b.latencyMs, b.cost, b.tariff, b.tokens, b.usage], [false, null, 'неизвестно', 'тариф не закреплён', '—', 'Usage неизвестен']);
  assert.equal(historyRows([attempt('x', { reported_model_id: 'model-x' })], [], connectionOf)[0].routed, false);
  assert.equal(usageLabel(attempt('p', { usage: usage({ completeness: 'partial' }) })), 'Провайдер, неполный');
  assert.equal(usageLabel(attempt('l', { usage: usage({ source: 'local_estimate' }) })), 'Локальная оценка');
});
test('analytics: known tokens per day with unknown attempts beside them; shares and per-currency days', () => {
  const rows = [attempt('a', { started_at: T(2, 13) }), attempt('b', { started_at: T(3, 13), usage: usage({ source: 'unknown', completeness: 'unknown', input_tokens: null, output_tokens: null }) }), attempt('c', { started_at: T(2, 14), role_id: 'gm' })];
  assert.deepEqual(perDay(rows), [{ day: '2026-10-13', tokens: 1500, unknown: 1 }, { day: '2026-10-14', tokens: 1500, unknown: 0 }]);
  const out = summarise(rows, [], connectionOf);
  assert.deepEqual(shares(out.byRole).map((row) => [row.key, row.percent]), [['character', 50], ['gm', 50]]);
  assert.deepEqual(shares({}), []);
  assert.deepEqual(shares({ idle: { ...out.totals, input: 0, output: 0 } }), [{ key: 'idle', tokens: 0, percent: 0 }]);
  const days = costPerDay(rows, [cost('a', '0.1'), cost('b', '0.2'), cost('c', '0.3', 'EUR'), cost('x', null, null, 'unknown')]);
  assert.deepEqual(days, { USD: [{ day: '2026-10-13', amount: '0.3' }], EUR: [{ day: '2026-10-14', amount: '0.3' }] });
});
test('tariffs group by connection and model; the version in force is the latest already effective', () => {
  const groups = groupTariffs([tariff(), tariff({ id: 't2', version: 2, effective_at: T(10) }), tariff({ id: 't3', version: 3, effective_at: '2099-01-01T00:00:00Z' }),
    tariff({ id: 'o1', model_id: 'other', effective_at: '2099-01-01T00:00:00Z' })], NOW);
  assert.equal(groups.length, 2);
  const main = groups.find((group) => group.model === 'model-x')!;
  assert.deepEqual(main.versions.map((v) => v.id), ['t3', 't2', 't1']);
  assert.equal(main.current?.id, 't2');
  assert.equal(groups.find((group) => group.model === 'other')!.current, null);
  assert.equal(rateText(null, tariff()), 'не задана');
  assert.equal(rateText('2', tariff()), '2 USD / 1000000 ток.');
});
test('a tariff form needs connection, model, unit, currency, a rate and a date; empty is not zero', () => {
  const ok = { ...emptyDraft, connection: 'conn-1', model: 'model-x', currency: 'USD', input: '2', output: '6', effective: '2026-10-14T10:00' };
  assert.equal(validTariff(ok, NOW), null);
  assert.match(validTariff({ ...ok, connection: '' }, NOW)!, /подключение/);
  assert.match(validTariff({ ...ok, model: 'bad id' }, NOW)!, /ID модели/);
  for (const unit of ['0', '-5', '1.5', 'abc', '1000000001', '']) assert.match(validTariff({ ...ok, unit }, NOW)!, /Единица/, unit);
  for (const currency of ['usd', 'US', 'USDT', '']) assert.match(validTariff({ ...ok, currency }, NOW)!, /Валюта/, currency);
  for (const key of ['input', 'output', 'cached', 'reasoning'] as const) assert.match(validTariff({ ...ok, [key]: '-1' }, NOW)!, /Ставка/, key);
  assert.match(validTariff({ ...ok, input: '', output: '' }, NOW)!, /не задана/);
  assert.equal(validTariff({ ...ok, input: '', output: '6' }, NOW), null);
  assert.match(validTariff({ ...ok, effective: '' }, NOW)!, /дату/);
  assert.match(validTariff({ ...ok, effective: '2030-01-01T00:00' }, NOW)!, /года/);
  assert.equal(backdated({ ...ok, effective: '2026-10-12T10:00' }, NOW), true);
  assert.equal(backdated({ ...ok, effective: '2026-10-16T10:00' }, NOW), false);
  assert.equal(backdated({ ...ok, effective: '' }, NOW), false);
});
test('a tariff command sends only the rates that were set', () => {
  const draft = { ...emptyDraft, connection: 'conn-1', model: 'model-x', currency: 'USD', input: '2', output: '', cached: '0.5', effective: '2026-10-14T10:00' };
  const command = tariffCommand(runScope, 'session-1', draft, 'req-t');
  assert.equal(command.type, 'tariff.create');
  const data = command.arguments.data as Record<string, unknown>;
  assert.deepEqual(Object.keys(data).sort(), ['cached_input_rate', 'connection_id', 'currency', 'effective_at', 'input_rate', 'model_id', 'previous_version', 'unit_tokens']);
  assert.equal(data.unit_tokens, 1000000);
  assert.match(String(data.effective_at), /Z$/);
});
test('a recalculation is bound to the chosen tariff version and refused when the plan misses it or expired', () => {
  const preview = recalcPreview(runScope, 'session-1', tariff({ version: 4 }), 'req-p');
  assert.deepEqual([preview.type, preview.expected_version, preview.arguments.data], ['llm.recalculate.preview', 4, { tariff_id: 't1', tariff_version: 4 }]);
  const run = recalcExecute(runScope, 'session-1', plan({ version: 2 }), 'req-e');
  assert.deepEqual([run.type, run.expected_version, run.arguments], ['llm.recalculate.execute', 2, { plan_id: 'plan-1', plan_version: 2 }]);
  assert.equal(recalcProblem(plan(), tariff(), NOW), null);
  assert.match(recalcProblem(plan({ dependencies: [{ resource_id: 'other', version: 1 }] }), tariff(), NOW)!, /не содержит/);
  assert.match(recalcProblem(plan({ expires_at: T(10) }), tariff(), NOW)!, /истёк/);
});
test('roles: a reserve is not the role, the newest binding wins, the fallback is resolved', () => {
  const primary = assignment({ fallback_assignment_id: 'asg-r' });
  const reserve = assignment({ id: 'asg-r', model_id: 'model-r', version: 9 });
  const older = assignment({ id: 'asg-0', version: 0, model_id: 'old' });
  const rows = roleRows([older, primary, reserve], { character: { attempts: 7 } as never });
  assert.equal(rows.length, 1);
  assert.deepEqual([rows[0].assignment.id, rows[0].fallback?.id, rows[0].attempts], ['asg-1', 'asg-r', 7]);
  assert.equal(roleRows([assignment({ id: 'a', version: 1 }), assignment({ id: 'b', version: 2 })], {})[0].assignment.id, 'b');
  assert.equal(roleRows([assignment({ fallback_assignment_id: 'missing' })], {})[0].fallback, null);
  assert.deepEqual(roleRows([], {}), []);
});
test('usage projections from another partition or with impossible values are rejected', () => {
  assert.throws(() => llmAttempts(page([{ ...attempt('a'), scope: { ...scope, memory_mode: 'test' } }]), scope));
  assert.throws(() => llmAttempts(page([attempt('a', { status: 'done' })]), scope));
  assert.throws(() => llmAttempts(page([attempt('a', { usage: usage({ source: 'guess' }) })]), scope));
  assert.throws(() => llmAttempts(page([attempt('a', { usage: usage({ completeness: 'most' }) })]), scope));
  assert.throws(() => llmAttempts(page([attempt('a', { usage: usage({ input_tokens: -1 }) })]), scope));
  assert.throws(() => llmAttempts(page([attempt('a', { tariff_id: null })]), scope));
  assert.throws(() => costCalculations(page([cost('a', '-1')]), scope));
  assert.throws(() => costCalculations(page([cost('a', '1', 'usd')]), scope));
  assert.throws(() => costCalculations(page([cost('a', '1', null)]), scope));
  assert.throws(() => costCalculations(page([cost('a', '1', 'USD', 'unknown')]), scope));
  assert.throws(() => tariffs(page([tariff({ unit_tokens: 0 })]), scope));
  assert.throws(() => tariffs(page([tariff({ currency: 'dollar' })]), scope));
  assert.throws(() => tariffs(page([tariff({ input_rate: '-2' })]), scope));
  assert.throws(() => llmAssignments(page([assignment({ capability_verification: 'trusted' })]), scope));
  assert.equal(llmAttempts(page([attempt('a')]), scope).items.length, 1);
  assert.equal(costCalculations(page([cost('a', null, null, 'unknown')]), scope).items.length, 1);
  assert.equal(tariffs(page([tariff()]), scope).items.length, 1);
  assert.equal(llmAssignments(page([assignment()]), scope).items.length, 1);
});
test('usage reads use the allowlisted resources, the exact partition and a validated cursor', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.llmAttempts(scope, undefined, 'cur-1'); await api.costCalculations(scope); await api.tariffs(scope); await api.llmAssignments(scope);
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname), ['/api/v1/llm-attempts', '/api/v1/cost-calculations', '/api/v1/tariffs', '/api/v1/llm-assignments']);
  assert.equal(new URL(paths[0], 'http://x').searchParams.get('cursor'), 'cur-1');
  await assert.rejects(api.llmAttempts(scope, undefined, 'a&b'), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  assert.equal(paths.length, 4);
});
test('usage that names no source is unknown whatever its completeness says', () => {
  const nameless = usage({ source: 'unknown', completeness: 'partial', input_tokens: null, output_tokens: null });
  const { totals } = summarise([attempt('a', { usage: nameless })], [], connectionOf);
  assert.deepEqual([totals.unknownUsage, totals.partialUsage], [1, 0]);
  assert.deepEqual(perDay([attempt('a', { usage: nameless })]), [{ day: '2026-10-14', tokens: 0, unknown: 1 }]);
});
test('an unknown cost is never summed even if an amount arrives with it; the total then stays incomplete', () => {
  const out = summarise([attempt('a')], [cost('a', '5', 'USD', 'unknown')], connectionOf);
  assert.deepEqual([out.cost.length, out.unknownCost, out.complete], [0, 1, false]);
  const unknownUsage = usage({ source: 'unknown', completeness: 'unknown', input_tokens: null, output_tokens: null });
  const priced = summarise([attempt('a', { usage: unknownUsage })], [cost('a', '1')], connectionOf);
  assert.deepEqual([priced.unknownCost, priced.cost.length, priced.complete], [0, 1, false]); // priced, but tokens unknown
  assert.deepEqual(costPerDay([attempt('a')], [cost('a', '5', 'USD', 'unknown')]), {});
});
test('history says "unknown" for a calculation without an amount or without a currency', () => {
  const rows = historyRows([attempt('a'), attempt('b', { started_at: T(5) })], [cost('a', null, null, 'unknown'), cost('b', '1', null, 'partial')], connectionOf);
  assert.deepEqual(rows.map((row) => row.cost), ['неизвестно', 'неизвестно']);
});
test('days and shares come out ordered whatever order the server sent', () => {
  const rows = [attempt('c', { started_at: T(2, 15) }), attempt('a', { started_at: T(2, 13) }), attempt('b', { started_at: T(2, 14) })];
  assert.deepEqual(perDay(rows).map((day) => day.day), ['2026-10-13', '2026-10-14', '2026-10-15']);
  const costs = [cost('c', '1'), cost('a', '1'), cost('b', '1')];
  assert.deepEqual(costPerDay(rows, costs).USD.map((day) => day.day), ['2026-10-13', '2026-10-14', '2026-10-15']);
  const table = { small: { ...summarise([], [], connectionOf).totals, input: 10, output: 0 }, big: { ...summarise([], [], connectionOf).totals, input: 90, output: 0 },
    tieB: { ...summarise([], [], connectionOf).totals, input: 5, output: 0 }, tieA: { ...summarise([], [], connectionOf).totals, input: 5, output: 0 } };
  assert.deepEqual(shares(table).map((row) => row.key), ['big', 'small', 'tieA', 'tieB']);
});
test('the same model on two connections has two price lists', () => {
  const groups = groupTariffs([tariff(), tariff({ id: 'other', connection_id: 'conn-2' })], NOW);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map((group) => group.connection).sort(), ['conn-1', 'conn-2']);
  assert.equal(groups.every((group) => group.current !== null), true);
});
test('a tariff command carries each rate that was set and none that was not', () => {
  const full = { ...emptyDraft, connection: 'conn-1', model: 'model-x', currency: 'USD', input: '2', output: '6', cached: '0.5', reasoning: '8', effective: '2026-10-14T10:00' };
  const data = tariffCommand(runScope, 'session-1', full, 'r').arguments.data as Record<string, unknown>;
  assert.deepEqual([data.input_rate, data.output_rate, data.cached_input_rate, data.reasoning_output_rate], ['2', '6', '0.5', '8']);
  const sparse = tariffCommand(runScope, 'session-1', { ...full, input: '', cached: '', reasoning: '' }, 'r').arguments.data as Record<string, unknown>;
  assert.deepEqual(Object.keys(sparse).sort(), ['connection_id', 'currency', 'effective_at', 'model_id', 'output_rate', 'previous_version', 'unit_tokens']);
  const onlyReasoning = tariffCommand(runScope, 'session-1', { ...full, input: '2', output: '', cached: '', reasoning: '8' }, 'r').arguments.data as Record<string, unknown>;
  assert.deepEqual([onlyReasoning.reasoning_output_rate, 'cached_input_rate' in onlyReasoning, 'output_rate' in onlyReasoning], ['8', false, false]);
});

test('API cap rejects foreign scope and invalid reserve and carries a version', () => {
  const value = { id: 'api-limits', scope, version: 2, attempt_limit: 300, minimum_limit: 9, period: 'utc_day' };
  assert.equal(apiLimits(value, scope).version, 2);
  for (const patch of [{ version: 0 }, { attempt_limit: 8 }, { attempt_limit: true }, { period: 'local_day' }, { scope: { ...scope, persona_id: 'foreign' } }]) assert.throws(() => apiLimits({ ...value, ...patch }, scope));
  const draft = { ...emptyDraft, connection: 'conn-1', model: 'model-x', currency: 'USD', input: '0.2', effective: '2026-10-14T10:00' };
  const command = tariffCommand(scope, draft.connection, draft, 'cas', 3);
  assert.equal(command.arguments.schema_id, 'tariff.create-args');
  assert.equal((command.arguments.data as Record<string, unknown>).previous_version, 3);
});
