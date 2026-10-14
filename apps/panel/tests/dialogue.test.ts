import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { briefings, deliveries, inbound, operations, utterances } from '../src/shared/validation.ts';
import {
  afterSend, buildTimeline, canSend, cancelBriefing, createBriefing, deliveryStatus, generating, goal,
  latestRevisions, messageCommand, validBriefing,
} from '../src/ui/dialogue/model.ts';
import type { Briefing, Delivery, OperationProjection, OperatorSession, Receipt, RunSession, Scope, Source, Utterance } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-14T00:${String(minute).padStart(2, '0')}:00Z`;
const run: RunSession = { id: 'run-1', scope: runScope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1', state: 'ACTIVE',
  activity: 'OPEN', autonomy: 'observation', gm_paused: true, voice_enabled: false, control_epoch: 2, version: 4, created_at: T(0), updated_at: T(0) };
const auth: OperatorSession = { principal_id: 'operator', scopes: [scope], csrf_token: 'c'.repeat(43), expires_at: '2099-01-01T00:00:00Z' };
const page = (items: unknown[], extra: object = {}) => ({ items, limit: 50, next_cursor: null, complete: true, ...extra });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const base = { schema_version: '1.0', scope, version: 1, updated_at: T(0) };
const utterance = (over: Partial<Utterance> = {}): Utterance => ({ ...base, id: 'u1', utterance_id: 'u1', revision: 1, content: 'Ответ', content_hash: 'h',
  audience_id: 'operator', channel_ids: ['panel'], moderation_status: 'approved', moderation_check_id: 'c1', created_at: T(2), ...over } as Utterance);
const delivery = (over: Partial<Delivery> = {}): Delivery => ({ ...base, id: 'd1', utterance_id: 'u1', revision: 1, channel_id: 'panel', attempt_id: 'a1',
  status: 'DELIVERED', created_at: T(3), ...over } as Delivery);
const source = (over: Partial<Source> = {}): Source => ({ ...base, id: 's1', source_id: 's1', provenance: 'operator_input', content: 'Привет', availability: 'available',
  occurred_at: T(1), author_id: 'operator', created_at: T(1), ...over } as unknown as Source);
const operation = (over: Partial<OperationProjection> = {}): OperationProjection => ({ ...base, id: 'o1', operation_id: 'o1', status: 'RUNNING',
  deadline: T(30), created_at: T(1), ...over } as OperationProjection);

test('projections from another partition or with bad enumerations are rejected', () => {
  const foreign = { ...utterance(), scope: { ...scope, memory_mode: 'test' } };
  assert.throws(() => utterances(page([foreign]), scope));
  assert.throws(() => utterances(page([utterance({ moderation_status: 'sent' as never })]), scope));
  assert.throws(() => deliveries(page([delivery({ status: 'DONE' as never })]), scope));
  assert.throws(() => briefings(page([{ ...base, id: 'b1', author_id: 'op', type: 'law', content: 'x', status: 'active', expires_at: T(9), created_at: T(1) }]), scope));
  assert.throws(() => inbound(page([source({ availability: 'secret' as never })]), scope));
  assert.throws(() => operations(page([operation({ status: 'DONE' as never })]), scope));
  assert.equal(utterances(page([utterance()]), scope).items.length, 1);
});
test('timeline orders by time, keeps only the newest revision and hides unavailable text', () => {
  const rows = buildTimeline(
    [source(), source({ id: 's2', source_id: 's2', occurred_at: T(5), content: null, availability: 'expired' } as never),
      source({ id: 's3', source_id: 's3', provenance: 'model_proposal', occurred_at: T(4) } as never)],
    [utterance({ revision: 1, content: 'Старая', id: 'u1' }), utterance({ revision: 2, content: 'Новая' })], [delivery({ revision: 2 })]);
  assert.deepEqual(rows.map((row) => row.key), ['in:s1', 'out:u1', 'in:s2']);
  assert.equal(rows[1].text, 'Новая');
  assert.equal(rows[2].text, null);
  assert.equal(rows[2].status, 'removed');
  assert.equal(latestRevisions([utterance({ revision: 3 }), utterance({ revision: 1 })]).length, 1);
});
test('delivery status never hides an unknown or failed channel behind another success', () => {
  const u = utterance({ channel_ids: ['panel', 'voice'] });
  const ok = delivery(); const bad = (status: Delivery['status']) => delivery({ id: 'd2', channel_id: 'voice', status });
  assert.equal(deliveryStatus(u, [ok, bad('DELIVERED')]).status, 'done');
  assert.equal(deliveryStatus(u, [ok, bad('UNKNOWN')]).status, 'unknown');
  assert.equal(deliveryStatus(u, [ok, bad('FAILED')]).status, 'failed');
  assert.equal(deliveryStatus(u, [ok, bad('PARTIAL')]).status, 'failed');
  assert.equal(deliveryStatus(u, [ok, bad('SENDING')]).status, 'generating');
  assert.equal(deliveryStatus(u, []).status, 'saved');
  // a newer attempt supersedes an older one of the same channel only
  assert.equal(deliveryStatus(u, [delivery({ status: 'FAILED', created_at: T(3) }), delivery({ id: 'd3', attempt_id: 'a2', status: 'DELIVERED', created_at: T(4) })]).status, 'done');
  // a delivery of an older revision does not describe the newest text
  assert.equal(deliveryStatus(utterance({ revision: 2 }), [delivery({ revision: 1 })]).status, 'saved');
});
test('moderation states are shown as such, not as delivered', () => {
  assert.equal(deliveryStatus(utterance({ moderation_status: 'blocked' }), [delivery()]).status, 'cancelled');
  assert.equal(deliveryStatus(utterance({ moderation_status: 'draft', moderation_check_id: null }), []).status, 'preview');
  assert.equal(deliveryStatus(utterance({ moderation_status: 'checking', moderation_check_id: null }), []).status, 'preview');
});
test('generating comes only from open owner operations; goal admits what the core does not say', () => {
  assert.equal(generating([operation({ status: 'RUNNING' })]), true);
  assert.equal(generating([operation({ status: 'SUCCEEDED' }), operation({ id: 'o2', status: 'WAITING' })]), false);
  const g = goal(run, [operation({ status: 'WAITING', waiting_reason: 'жду ответа' })]);
  assert.equal(g.headline, 'Агент работает');
  assert.match(g.reasons[0], /не переданы/);
  assert.deepEqual(g.pending, ['o1: жду ответа']);
  assert.equal(goal(undefined, []).headline, 'Состояние запуска недоступно');
});
test('sending is allowed only for a working agent, a non-empty bounded text and no pending command', () => {
  assert.equal(canSend(run, 'Привет', 'idle').ok, true);
  assert.equal(canSend(undefined, 'Привет', 'idle').ok, false);
  assert.equal(canSend({ ...run, state: 'STOPPED' }, 'Привет', 'idle').ok, false);
  assert.equal(canSend({ ...run, state: 'STOPPING' }, 'Привет', 'idle').ok, false);
  assert.equal(canSend(run, '   ', 'idle').ok, false);
  assert.equal(canSend(run, 'я'.repeat(4001), 'idle').ok, false);
  assert.equal(canSend(run, 'Привет', 'sending').ok, false);
  assert.equal(canSend(run, 'Привет', 'unknown').ok, false);
});
test('an unknown outcome keeps the identical command; a clear refusal does not', () => {
  const command = messageCommand(run, 'Привет', 'req-1');
  assert.deepEqual(command.scope, runScope);
  assert.equal(command.target, 'session-1');
  for (const code of ['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT']) assert.deepEqual(afterSend(command, { code }), { phase: 'unknown', command });
  assert.deepEqual(afterSend(command, { code: 'ACCESS_DENIED' }), { phase: 'rejected', code: 'ACCESS_DENIED' });
  const ok: Receipt = { request_id: 'req-1', scope: runScope, status: 'completed' } as Receipt;
  assert.equal(afterSend(command, { receipt: ok }).phase, 'saved');
  assert.equal(afterSend(command, { receipt: { ...ok, status: 'rejected' } }).phase, 'rejected');
});
test('send posts once with CSRF; a lost reply is checked by the same request id and never doubled', async () => {
  const calls: { path: string; body: { request_id: string; arguments: { text: string } } }[] = [];
  let lose = true;
  const api = new PanelApi(fetcher(async (path, init) => {
    const body = JSON.parse(String(init.body));
    calls.push({ path, body });
    if (lose) { lose = false; throw new Error('connection reset'); }
    return json({ request_id: body.request_id, scope: runScope, status: 'completed', resource_id: 'msg-1', version: 1 });
  }));
  api.authorize(auth);
  const command = messageCommand(run, 'Привет', 'req-1');
  const first = await api.command(command).catch((error: unknown) => error);
  assert.ok(first instanceof ApiFailure && first.code === 'NETWORK');
  assert.equal(calls.length, 1); // no automatic retry
  const checked = await api.command(command) as Receipt;
  assert.equal(checked.request_id, 'req-1');
  assert.equal(calls.length, 2);
  assert.equal(calls[0].body.request_id, calls[1].body.request_id);
  assert.equal(calls[0].path, '/api/v1/commands');
  assert.equal(JSON.stringify(calls[0].body), JSON.stringify(calls[1].body));
});
test('a receipt for another request or scope is not accepted as saved', async () => {
  const api = new PanelApi(fetcher(async () => json({ request_id: 'other', scope: runScope, status: 'completed' })));
  api.authorize(auth);
  await assert.rejects(api.command(messageCommand(run, 'Привет', 'req-1')));
  const foreign = new PanelApi(fetcher(async () => json({ request_id: 'req-1', scope: { ...runScope, memory_mode: 'test' }, status: 'completed' })));
  foreign.authorize(auth);
  await assert.rejects(foreign.command(messageCommand(run, 'Привет', 'req-1')));
});
test('dialogue reads use the exact partition and the allowlisted resource names', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.utterances(scope); await api.deliveries(scope); await api.briefings(scope); await api.inbound(scope); await api.operations(scope);
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname),
    ['/api/v1/utterances', '/api/v1/deliveries', '/api/v1/briefings', '/api/v1/sources', '/api/v1/operations']);
  for (const path of paths) assert.equal(new URL(path, 'http://x').searchParams.get('memory_mode'), 'work');
});
test('briefing commands carry type, expiry and version; validation bounds the form', () => {
  const create = createBriefing(runScope, 'session-1', 'scenario', 'Играем в шахматы', T(30), 'req-b');
  assert.equal(create.type, 'briefing.create');
  assert.deepEqual(create.arguments.data, { type: 'scenario', content: 'Играем в шахматы', expires_at: T(30) });
  const row = { ...base, id: 'b1', version: 3, author_id: 'op', type: 'context', content: 'x', status: 'active', expires_at: T(30), created_at: T(1) } as unknown as Briefing;
  const cancel = cancelBriefing(runScope, 'session-1', row, 'req-c');
  assert.equal(cancel.expected_version, 3);
  assert.deepEqual(cancel.arguments.data, { briefing_id: 'b1' });
  const now = Date.parse(T(0));
  assert.equal(validBriefing('x', T(30), now), null);
  assert.match(validBriefing('  ', T(30), now)!, /текст/);
  assert.match(validBriefing('x', '', now)!, /срок/i);
  assert.match(validBriefing('x', T(0), now)!, /будущем/);
  assert.match(validBriefing('x', '2030-01-01T00:00:00Z', now)!, /года/);
  assert.match(validBriefing('я'.repeat(4001), T(30), now)!, /4000/);
});
test('delivery rows of other utterances, older attempts and stale text never leak into a status', () => {
  const u = utterance({ channel_ids: ['panel'] });
  // a failure that belongs to a different utterance does not touch this one
  assert.equal(deliveryStatus(u, [delivery({ utterance_id: 'u-other', status: 'FAILED' })]).status, 'saved');
  // the newest attempt wins whatever the order of rows in the page
  const newer = delivery({ id: 'd9', attempt_id: 'a9', status: 'DELIVERED', created_at: T(9) });
  const older = delivery({ id: 'd8', attempt_id: 'a8', status: 'FAILED', created_at: T(4) });
  assert.equal(deliveryStatus(u, [newer, older]).status, 'done');
  assert.equal(deliveryStatus(u, [older, newer]).status, 'done');
  // text that the owner marks expired is not shown even if a stale copy of it arrived
  const stale = buildTimeline([source({ availability: 'expired', content: 'остаток текста' } as never)], [], []);
  assert.equal(stale[0].text, null);
  assert.equal(stale[0].status, 'removed');
});
test('finished operations are not pending work', () => {
  const done = operation({ id: 'o2', operation_id: 'o2', status: 'SUCCEEDED' });
  assert.deepEqual(goal(run, [done]).pending, []);
  assert.equal(goal(run, [done, operation({ status: 'QUEUED' })]).pending.length, 1);
});
test('an utterance must name at least one and at most 200 channels', () => {
  assert.throws(() => utterances(page([utterance({ channel_ids: [] })]), scope));
  assert.throws(() => utterances(page([utterance({ channel_ids: Array.from({ length: 201 }, (_, n) => 'c' + n) })]), scope));
  assert.throws(() => utterances(page([utterance({ channel_ids: ['ok', 'bad id'] })]), scope));
});
