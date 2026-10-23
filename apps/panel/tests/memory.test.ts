import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { artifact, claims, episodes, memorySources, operation, plan as parsePlan, summaries } from '../src/shared/validation.ts';
import {
  afterExecute, basisAvailability, basisSummary, buildCards, claimCard, conflictsOf, correctCommand, dependencyCount, episodeCard,
  executeCommand, filterCards, noFilters, planProblem, previewCommand, progress, retentionText, rightsText, sourceCard, summaryCard,
  validCorrection, visibleText,
} from '../src/ui/memory/model.ts';
import type {
  Claim, ConsequencePlan, Episode, OperationProjection, OperatorSession, Receipt, Scope, Source, SourceRef, Summary,
} from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const auth: OperatorSession = { principal_id: 'operator', scopes: [scope], csrf_token: 'c'.repeat(43), expires_at: '2099-01-01T00:00:00Z' };
const T = (minute: number) => `2026-10-14T00:${String(minute).padStart(2, '0')}:00Z`;
const NOW = Date.parse(T(30));
const base = { schema_version: '1.0', scope, created_at: T(1), updated_at: T(1), version: 1 };
const page = (items: unknown[], extra: object = {}) => ({ items, limit: 50, next_cursor: null, complete: true, ...extra });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const ref = (id: string, availability: SourceRef['availability'] = 'available'): SourceRef => ({ source_id: id, version: 1, scope, availability });
const source = (over: object = {}): Source => ({ ...base, id: 's1', source_id: 's1', provenance: 'third_party_message', source_type: 'chat', adapter_id: 'fixture',
  adapter_version: '1', occurred_at: T(2), registered_at: T(2), author_id: 'ivan', content: 'Привет, меня зовут Иван', availability: 'available',
  retention: { policy_id: 'conversation-30d', expires_at: '2026-11-13T00:00:00Z', protected: false }, read_allowed: true,
  api_transfer_allowed: true, public_disclosure_allowed: false, ...over } as unknown as Source);
const claim = (over: object = {}): Claim => ({ ...base, id: 'c1', subject_id: 'ivan', content: 'Иван живёт у озера', knowledge_type: 'reported',
  sources: [ref('s1')], confidence: 0.6, conflict_ids: [], updated_at: T(5), ...over } as unknown as Claim);
const episode = (over: object = {}): Episode => ({ schema_version: '1.0', scope, created_at: T(1), id: 'e1', choice: 'Рассмотреть камень', actual_outcome: 'Камень обычный', operation_id: 'op-1',
  outcome_confirmed: true, participant_ids: ['ivan'], provenance: 'observation', sources: [ref('s1')], ...over } as unknown as Episode);
const summary = (over: object = {}): Summary => ({ ...base, id: 'm1', analysis_id: 'an-1', content: 'За день: обсуждали озеро', episode_ids: ['e1'],
  period_start: T(0), period_end: T(9), sources: [ref('s1')], ...over } as unknown as Summary);
const plan = (over: object = {}): ConsequencePlan => ({ ...base, id: 'plan-1', operation_type: 'memory.delete', consequences: 'Будут удалены текст, выдержки и индексы.',
  dependencies: [{ resource_id: 's1', version: 1 }, { resource_id: 'c1', version: 1 }], expires_at: T(50), ...over } as unknown as ConsequencePlan);

test('immutable episodes need no version; mutable memory records still require one', () => {
  assert.equal(episodes(page([episode()]), scope).items.length, 1);
  assert.throws(() => episodes(page([episode({ version: 0 })]), scope));
  assert.throws(() => episodes(page([episode({ scope: { ...scope, memory_mode: 'test' } })]), scope));
  assert.throws(() => memorySources(page([source({ version: undefined })]), scope));
  assert.throws(() => claims(page([claim({ version: undefined })]), scope));
  assert.throws(() => summaries(page([summary({ version: undefined })]), scope));
});

test('memory pages from another partition or with impossible states are rejected before the cache', () => {
  assert.throws(() => memorySources(page([{ ...source(), scope: { ...scope, memory_mode: 'test' } }]), scope));
  assert.throws(() => memorySources(page([source({ availability: 'secret' })]), scope));
  // removed sources must not carry text or artifacts
  assert.throws(() => memorySources(page([source({ availability: 'deleted', content: 'остаток' })]), scope));
  assert.throws(() => memorySources(page([source({ availability: 'expired', content: 'остаток' })]), scope));
  assert.throws(() => memorySources(page([source({ read_allowed: 'yes' })]), scope));
  assert.throws(() => claims(page([claim({ confidence: 1.5 })]), scope));
  assert.throws(() => claims(page([claim({ knowledge_type: 'rumour' })]), scope));
  assert.throws(() => claims(page([claim({ sources: [{ ...ref('s1'), availability: 'lost' }] })]), scope));
  assert.throws(() => episodes(page([episode({ provenance: 'dream' })]), scope));
  assert.throws(() => summaries(page([summary({ period_end: 'never' })]), scope));
  assert.equal(memorySources(page([source()]), scope).items.length, 1);
  assert.equal(claims(page([claim()]), scope).items.length, 1);
});
test('plan, operation and artifact reads check identity and partition', () => {
  assert.equal(parsePlan(plan(), scope, 'plan-1').id, 'plan-1');
  assert.throws(() => parsePlan(plan(), scope, 'plan-2'));
  assert.throws(() => parsePlan({ ...plan(), scope: { ...scope, memory_mode: 'test' } }, scope, 'plan-1'));
  assert.throws(() => parsePlan(plan({ dependencies: [{ resource_id: 'bad id', version: 1 }] }), scope, 'plan-1'));
  const op = { ...base, id: 'o1', operation_id: 'o1', status: 'RUNNING', deadline: T(40) };
  assert.equal(operation(op, scope, 'o1').operation_id, 'o1');
  assert.throws(() => operation(op, scope, 'o2'));
  assert.throws(() => operation({ ...op, status: 'DONE' }, scope, 'o1'));
  const art = { ...base, id: 'a1', reference: { artifact_id: 'a1' }, protected: false, expires_at: null };
  assert.equal(artifact(art, scope, 'a1').id, 'a1');
  assert.throws(() => artifact({ ...art, reference: { artifact_id: 'other' } }, scope, 'a1'));
});
test('cards label the kind of knowledge and never show text of an unavailable record', () => {
  assert.equal(sourceCard(source()).category, 'message');
  assert.equal(sourceCard(source({ provenance: 'observation' })).category, 'observation');
  assert.equal(sourceCard(source({ provenance: 'model_proposal' })).category, 'hypothesis');
  assert.equal(sourceCard(source({ provenance: 'interpretation' })).category, 'interpretation');
  assert.equal(claimCard(claim({ knowledge_type: 'observed' })).category, 'observation');
  assert.equal(claimCard(claim({ knowledge_type: 'reported' })).category, 'message');
  assert.equal(claimCard(claim({ knowledge_type: 'hypothesis' })).category, 'hypothesis');
  assert.equal(visibleText(sourceCard(source())), 'Привет, меня зовут Иван');
  // read rights close the text even for an available source
  assert.equal(visibleText(sourceCard(source({ read_allowed: false }))), null);
  assert.equal(visibleText(sourceCard(source({ availability: 'expired', content: null }))), null);
  // a stale copy of text on a deleted/unavailable derived record is not shown either
  const deleted = claimCard(claim({ sources: [ref('s1', 'deleted')] }));
  assert.equal(deleted.availability, 'deleted');
  assert.equal(visibleText(deleted), null);
  assert.equal(visibleText(claimCard(claim({ sources: [ref('s1', 'unavailable')] }))), null);
  assert.equal(visibleText(claimCard(claim({ sources: [ref('s1', 'expired')] }))), 'Иван живёт у озера');
});
test('a derived record is as available as its basis and says what is missing', () => {
  assert.equal(basisAvailability([ref('a', 'deleted'), ref('b')]), 'available');
  assert.equal(basisAvailability([ref('a', 'deleted'), ref('b', 'expired')]), 'expired');
  assert.equal(basisAvailability([ref('a', 'deleted')]), 'deleted');
  assert.equal(basisAvailability([ref('a', 'unavailable')]), 'unavailable');
  assert.equal(basisAvailability([]), 'unavailable');
  assert.equal(basisSummary([]), 'Основание не указано');
  assert.match(basisSummary([ref('a'), ref('b', 'deleted')]), /1 · доступно; 1 · удалено/);
});
test('cards are ordered newest first and filters combine', () => {
  const cards = buildCards({ sources: [source()], episodes: [episode()], claims: [claim(), claim({ id: 'c2', content: 'Он любит чай', updated_at: T(8), sources: [ref('s1', 'deleted')], subject_id: 'petr' })], summaries: [summary()] });
  assert.deepEqual(cards.map((card) => card.key), ['summary:m1', 'claim:c2', 'claim:c1', 'source:s1', 'episode:e1']);
  assert.equal(filterCards(cards, { ...noFilters, kind: 'claim' }).length, 2);
  assert.equal(filterCards(cards, { ...noFilters, availability: 'deleted' }).length, 1);
  assert.equal(filterCards(cards, { ...noFilters, category: 'message' }).length, 3);
  assert.equal(filterCards(cards, { ...noFilters, person: 'PETR' }).length, 1);
  assert.equal(filterCards(cards, { ...noFilters, query: 'озера' }).map((card) => card.id).includes('c1'), true);
  // text of an unavailable record is not searchable
  assert.equal(filterCards(cards, { ...noFilters, query: 'чай' }).length, 0);
  assert.equal(filterCards(cards, { ...noFilters, from: '2026-10-15' }).length, 0);
  assert.equal(filterCards(cards, { ...noFilters, to: '2026-10-13' }).length, 0);
  assert.equal(filterCards(cards, { ...noFilters, from: '2026-10-14', to: '2026-10-14' }).length, cards.length);
});
test('retention text names protection, expiry and expired state; rights are explicit', () => {
  assert.match(retentionText(source({ retention: { policy_id: 'p', expires_at: null, protected: true } }), NOW), /Защищено/);
  assert.equal(retentionText(source({ retention: { policy_id: 'p', expires_at: null, protected: false } }), NOW), 'Без срока.');
  assert.match(retentionText(source({ retention: { policy_id: 'p', expires_at: T(40), protected: false } }), NOW), /Хранится до/);
  assert.match(retentionText(source({ retention: { policy_id: 'p', expires_at: T(10), protected: false } }), NOW), /истёк/);
  assert.match(retentionText(source({ availability: 'expired', content: null }), NOW), /истёк/);
  assert.match(retentionText(null, NOW), /основания/);
});
test('conflicting claims link to each other', () => {
  const cards = buildCards({ sources: [], episodes: [], summaries: [], claims: [claim({ conflict_ids: ['c2'] }), claim({ id: 'c2', content: 'Живёт у реки', conflict_ids: ['c1'] })] });
  const first = cards.find((card) => card.id === 'c1')!;
  assert.deepEqual(conflictsOf(first, cards).map((card) => card.id), ['c2']);
  assert.deepEqual(conflictsOf({ ...first, conflicts: ['missing'] }, cards), []);
});
test('corrections need a reason, only lower certainty and never touch other kinds', () => {
  const card = claimCard(claim());
  const ok = { content: 'Иван живёт у реки', confidence: '', reason: 'Он сам уточнил' };
  assert.equal(validCorrection(card, ok), null);
  assert.match(validCorrection(card, { ...ok, reason: ' ' })!, /основание/);
  assert.match(validCorrection(card, { ...ok, content: card.text!, confidence: '' })!, /Измените/);
  assert.equal(validCorrection(card, { content: card.text!, confidence: '0.3', reason: 'сомнение' }), null);
  assert.match(validCorrection(card, { content: card.text!, confidence: '0.6', reason: 'x' })!, /только снизить/);
  assert.match(validCorrection(card, { content: card.text!, confidence: '0.9', reason: 'x' })!, /только снизить/);
  assert.match(validCorrection(card, { content: card.text!, confidence: '-1', reason: 'x' })!, /от 0 до 1/);
  assert.match(validCorrection(card, { content: card.text!, confidence: 'abc', reason: 'x' })!, /от 0 до 1/);
  assert.match(validCorrection(card, { ...ok, reason: 'я'.repeat(1001) })!, /1000/);
  assert.match(validCorrection(card, { ...ok, content: 'я'.repeat(4001) })!, /4000/);
  assert.match(validCorrection(sourceCard(source()), ok)!, /утверждения и сводки/);
  assert.match(validCorrection(episodeCard(episode()), ok)!, /утверждения и сводки/);
  assert.match(validCorrection(summaryCard(summary()), { content: 'x', confidence: '0.1', reason: 'r' })!, /только у утверждений/);
  assert.equal(validCorrection(summaryCard(summary()), { content: 'Уточнённая сводка', confidence: '', reason: 'r' }), null);
});
test('correction carries the version the operator saw', () => {
  const card = claimCard(claim({ version: 4 }));
  const cmd = correctCommand(runScope, card, { content: 'Иван живёт у реки', confidence: '0.3', reason: ' причина ' }, 'req-1');
  assert.equal(cmd.type, 'memory.correct');
  assert.equal(cmd.expected_version, 4);
  assert.equal(cmd.target, card.id);
  assert.equal(cmd.arguments.schema_id, 'memory.correct-args');
  assert.deepEqual(cmd.arguments.data, { kind: 'claim', record_id: 'c1', reason: 'причина', content: 'Иван живёт у реки', confidence: 0.3 });
  const only = correctCommand(runScope, card, { content: card.text!, confidence: '', reason: 'r' }, 'req-2');
  assert.deepEqual(only.arguments.data, { kind: 'claim', record_id: 'c1', reason: 'r' });
});
test('plans name the chosen record, expire, and execution is bound to the plan version', () => {
  const card = sourceCard(source());
  assert.equal(planProblem(plan(), card, NOW), null);
  assert.match(planProblem(plan({ dependencies: [{ resource_id: 'other', version: 1 }] }), card, NOW)!, /не содержит/);
  assert.match(planProblem(plan({ expires_at: T(10) }), card, NOW)!, /истёк/);
  assert.equal(dependencyCount(plan(), card), 1);
  const preview = previewCommand(runScope, 'session-1', card, 'delete', 'req-p');
  assert.equal(preview.type, 'memory.delete.preview');
  assert.deepEqual(preview.arguments.data, { source_ids: ['s1'] });
  assert.equal(previewCommand(runScope, 'session-1', card, 'export', 'req-q').type, 'memory.export.preview');
  const run = executeCommand(runScope, 'session-1', plan({ version: 3 }), 'delete', 'req-e');
  assert.equal(run.type, 'memory.delete.execute');
  assert.deepEqual(run.arguments, { plan_id: 'plan-1', plan_version: 3 });
  assert.equal(run.expected_version, 3);
  assert.equal(executeCommand(runScope, 'session-1', plan(), 'export', 'req-f').type, 'memory.export.execute');
});
test('an unknown execution keeps the identical command; a refusal does not', () => {
  const command = executeCommand(runScope, 'session-1', plan(), 'delete', 'req-e');
  for (const code of ['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT']) assert.deepEqual(afterExecute(plan(), command, { code }), { phase: 'unknown', plan: plan(), command });
  assert.deepEqual(afterExecute(plan(), command, { code: 'VERSION_CONFLICT' }), { phase: 'refused', code: 'VERSION_CONFLICT' });
  const receipt = { request_id: 'req-e', scope: runScope, status: 'waiting', operation_id: 'op-9' } as Receipt;
  assert.equal(afterExecute(plan(), command, { receipt }).phase, 'running');
  assert.equal(afterExecute(plan(), command, { receipt: { ...receipt, status: 'rejected' } }).phase, 'refused');
});
test('progress shows done and pending parts as the owner reports them; unknown is never done', () => {
  const op = (status: string, extra: object = {}) => ({ ...base, id: 'o', operation_id: 'o', status, deadline: T(40), ...extra }) as unknown as OperationProjection;
  assert.deepEqual(progress(null, 'Удаление').pending, ['Удаление']);
  assert.deepEqual(progress(op('SUCCEEDED'), 'Удаление').done, ['Удаление']);
  assert.deepEqual(progress(op('RUNNING', { waiting_reason: 'чистим индекс' }), 'Удаление'), { done: [], pending: ['Удаление'], text: 'чистим индекс' });
  assert.deepEqual(progress(op('OUTCOME_UNKNOWN'), 'Удаление').done, []);
  assert.match(progress(op('OUTCOME_UNKNOWN'), 'Удаление').text, /сверка/);
  assert.deepEqual(progress(op('FAILED'), 'Удаление').done, []);
  assert.match(progress(op('EXPIRED'), 'Удаление').text, /Не выполнено/);
});
test('memory reads use allowlisted resources, exact partition and a validated cursor', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.sources(scope); await api.episodes(scope, undefined, 'cur-1'); await api.claims(scope); await api.summaries(scope);
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname), ['/api/v1/sources', '/api/v1/episodes', '/api/v1/claims', '/api/v1/summaries']);
  assert.equal(new URL(paths[1], 'http://x').searchParams.get('cursor'), 'cur-1');
  assert.equal(new URL(paths[0], 'http://x').searchParams.get('memory_mode'), 'work');
  await assert.rejects(api.sources(scope, undefined, '../../etc'), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  assert.equal(paths.length, 4);
});
test('a plan and an operation are read by id inside the same partition', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(path.includes('/plans/') ? plan() : { ...base, id: 'o1', operation_id: 'o1', status: 'RUNNING', deadline: T(40) }); }));
  assert.equal((await api.plan(scope, 'plan-1')).id, 'plan-1');
  assert.equal((await api.operation(scope, 'o1')).operation_id, 'o1');
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname), ['/api/v1/plans/plan-1', '/api/v1/operations/o1']);
  await assert.rejects(api.plan(scope, 'bad/../id'));
  const wrong = new PanelApi(fetcher(async () => json(plan({ id: 'plan-2' }))));
  await assert.rejects(wrong.plan(scope, 'plan-1'));
});
test('execute and preview post once with CSRF; a lost reply is checked by the same request id', async () => {
  const bodies: { request_id: string }[] = [];
  let lose = true;
  const api = new PanelApi(fetcher(async (_path, init) => {
    const body = JSON.parse(String(init.body)); bodies.push(body);
    if (lose) { lose = false; throw new Error('reset'); }
    return json({ request_id: body.request_id, scope: runScope, status: 'waiting', operation_id: 'op-9' });
  }));
  api.authorize(auth);
  const command = executeCommand(runScope, 'session-1', plan(), 'delete', 'req-e');
  await assert.rejects(api.command(command));
  assert.equal(bodies.length, 1);
  const checked = await api.command(command) as Receipt;
  assert.equal(checked.operation_id, 'op-9');
  assert.equal(JSON.stringify(bodies[0]), JSON.stringify(bodies[1]));
});
test('availability and rights are read from the source itself, not assumed', () => {
  // a stale copy of text on a source that is no longer available stays hidden
  assert.equal(visibleText(sourceCard(source({ availability: 'expired', content: 'остаток' }))), null);
  assert.equal(visibleText(sourceCard(source({ availability: 'deleted', content: 'остаток' }))), null);
  assert.equal(visibleText(sourceCard(source({ availability: 'unavailable', content: 'остаток' }))), null);
  assert.deepEqual(rightsText(source({ read_allowed: true, api_transfer_allowed: false, public_disclosure_allowed: true })),
    ['Чтение: да', 'Передача в API: нет', 'Публичное раскрытие: да']);
  assert.deepEqual(rightsText(source({ read_allowed: false, api_transfer_allowed: true, public_disclosure_allowed: false })),
    ['Чтение: нет', 'Передача в API: да', 'Публичное раскрытие: нет']);
});
test('cursor and resource ids with separators or query syntax are refused before any request', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  for (const cursor of ['a&limit=200', 'a b', 'a/b', 'a#b', 'x'.repeat(129), '']) {
    await assert.rejects(api.sources(scope, undefined, cursor), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  }
  for (const id of ['a/b', 'a?b=c', 'a b', 'a#b', '', 'x'.repeat(129)]) {
    await assert.rejects(api.plan(scope, id), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  }
  assert.equal(paths.length, 0);
});
