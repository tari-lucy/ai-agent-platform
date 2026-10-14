import test from 'node:test';
import assert from 'node:assert/strict';
import {
  TEXT_LIMIT, applyBlocker, applyCommand, changedLines, checkedFrom, draftKey, draftProblems, emptyNew, hasProblems, idPattern, lineDiff, newCommand, newProblem,
  propertyRows, stateNote, validateCommand,
} from '../src/ui/persona/model.ts';
import type { CharacterState, Profile, Receipt, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const base = { schema_version: '1.0', scope, created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z', version: 1 };
const profile = (over: object = {}): Profile => ({ ...base, id: 'profile-1', persona_id: 'alpha', canon: 'Строка один\nСтрока два', style: 'Мягкий стиль', version: 3, accepted_revision_id: 'rev-1',
  property_definitions: { schema_id: 'd', data: { trust: { autonomy: 'experience' }, curiosity: 'свободно' } },
  initial_values: { schema_id: 'i', data: { trust: 0.4, curiosity: 0.7, calm: 'high' } }, development_bounds: { schema_id: 'b', data: {} }, ...over } as unknown as Profile);
const state = (over: object = {}): CharacterState => ({ ...base, id: 'state-1', persona_id: 'alpha', profile_version: 3, state_version: 5, values: { schema_id: 'v', data: { trust: 0.55, curiosity: 0.7 } }, ...over } as unknown as CharacterState);
const draft = { canon: 'Строка один\nСтрока два', style: 'Мягкий стиль' };

test('line diff shows exactly what was added and removed', () => {
  assert.deepEqual(lineDiff('a\nb\nc', 'a\nb\nc').map((l) => l.op), ['same', 'same', 'same']);
  assert.deepEqual(lineDiff('a\nb\nc', 'a\nX\nc'), [{ op: 'same', text: 'a' }, { op: 'del', text: 'b' }, { op: 'add', text: 'X' }, { op: 'same', text: 'c' }]);
  assert.deepEqual(lineDiff('a', 'a\nb').map((l) => [l.op, l.text]), [['same', 'a'], ['add', 'b']]);
  assert.deepEqual(lineDiff('a\nb', 'a').map((l) => [l.op, l.text]), [['same', 'a'], ['del', 'b']]);
  assert.deepEqual(lineDiff('', 'x').map((l) => l.op).sort(), ['add', 'del']); // an empty text is one empty line
  assert.equal(changedLines(lineDiff('a\nb\nc', 'a\nX\nc')), 2);
  assert.equal(changedLines(lineDiff('same', 'same')), 0);
  assert.equal(lineDiff('x\ny', 'y\nx').filter((l) => l.op === 'same').length, 1);
});
test('field problems sit with their field; an unchanged draft is not a change', () => {
  assert.deepEqual(draftProblems(profile(), { canon: '  ', style: 'ок' }), { canon: 'Поле не может быть пустым.' });
  assert.deepEqual(draftProblems(profile(), { canon: 'ок', style: '' }), { style: 'Поле не может быть пустым.' });
  assert.match(draftProblems(profile(), { canon: 'я'.repeat(TEXT_LIMIT + 1), style: 'ок' }).canon!, /символов/);
  assert.match(draftProblems(profile(), { canon: 'ок', style: 'я'.repeat(TEXT_LIMIT + 1) }).style!, /символов/);
  assert.deepEqual(draftProblems(profile(), draft), { general: 'Изменений нет.' });
  assert.deepEqual(draftProblems(profile(), { canon: 'Новый канон', style: 'Мягкий стиль' }), {});
  assert.deepEqual(draftProblems(profile(), { canon: 'я'.repeat(TEXT_LIMIT), style: 'ок' }), {});
  assert.equal(hasProblems({}), false);
  assert.equal(hasProblems({ general: 'x' }), true);
});
test('static values and live values stay in separate columns and drift is named', () => {
  const rows = propertyRows(profile(), state());
  assert.deepEqual(rows.map((r) => r.name), ['calm', 'curiosity', 'trust']);
  const trust = rows.find((r) => r.name === 'trust')!;
  assert.deepEqual([trust.initial, trust.live, trust.drifted], ['0.4', '0.55', true]);
  assert.equal(rows.find((r) => r.name === 'curiosity')!.drifted, false);
  const calm = rows.find((r) => r.name === 'calm')!;
  assert.deepEqual([calm.initial, calm.live, calm.drifted, calm.definition], ['high', null, false, 'определение не задано']);
  assert.equal(rows.find((r) => r.name === 'curiosity')!.definition, 'свободно');
  assert.match(trust.definition, /experience/);
  assert.deepEqual(propertyRows(profile(), undefined).map((r) => r.live), [null, null, null]);
  assert.deepEqual(propertyRows(undefined, undefined), []);
  assert.deepEqual(propertyRows(profile({ initial_values: { schema_id: 'i', data: [] } }), state()).map((r) => r.name), ['curiosity', 'trust']);
});
test('the state note names a missing state and a profile version mismatch', () => {
  assert.equal(stateNote(profile(), state()), null);
  assert.match(stateNote(undefined, state())!, /недоступен/);
  assert.match(stateNote(profile(), undefined)!, /недоступно/);
  assert.match(stateNote(profile(), state({ profile_version: 2 }))!, /v2.*v3/);
});
test('apply opens only for the exact draft and profile version that was validated, with a digest', () => {
  const p = profile();
  const receipt = (resource: string | undefined) => ({ request_id: 'r', scope: runScope, status: 'completed', resource_id: resource }) as unknown as Receipt;
  assert.match(applyBlocker(p, draft, null)!, /Сначала проверьте/);
  const ok = checkedFrom(p, draft, receipt('rev-abc'));
  assert.equal(applyBlocker(p, draft, ok), null);
  assert.match(applyBlocker(p, { ...draft, canon: 'другое' }, ok)!, /проверьте заново/);
  assert.match(applyBlocker(p, { ...draft, style: 'другое' }, ok)!, /проверьте заново/);
  assert.match(applyBlocker(profile({ version: 4 }), draft, ok)!, /проверьте заново/);
  assert.match(applyBlocker(profile({ id: 'other' }), draft, ok)!, /проверьте заново/);
  assert.match(applyBlocker(p, draft, checkedFrom(p, draft, receipt(undefined)))!, /не вернуло/);
  assert.notEqual(draftKey(p, draft), draftKey(p, { ...draft, canon: 'x' }));
});
test('validate and apply carry the seen version; apply repeats the checked digest', () => {
  const p = profile({ version: 7 });
  const v = validateCommand(runScope, 'session-1', p, draft, 'req-v');
  assert.deepEqual([v.type, v.expected_version, v.target], ['persona.validate', 7, 'session-1']);
  assert.deepEqual(v.arguments.data, { profile_id: 'profile-1', canon: draft.canon, style: draft.style });
  const a = applyCommand(runScope, 'session-1', p, { canon: 'Новый', style: 'Стиль' }, 'rev-abc', 'req-a');
  assert.deepEqual([a.type, a.expected_version], ['persona.apply', 7]);
  assert.deepEqual(a.arguments.data, { profile_id: 'profile-1', canon: 'Новый', style: 'Стиль', revision_digest: 'rev-abc' });
  assert.deepEqual(a.scope, runScope);
});
test('a new persona needs a free id, its own memory space and, from scratch, a canon and a style', () => {
  const ok = { ...emptyNew, persona: 'beta', space: 'beta-work' };
  assert.equal(newProblem(ok, ['alpha']), null);
  for (const persona of ['', ' ', 'a b', '-x', 'я', 'x'.repeat(129)]) assert.match(newProblem({ ...ok, persona }, [])!, /Идентификатор/, persona);
  assert.match(newProblem({ ...ok, persona: 'alpha' }, ['alpha'])!, /уже есть/);
  assert.match(newProblem({ ...ok, space: '' }, [])!, /область памяти/);
  assert.match(newProblem({ ...ok, space: 'a b' }, [])!, /область памяти/);
  const scratch = { ...ok, mode: 'create' as const, canon: 'Канон', style: 'Стиль' };
  assert.equal(newProblem(scratch, []), null);
  assert.match(newProblem({ ...scratch, canon: ' ' }, [])!, /канон и стиль/);
  assert.match(newProblem({ ...scratch, style: '' }, [])!, /канон и стиль/);
  assert.match(newProblem({ ...scratch, canon: 'я'.repeat(TEXT_LIMIT + 1) }, [])!, /не длиннее/);
  assert.equal(newProblem({ ...ok, canon: '', style: '' }, []), null); // a copy needs no text
  assert.equal(idPattern.test('beta-1'), true);
});
test('copy names the source and never carries text or experience; create carries canon and style', () => {
  const copy = newCommand(scope, profile(), { ...emptyNew, persona: 'beta', space: 'beta-work', canon: 'не уйдёт', style: 'не уйдёт' }, 'req-c');
  assert.equal(copy.type, 'persona.copy');
  assert.deepEqual(copy.arguments.data, { persona_id: 'beta', memory_space_id: 'beta-work', source_profile_id: 'profile-1' });
  const create = newCommand(scope, profile(), { mode: 'create', persona: 'gamma', space: 'gamma-work', canon: 'Канон', style: 'Стиль' }, 'req-n');
  assert.equal(create.type, 'persona.create');
  assert.deepEqual(create.arguments.data, { persona_id: 'gamma', memory_space_id: 'gamma-work', canon: 'Канон', style: 'Стиль' });
  assert.deepEqual(newCommand(scope, undefined, { ...emptyNew, persona: 'beta', space: 'b' }, 'r').arguments.data, { persona_id: 'beta', memory_space_id: 'b' });
});
