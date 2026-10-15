import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { memoryCycles, privateApiStatus, briefings, deliveries, inbound, operations, utterances } from '../src/shared/validation.ts';
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


test('private API counter rejects foreign scope, inconsistent totals and missing reset', () => {
  const status = { scope, enabled: true, period: 'utc_day', attempt_limit: 300,
    attempts_used: 7, attempts_remaining: 293, resets_at: '2026-10-15T00:00:00Z', checked_at: T(0) };
  assert.equal(privateApiStatus(status, scope).attempts_remaining, 293);
  for (const changed of [ { attempts_remaining: 94 }, { scope: { ...scope, memory_space_id: 'foreign' } },
    { resets_at: null }, { attempt_limit: 0 }, { attempts_used: -1 }, { attempts_used: 0.5 } ]) {
    assert.throws(() => privateApiStatus({ ...status, ...changed }, scope));
  }
});


test('memory reserve belongs to the total and malformed cycle progress is rejected', () => {
  const status = {scope, enabled:true, period:'utc_day',attempt_limit:300,attempts_used:7,attempts_remaining:293,
    checked_at:T(0),resets_at:'2026-10-15T00:00:00Z',memory_processing:true,memory_daily_calls:8,memory_calls_used:0,dialogue_remaining:285};
  assert.equal(privateApiStatus(status,scope).dialogue_remaining,285);
  assert.throws(() => privateApiStatus({...status,dialogue_remaining:293},scope));
  assert.throws(() => privateApiStatus({...status,memory_calls_used:8},scope));
  const cycle = {id:'cycle-a',scope,version:1,created_at:T(0),updated_at:T(0),episode_ids:['episode-a'],source_ids:['source-a'],input_digest:'a'.repeat(64),operation_id:'job-a',stage:'done',status:'completed',record_ids:['summary-a'],error_code:null};
  const page = {items:[cycle],limit:50,next_cursor:null,complete:true};
  assert.equal(memoryCycles(page,scope).items[0].status,'completed');
  assert.throws(() => memoryCycles({...page,items:[{...cycle,status:'running'}]},scope));
});

// ---- daily use: pagination, scroll, message path, acknowledgement ------------------------------
import { historyNote, PAGE_BUDGET_MAX, walk } from '../src/ui/dialogue/history.ts';
import { atBottom, plan, unseen } from '../src/ui/dialogue/scroll.ts';
import { mayAcknowledge, messageProgress, type AckFacts } from '../src/ui/dialogue/model.ts';

test('the five dialogue reads pass an optional cursor to the existing reader and validate it', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.utterances(scope, undefined, 'c1'); await api.deliveries(scope, undefined, 'c2'); await api.briefings(scope, undefined, 'c3');
  await api.inbound(scope, undefined, 'c4'); await api.operations(scope, undefined, 'c5');
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').searchParams.get('cursor')), ['c1', 'c2', 'c3', 'c4', 'c5']);
  await assert.rejects(api.inbound(scope, undefined, '../x'));
});
const rec = (id: string) => ({ id });
const pages = (...chunks: string[][]) => async (cursor?: string) => {
  const index = cursor === undefined ? 0 : Number(cursor);
  return { items: chunks[index].map(rec), next_cursor: index + 1 < chunks.length ? String(index + 1) : null, complete: index + 1 >= chunks.length };
};
test('walk joins pages without duplicates, in server order, and says when history is whole', async () => {
  const result = await walk(pages(['z', 'a'], ['a', 'm'], ['b']), 4);
  assert.deepEqual(result.items.map((row) => row.id), ['z', 'a', 'm', 'b']);
  assert.equal(result.complete, true); assert.equal(result.pages, 3); assert.equal(result.error, null);
});
test('walk stops at its budget and reports unfinished history, never the end', async () => {
  const result = await walk(pages(['a'], ['b'], ['c'], ['d']), 2);
  assert.equal(result.items.length, 2); assert.equal(result.complete, false); assert.equal(result.error, null);
  assert.equal(historyNote([result], 2).tone, 'partial');
  const huge = await walk(pages(...Array.from({ length: 60 }, (_, i) => [`r${i}`])), 1000);
  assert.equal(huge.pages, PAGE_BUDGET_MAX);
});
test('a failed later page is shown as an error, a failed first page rejects, a stuck cursor cannot loop', async () => {
  let call = 0;
  const flaky = async (): Promise<{ items: { id: string }[]; next_cursor: string | null; complete: boolean }> => {
    call += 1;
    if (call === 2) throw new ApiFailure('NETWORK');
    return { items: [rec('a')], next_cursor: 'n', complete: false };
  };
  const partial = await walk(flaky, 4);
  assert.equal(partial.error, 'NETWORK'); assert.equal(partial.complete, false); assert.equal(partial.items.length, 1);
  assert.equal(historyNote([partial], 4).tone, 'error');
  await assert.rejects(walk(async () => { throw new ApiFailure('NETWORK'); }, 4));
  const stuck = await walk(async () => ({ items: [rec('a')], next_cursor: 'same', complete: false }), 10);
  assert.equal(stuck.error, 'NETWORK'); assert.equal(stuck.pages, 2);
});
test('time order is rebuilt from timestamps, so shuffled ids do not reorder the conversation', () => {
  const rows = [source({ id: 'zzz', source_id: 'zzz', occurred_at: T(1) } as never), source({ id: 'aaa', source_id: 'aaa', occurred_at: T(9) } as never),
    source({ id: 'mmm', source_id: 'mmm', occurred_at: T(5) } as never)];
  assert.deepEqual(buildTimeline(rows, [], []).map((row) => row.key), ['in:zzz', 'in:mmm', 'in:aaa']);
});
test('scroll rules: follow only at the end, otherwise hold the anchor and count what is new', () => {
  assert.equal(atBottom({ top: 952, height: 1500, client: 500 }), true);
  assert.equal(atBottom({ top: 400, height: 1500, client: 500 }), false);
  assert.deepEqual(plan(true, 10, 80), { kind: 'follow' });
  assert.deepEqual(plan(false, 10, 80), { kind: 'anchor', offset: 70 });
  assert.deepEqual(plan(false, null, 80), { kind: 'stay' });
  assert.equal(unseen(['a', 'b', 'c', 'd'], 'b'), 2);
  assert.equal(unseen(['a', 'b'], 'b'), 0);
  assert.equal(unseen(['a', 'b'], null), 0);
});
test('message path is derived from core data and an absent projection is not a failure', () => {
  const receipt = { request_id: 'r', scope: runScope, status: 'accepted', operation_id: 'o1' } as Receipt;
  assert.equal(messageProgress(receipt, [], []).stage, 'accepted');
  assert.equal(messageProgress(receipt, [operation({ status: 'QUEUED' })], []).stage, 'accepted');
  assert.equal(messageProgress(receipt, [operation({ status: 'RUNNING' })], []).stage, 'processing');
  assert.equal(messageProgress(receipt, [operation({ status: 'FAILED' })], []).stage, 'failed');
  assert.equal(messageProgress(receipt, [operation({ status: 'CANCELLED' })], []).stage, 'cancelled');
  assert.equal(messageProgress(receipt, [operation({ status: 'OUTCOME_UNKNOWN' })], []).stage, 'unknown');
  assert.equal(messageProgress(receipt, [operation({ status: 'RUNNING' })], [utterance({ operation_id: 'o1' } as never)]).stage, 'ready');
  assert.equal(messageProgress({ ...receipt, operation_id: null }, [operation()], []).stage, 'accepted');
});
test('acknowledge only a final exact newest revision that is really shown, in a visible work tab', () => {
  const row = utterance({ audience_id: 'operator-panel', channel_ids: ['operator-panel'], moderation_status: 'approved', scope: runScope } as never);
  const ok: AckFacts = { visibility: 'visible', ratio: 1, mode: 'work', runActive: true, runOpen: true, sameRun: true, entryKey: 'out:u1', row,
    shownText: 'Ответ', newestRevision: true, delivered: false, streaming: false, readsHealthy: true };
  assert.equal(mayAcknowledge(ok), true);
  for (const bad of [{ visibility: 'hidden' }, { ratio: 0.3 }, { mode: 'test' }, { runActive: false }, { runOpen: false }, { sameRun: false },
    { entryKey: 'stream' }, { shownText: 'Другой' }, { newestRevision: false }, { delivered: true }, { readsHealthy: false }, { row: undefined },
    { row: { ...row, channel_ids: ['operator-panel', 'voice'] } }, { row: { ...row, moderation_status: 'blocked' } }] as Partial<AckFacts>[]) {
    assert.equal(mayAcknowledge({ ...ok, ...bad }), false, JSON.stringify(Object.keys(bad)));
  }
});
