import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { encounters, people, promises, relationships, topics } from '../src/shared/validation.ts';
import {
  collisionsOf, confirmedAccounts, emptyLink, encountersOf, groundsSummary, involves, label, linkCommand, linkProblem, orderPromises, otherAccounts, overdue,
  ownerOf, relationshipOf, sharedNames, unlinkCommand, unlinkProblem,
} from '../src/ui/people/model.ts';
import type { AccountLink, Encounter, Person, PromiseRecord, Relationship, RunSession, Scope, SourceRef } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-14T00:${String(minute).padStart(2, '0')}:00Z`;
const NOW = Date.parse(T(30));
const base = { schema_version: '1.0', scope, created_at: T(1), updated_at: T(1), version: 1 };
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const ref = (availability: SourceRef['availability'] = 'available'): SourceRef => ({ source_id: 's1', version: 1, scope, availability });
const run = (): RunSession => ({ ...base, id: 'run-1', scope: runScope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1', state: 'ACTIVE', activity: 'OPEN', autonomy: 'observation', gm_paused: true, voice_enabled: false, control_epoch: 2, version: 4 } as unknown as RunSession);
const account = (platform: string, id: string, name: string, confirmation: AccountLink['confirmation'] = 'confirmed'): AccountLink => ({ platform_id: platform, account_id: id, display_name: name, confirmation, evidence: [ref()] });
const person = (id: string, accounts: AccountLink[], over: object = {}): Person => ({ ...base, id: 'row-' + id, person_id: id, accounts, ...over } as unknown as Person);
const meeting = (name: string, ids: string[], minute: number): Encounter => ({ ...base, id: name, person_ids: ids, sources: [ref()], created_at: T(minute) } as unknown as Encounter);
const promise = (name: string, status: string, over: object = {}): PromiseRecord => ({ ...base, id: name, participant_ids: ['p1'], topic_id: 't1', delivered_utterance_id: 'u1', delivery_id: 'd1', sources: [ref()], content: 'Вернуться к вопросу',
  deadline: null, completion_criterion: 'Ответить', status, closure_result: status === 'fulfilled' || status === 'withdrawn' ? 'закрыто' : null, ...over } as unknown as PromiseRecord);

test('a nickname never identifies a person; only a shared display name is reported as a collision', () => {
  const anna = person('p1', [account('chat', 'a-1', 'Ника')]);
  const other = person('p2', [account('chat', 'a-2', ' ника ')]);
  const alone = person('p3', [account('chat', 'a-3', 'Игорь')]);
  const shared = sharedNames([anna, other, alone]);
  assert.deepEqual([...shared], [['ника', ['p1', 'p2']]]);
  assert.deepEqual(collisionsOf(anna, shared), ['p2']);
  assert.deepEqual(collisionsOf(other, shared), ['p1']);
  assert.deepEqual(collisionsOf(alone, shared), []);
  assert.equal(sharedNames([person('p1', [account('chat', 'a-1', 'Ника'), account('web', 'w-1', 'ника')])]).size, 0); // one person with two accounts
  assert.equal(sharedNames([person('p1', [account('chat', 'a-1', '  ')]), person('p2', [account('chat', 'a-2', '')])]).size, 0);
});
test('accounts are split into confirmed and the rest; the label prefers a confirmed name', () => {
  const p = person('p1', [account('chat', 'a-1', 'Гипотеза', 'hypothesis'), account('chat', 'a-2', 'Настоящее', 'confirmed'), account('web', 'w', 'Отклонён', 'rejected')]);
  assert.deepEqual(confirmedAccounts(p).map((a) => a.account_id), ['a-2']);
  assert.deepEqual(otherAccounts(p).map((a) => a.account_id), ['a-1', 'w']);
  assert.equal(label(p), 'Настоящее');
  assert.equal(label(person('p2', [account('chat', 'a', 'Только гипотеза', 'hypothesis')])), 'Только гипотеза');
  assert.equal(label(person('p3', [])), 'p3');
});
test('history, relationship and participation are found by person id and ordered newest first', () => {
  const rows = [meeting('m1', ['p1'], 2), meeting('m2', ['p1', 'p2'], 9), meeting('m3', ['p2'], 5)];
  assert.deepEqual(encountersOf(rows, 'p1').map((m) => m.id), ['m2', 'm1']);
  assert.deepEqual(encountersOf(rows, 'p9'), []);
  const relations = [{ ...base, id: 'r1', person_id: 'p2', aspects: { schema_id: 'a', data: {} }, grounds: [ref()] }] as unknown as Relationship[];
  assert.equal(relationshipOf(relations, 'p2')?.id, 'r1');
  assert.equal(relationshipOf(relations, 'p1'), undefined);
  assert.deepEqual(involves([{ participant_ids: ['p1'] }, { participant_ids: ['p2', 'p1'] }, { participant_ids: ['p3'] }], 'p1').length, 2);
  assert.equal(groundsSummary([ref(), ref('deleted')]), '2 основ., недоступно: 1');
  assert.equal(groundsSummary([ref()]), '1 основ.');
});
test('promises: doubtful first, then open by deadline, closed last; overdue only for open ones', () => {
  const rows = [promise('done', 'fulfilled'), promise('late', 'open', { deadline: T(50) }), promise('soon', 'open', { deadline: T(10) }), promise('who-knows', 'unknown'), promise('nodate', 'open')];
  assert.deepEqual(orderPromises(rows).map((r) => r.id), ['who-knows', 'soon', 'late', 'nodate', 'done']);
  assert.equal(overdue(promise('a', 'open', { deadline: T(10) }), NOW), true);
  assert.equal(overdue(promise('a', 'open', { deadline: T(40) }), NOW), false);
  assert.equal(overdue(promise('a', 'open'), NOW), false);
  assert.equal(overdue(promise('a', 'fulfilled', { deadline: T(10) }), NOW), false);
  assert.equal(overdue(promise('a', 'unknown', { deadline: T(10) }), NOW), false);
});
test('linking needs platform, account, a reason and an explicit tick, and never fuses two people', () => {
  const p1 = person('p1', [account('chat', 'a-1', 'Ника')]); const p2 = person('p2', [account('chat', 'a-2', 'Ника')]);
  const ok = { platform: 'web', account: 'w-9', reason: 'Сам сказал', confirmed: true };
  assert.equal(linkProblem(p1, [p1, p2], ok), null);
  for (const platform of ['', 'a b', '-x']) assert.match(linkProblem(p1, [p1, p2], { ...ok, platform })!, /Платформа/, platform);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, account: '  ' })!, /аккаунта/);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, account: 'я'.repeat(513) })!, /аккаунта/);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, reason: ' ' })!, /основание/);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, reason: 'я'.repeat(1001) })!, /1000/);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, confirmed: false })!, /Подтвердите/);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, platform: 'chat', account: 'a-2' })!, /другого человека \(p2\)/);
  assert.match(linkProblem(p1, [p1, p2], { ...ok, platform: 'chat', account: 'a-1' })!, /уже подтверждён у человека/);
  assert.equal(linkProblem(p1, [p1, p2, person('p3', [account('web', 'w-9', 'X', 'hypothesis')])], ok), null); // a hypothesis does not own an account
  assert.equal(ownerOf([p1, p2], 'chat', 'a-2'), 'p2');
  assert.equal(ownerOf([p1, p2], 'chat', 'zzz'), null);
  assert.equal(ownerOf([p1], 'web', 'a-1'), null);
  assert.deepEqual(emptyLink, { platform: '', account: '', reason: '', confirmed: false });
});
test('link and unlink carry the version of the person the operator saw and the reason', () => {
  const p = person('p1', [account('chat', 'a-1', 'Ника')], { version: 6 });
  const link = linkCommand(run(), p, { platform: 'web', account: ' w-9 ', reason: ' Сам сказал ', confirmed: true }, 'req-l');
  assert.deepEqual([link.type, link.expected_version, link.target], ['person.link', 6, 'session-1']);
  assert.deepEqual(link.arguments.data, { person_id: 'p1', platform_id: 'web', account_id: 'w-9', reason: 'Сам сказал' });
  const unlink = unlinkCommand(run(), p, p.accounts[0], ' ошибочная связь ', 'req-u');
  assert.deepEqual([unlink.type, unlink.expected_version, unlink.arguments.data], ['person.unlink', 6, { person_id: 'p1', platform_id: 'chat', account_id: 'a-1', reason: 'ошибочная связь' }]);
  assert.match(unlinkProblem(' ')!, /Укажите/);
  assert.match(unlinkProblem('я'.repeat(1001))!, /1000/);
  assert.equal(unlinkProblem('причина'), null);
});
test('people projections from another partition or with impossible values are rejected', () => {
  const p = person('p1', [account('chat', 'a-1', 'Ника')]);
  assert.throws(() => people(page([{ ...p, scope: { ...scope, memory_mode: 'test' } }]), scope));
  assert.throws(() => people(page([person('p1', [{ ...account('chat', 'a', 'x'), confirmation: 'sure' as never }])]), scope));
  assert.throws(() => people(page([person('bad id', [])]), scope));
  assert.throws(() => people(page([person('p1', [{ ...account('chat', 'a', 'x'), platform_id: 'bad id' }])]), scope));
  assert.throws(() => people(page([person('p1', [{ ...account('chat', 'a', 'x'), evidence: [{ ...ref(), availability: 'lost' as never }] }])]), scope));
  assert.throws(() => encounters(page([meeting('m', [], 2)]), scope));
  assert.throws(() => encounters(page([meeting('m', ['bad id'], 2)]), scope));
  assert.throws(() => relationships(page([{ ...base, id: 'r', person_id: 'p1', aspects: 'x', grounds: [ref()] }]), scope));
  assert.throws(() => topics(page([{ ...base, id: 't', topic_id: 't1', status: 'maybe', participant_ids: [], sources: [ref()] }]), scope));
  assert.throws(() => promises(page([promise('x', 'soon')]), scope));
  assert.throws(() => promises(page([promise('x', 'fulfilled', { closure_result: null })]), scope));
  assert.throws(() => promises(page([promise('x', 'open', { participant_ids: [] })]), scope));
  assert.equal(people(page([p]), scope).items.length, 1);
  assert.equal(encounters(page([meeting('m', ['p1'], 2)]), scope).items.length, 1);
  assert.equal(promises(page([promise('x', 'fulfilled')]), scope).items.length, 1);
  assert.equal(topics(page([{ ...base, id: 't', topic_id: 't1', status: 'answered', participant_ids: ['p1'], sources: [ref()], answer: 'да' }]), scope).items.length, 1);
  assert.equal(relationships(page([{ ...base, id: 'r', person_id: 'p1', aspects: { schema_id: 'a', data: {} }, grounds: [ref()] }]), scope).items.length, 1);
});
test('people reads use the allowlisted resources, the partition and a validated cursor', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.people(scope, undefined, 'cur-1'); await api.encounters(scope); await api.relationships(scope); await api.topics(scope); await api.promises(scope);
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname), ['/api/v1/people', '/api/v1/encounters', '/api/v1/relationships', '/api/v1/topics', '/api/v1/promises']);
  assert.equal(new URL(paths[0], 'http://x').searchParams.get('cursor'), 'cur-1');
  await assert.rejects(api.people(scope, undefined, 'a&b'), (error: unknown) => error instanceof ApiFailure && error.code === 'INVALID_ARGUMENTS');
  assert.equal(paths.length, 5);
});
test('collisions list each other person once and in order, however many names they share', () => {
  const p1 = person('p1', [account('chat', 'a-1', 'Ника'), account('web', 'w-1', 'Nika')]);
  const p2 = person('p2', [account('chat', 'a-2', 'ника'), account('web', 'w-2', 'nika')]);
  const p9 = person('p9', [account('chat', 'a-9', 'Ника')]);
  const p3 = person('p3', [account('web', 'w-3', 'NIKA')]);
  const shared = sharedNames([p1, p2, p9, p3]);
  assert.deepEqual(collisionsOf(p1, shared), ['p2', 'p3', 'p9']);
  assert.deepEqual(collisionsOf(p9, shared), ['p1', 'p2']);
  assert.deepEqual(collisionsOf(p3, shared), ['p1', 'p2']);
});
test('a person id that is not an id is rejected even when the row id is fine', () => {
  const bad = { ...person('p1', []), person_id: 'bad id' };
  assert.throws(() => people(page([bad]), scope));
  assert.equal(people(page([person('p1', [])]), scope).items.length, 1);
});
