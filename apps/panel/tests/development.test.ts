import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { characterStates, opportunities, stateChanges, storyArcs } from '../src/shared/validation.ts';
import {
  arcStateNote, attention, awaitingOperator, cancelOpportunityCommand, canCancelOpportunity, canCorrect, canPauseArc, canRollback, decisionCommand,
  evidenceLost, evidenceSummary, gmIntensityCommand, gmPauseCommand, interpretationCommand, isLived, isPossibility, laterExperience, laterExperienceNote,
  opportunitiesOf, patchOf, pathItems,
  pauseArcCommand, rollbackExecute, rollbackPreview, rollbackProblem, validInterpretation, versions,
} from '../src/ui/development/model.ts';
import type { GmRuntimeState, ConsequencePlan, Episode, Opportunity, RunSession, Scope, SourceRef, StateChange, StoryArc } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-14T00:${String(minute).padStart(2, '0')}:00Z`;
const NOW = Date.parse(T(30));
const base = { schema_version: '1.0', scope, created_at: T(1), updated_at: T(1), version: 1 };
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const ref = (availability: SourceRef['availability'] = 'available'): SourceRef => ({ source_id: 's1', version: 1, scope, availability });
const run = (over: object = {}): RunSession => ({ ...base, id: 'run-1', scope: runScope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1', state: 'ACTIVE',
  activity: 'OPEN', autonomy: 'observation', gm_paused: true, voice_enabled: false, control_epoch: 2, version: 4, ...over } as unknown as RunSession);
const arc = (over: object = {}): StoryArc => ({ ...base, id: 'arc-1', direction: 'Найти причину страха воды', boundaries: 'Без насилия', status: 'open', grounds: [ref()], episode_ids: [], cooldown_until: null, rules_version: 1, ...over } as unknown as StoryArc);
const opp = (name: string, status: string, over: object = {}): Opportunity => ({ ...base, id: name, arc_id: 'arc-1', grounds: [ref()], applicability: 'Предложить прогулку к озеру', boundaries: 'Без принуждения',
  expires_at: T(50), capability_ids: [], status, choice: null, actual_outcome: null, reason_code: null, rolls: [], ...over } as unknown as Opportunity);
const patch = (over: object = {}) => ({ schema_id: 'development.patch', data: { property: 'trust', before: 0.4, after: 0.5, kind: 'change', ...over } });
const change = (name: string, status: string, over: object = {}): StateChange => ({ ...base, id: name, proposal_id: 'p-' + name, owner: 'persona', base_version: 3, new_version: null,
  patch: patch(), evidence: [ref()], interpretation: 'Он дважды помог', uncertainty: 0.3, expected_manifestation: 'Охотнее доверяет', status, checks: [], ...over } as unknown as StateChange);
const plan = (over: object = {}): ConsequencePlan => ({ ...base, id: 'plan-1', operation_type: 'change.rollback', consequences: 'Будет создана компенсирующая версия.',
  dependencies: [{ resource_id: 'c1', version: 1 }, { resource_id: 'op-x', version: 1 }], expires_at: T(50), ...over } as unknown as ConsequencePlan);

test('possibilities, lived events and the rest are kept apart', () => {
  for (const status of ['DRAFT', 'SCREENED', 'OFFERED', 'ACCEPTED']) { assert.equal(isPossibility(opp('o', status)), true, status); assert.equal(isLived(opp('o', status)), false); }
  assert.equal(isLived(opp('o', 'RESOLVED')), true);
  for (const status of ['REALIZING', 'RESOLVED', 'BLOCKED', 'DECLINED', 'EXPIRED', 'CANCELLED']) assert.equal(isPossibility(opp('o', status)), false, status);
  const groups = opportunitiesOf(arc(), [opp('a', 'OFFERED'), opp('b', 'RESOLVED', { choice: 'пошёл', actual_outcome: 'вернулся' }), opp('c', 'BLOCKED'), opp('d', 'EXPIRED'), opp('e', 'REALIZING'),
    opp('f', 'OFFERED', { arc_id: 'arc-2' })]);
  assert.deepEqual([groups.possible.map((o) => o.id), groups.lived.map((o) => o.id), groups.blocked.map((o) => o.id), groups.other.map((o) => o.id).sort()], [['a'], ['b'], ['c'], ['d', 'e']]);
});
test('only open possibilities can be cancelled and only an open arc can be paused', () => {
  for (const status of ['DRAFT', 'SCREENED', 'OFFERED', 'ACCEPTED']) assert.equal(canCancelOpportunity(opp('o', status)), true, status);
  for (const status of ['REALIZING', 'RESOLVED', 'BLOCKED', 'DECLINED', 'EXPIRED', 'CANCELLED']) assert.equal(canCancelOpportunity(opp('o', status)), false, status);
  assert.equal(canPauseArc(arc()), true);
  assert.equal(canPauseArc(arc({ status: 'paused' })), false);
  assert.equal(canPauseArc(arc({ status: 'closed' })), false);
});
test('the arc note says what a pause does and warns about blocked tests', () => {
  assert.match(arcStateNote(arc({ status: 'paused' }), []), /не оживляет/);
  assert.match(arcStateNote(arc({ status: 'closed' }), []), /закрыта/);
  assert.match(arcStateNote(arc(), [opp('c', 'BLOCKED'), opp('d', 'BLOCKED'), opp('e', 'BLOCKED', { arc_id: 'arc-2' })]), /заблокированные границей испытания: 2/);
  assert.match(arcStateNote(arc(), []), /автоматически/);
});
test('the patch is read defensively and an unrecognised shape is not guessed', () => {
  assert.deepEqual(patchOf(change('c', 'APPLIED')), { property: 'trust', before: '0.4', after: '0.5', rollback: false });
  assert.equal(patchOf(change('c', 'APPLIED', { patch: patch({ kind: 'rollback' }) }))?.rollback, true);
  for (const data of [null, [], 'x', { property: 'p' }, { property: 1, before: 1, after: 2 }, { property: 'p', before: {}, after: 2 }]) {
    assert.equal(patchOf(change('c', 'APPLIED', { patch: { schema_id: 'x', data } })), null, JSON.stringify(data));
  }
});
test('which controls a change offers follows its state', () => {
  assert.equal(awaitingOperator(change('c', 'AWAITING_APPROVAL')), true);
  assert.equal(awaitingOperator(change('c', 'READY')), false);
  for (const status of ['READY', 'AWAITING_APPROVAL', 'APPLIED']) assert.equal(canCorrect(change('c', status)), true, status);
  for (const status of ['PROPOSED', 'VALIDATING', 'REJECTED', 'CONFLICT', 'CANCELLED']) assert.equal(canCorrect(change('c', status)), false, status);
  assert.equal(canRollback(change('c', 'APPLIED')), true);
  assert.equal(canRollback(change('c', 'READY')), false);
  assert.equal(canRollback(change('c', 'APPLIED', { patch: patch({ kind: 'rollback' }) })), false); // a rollback is not rolled back again
});
test('versions, evidence and lost evidence are stated, not smoothed over', () => {
  assert.equal(versions(change('c', 'READY')), 'основа v3, ещё не применено');
  assert.equal(versions(change('c', 'APPLIED', { new_version: 4 })), 'v3 → v4');
  assert.equal(evidenceSummary([ref(), ref('deleted')]), '2 основ., недоступно: 1');
  assert.equal(evidenceSummary([ref()]), '1 основ.');
  assert.equal(evidenceLost(change('c', 'APPLIED', { evidence: [ref('deleted'), ref('expired')] })), true);
  assert.equal(evidenceLost(change('c', 'APPLIED', { evidence: [ref('deleted'), ref()] })), false);
  assert.equal(evidenceLost(change('c', 'APPLIED', { evidence: [] })), false);
});
test('attention collects waiting approvals, conflicts and boundary blocks only', () => {
  const rows = attention([change('c1', 'AWAITING_APPROVAL'), change('c2', 'CONFLICT'), change('c3', 'APPLIED'), change('c4', 'READY')],
    [opp('o1', 'BLOCKED', { reason_code: 'граница-насилие' }), opp('o2', 'BLOCKED'), opp('o3', 'OFFERED')]);
  assert.deepEqual(rows.map((row) => row.kind), ['approval', 'conflict', 'blocked', 'blocked']);
  assert.match(rows[0].text, /trust/);
  assert.match(rows[2].text, /граница-насилие/);
  assert.match(rows[3].text, /причина не указана/);
  assert.deepEqual(attention([], []), []);
});
test('the path has lived events and applied changes in time order, never possibilities or pending changes', () => {
  const items = pathItems([change('c1', 'APPLIED', { updated_at: T(9) }), change('c2', 'READY'), change('c3', 'APPLIED', { updated_at: T(2), patch: patch({ kind: 'rollback', before: 0.5, after: 0.4 }) }),
    change('c4', 'APPLIED', { patch: { schema_id: 'x', data: 'bad' } })],
  [opp('o1', 'RESOLVED', { updated_at: T(5), choice: 'пошёл', actual_outcome: 'вернулся' }), opp('o2', 'OFFERED', { updated_at: T(6) }), opp('o3', 'RESOLVED', { updated_at: T(7) })]);
  assert.deepEqual(items.map((item) => item.key), ['c:c1', 'o:o3', 'o:o1', 'c:c3', 'c:c4']);
  assert.equal(items.find((item) => item.key === 'c:c3')!.kind, 'rollback');
  assert.equal(items.find((item) => item.key === 'c:c1')!.title, 'trust: 0.4 → 0.5');
  assert.equal(items.find((item) => item.key === 'o:o3')!.title, 'Выбор не записан');
  assert.equal(items.find((item) => item.key === 'o:o1')!.detail, 'вернулся');
  assert.match(items.find((item) => item.key === 'c:c4')!.title, /не распознан/);
  assert.equal(items.some((item) => item.key === 'o:o2' || item.key === 'c:c2'), false);
});
test('commands carry the version seen and the right target', () => {
  const r = run();
  const settings = { id: 'gm-runtime', version: 12 } as GmRuntimeState;
  const pause = gmPauseCommand(r, settings, false, 'req-1');
  assert.deepEqual([pause.type, pause.expected_version, pause.target, pause.arguments.data], ['gm.configure', 12, 'gm-runtime', { paused: false }]);
  assert.deepEqual(gmIntensityCommand(r, settings, 'high', 'req-2').arguments.data, { intensity: 0.8 });
  const cancel = cancelOpportunityCommand(r, opp('o1', 'OFFERED', { version: 6 }), 'req-3');
  assert.deepEqual([cancel.type, cancel.expected_version, cancel.arguments.data], ['opportunity.cancel', 6, {}]);
  const stop = pauseArcCommand(r, arc({ version: 2 }), 'req-4');
  assert.deepEqual([stop.type, stop.expected_version, stop.arguments.data], ['arc.pause', 2, {}]);
  const decision = decisionCommand(r, change('c1', 'AWAITING_APPROVAL', { version: 5 }), 'reject', ' слишком быстро ', 'req-5');
  assert.deepEqual([decision.type, decision.expected_version, decision.arguments.data], ['change.decision', 5, { decision: 'reject', reason: 'слишком быстро' }]);
  assert.deepEqual(decision.scope, runScope);
});
test('an interpretation correction needs a different text, a reason and an editable state', () => {
  const c = change('c1', 'AWAITING_APPROVAL');
  assert.equal(validInterpretation(c, 'Он помог лишь однажды', 'уточнение'), null);
  assert.match(validInterpretation(change('c', 'REJECTED'), 'x', 'y')!, /нельзя/);
  assert.match(validInterpretation(c, '  ', 'y')!, /Введите/);
  assert.match(validInterpretation(c, 'я'.repeat(1501), 'y')!, /1500/);
  assert.match(validInterpretation(c, c.interpretation, 'y')!, /не отличается/);
  assert.match(validInterpretation(c, 'другое', ' ')!, /основание/);
  assert.match(validInterpretation(c, 'другое', 'я'.repeat(1001))!, /1000/);
  const cmd = interpretationCommand(run(), change('c1', 'READY', { version: 7 }), 'Новая версия', ' причина ', 'req-6');
  assert.deepEqual([cmd.type, cmd.expected_version, cmd.arguments.data], ['change.interpretation', 7, { interpretation: 'Новая версия', reason: 'причина' }]);
});
test('a rollback plan names the change, is bound to its version, and expires', () => {
  const preview = rollbackPreview(run(), change('c1', 'APPLIED', { version: 8 }), 'req-p');
  assert.deepEqual([preview.type, preview.expected_version, preview.arguments.data], ['change.rollback.preview', 8, {}]);
  const exec = rollbackExecute(run(), change('c1', 'APPLIED'), plan({ version: 3 }), 'req-e');
  assert.deepEqual([exec.type, exec.expected_version, exec.arguments], ['change.rollback.execute', 3, { plan_id: 'plan-1', plan_version: 3 }]);
  assert.equal(rollbackProblem(plan(), change('c1', 'APPLIED'), NOW), null);
  assert.match(rollbackProblem(plan({ dependencies: [{ resource_id: 'other', version: 1 }] }), change('c1', 'APPLIED'), NOW)!, /не содержит/);
  assert.match(rollbackProblem(plan({ expires_at: T(10) }), change('c1', 'APPLIED'), NOW)!, /истёк/);
});
test('development projections from another partition or with impossible values are rejected', () => {
  assert.throws(() => storyArcs(page([{ ...arc(), scope: { ...scope, memory_mode: 'test' } }]), scope));
  assert.throws(() => storyArcs(page([arc({ status: 'finished' })]), scope));
  assert.throws(() => storyArcs(page([arc({ cooldown_until: 'soon' })]), scope));
  assert.throws(() => opportunities(page([opp('o', 'MAYBE')]), scope));
  assert.throws(() => opportunities(page([opp('o', 'OFFERED', { rolls: [1, 2, 3, 4] })]), scope));
  assert.throws(() => opportunities(page([opp('o', 'OFFERED', { arc_id: 'bad id' })]), scope));
  assert.throws(() => stateChanges(page([change('c', 'DONE')]), scope));
  assert.throws(() => stateChanges(page([change('c', 'READY', { uncertainty: 2 })]), scope));
  assert.throws(() => stateChanges(page([change('c', 'READY', { patch: 'x' })]), scope));
  assert.throws(() => stateChanges(page([change('c', 'READY', { base_version: 0 })]), scope));
  assert.throws(() => characterStates(page([{ ...base, id: 's', persona_id: 'alpha', profile_version: 0, state_version: 1, values: {} }]), scope));
  assert.equal(storyArcs(page([arc()]), scope).items.length, 1);
  assert.equal(opportunities(page([opp('o', 'OFFERED')]), scope).items.length, 1);
  assert.equal(stateChanges(page([change('c', 'READY')]), scope).items.length, 1);
  assert.equal(characterStates(page([{ ...base, id: 's', persona_id: 'alpha', profile_version: 1, state_version: 1, values: { schema_id: 'v', data: {} } }]), scope).items.length, 1);
});
test('development reads use the allowlisted resources, the partition and a validated cursor', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.arcs(scope, undefined, 'cur-1'); await api.opportunities(scope); await api.changes(scope); await api.characterStates(scope);
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname), ['/api/v1/arcs', '/api/v1/opportunities', '/api/v1/changes', '/api/v1/character-states']);
  assert.equal(new URL(paths[0], 'http://x').searchParams.get('cursor'), 'cur-1');
  await assert.rejects(api.changes(scope, undefined, 'a&b'), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  assert.equal(paths.length, 4);
});

test('an applied change is shown next to the experience lived after it, never scored', () => {
  const applied = change('c1', 'APPLIED', { new_version: 4, updated_at: T(10) });
  const episode = (name: string, minute: number, choice = 'Ответил оператору.'): Episode => ({ ...base, id: name, created_at: T(minute), sources: [ref()],
    experience_source_ids: [], participant_ids: [], choice, actual_outcome: 'Доставлено.', operation_id: 'op-' + name, provenance: 'observation', outcome_confirmed: true } as unknown as Episode);
  const before = [episode('e0', 5)];
  assert.deepEqual(laterExperience(applied, before, true), { status: 'none', complete: true });
  assert.equal(laterExperienceNote(laterExperience(applied, before, false)), 'После изменения подходящий опыт ещё не наблюдался. Загружены не все эпизоды.');
  const after = [...before, episode('e1', 12), episode('e2', 20, 'Пошла к маяку.'), episode('e3', 15), episode('e4', 18)];
  const seen = laterExperience(applied, after, true);
  assert.equal(seen.status, 'seen');
  if (seen.status === 'seen') {
    assert.equal(seen.count, 4);
    assert.deepEqual(seen.recent.map((row) => row.id), ['e2', 'e4', 'e3']);  // newest first, bounded
  }
  assert.match(laterExperienceNote(seen), /не оценивалось автоматически/);
  assert.equal(laterExperience(change('c2', 'AWAITING_APPROVAL'), after, true).status, 'not-applied');
  const rollback = change('c3', 'APPLIED', { new_version: 5, patch: patch({ kind: 'rollback' }) });
  assert.equal(laterExperience(rollback, after, true).status, 'not-applied');
});
