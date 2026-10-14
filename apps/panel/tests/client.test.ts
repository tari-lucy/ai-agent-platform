import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import { scopeKey, permits } from '../src/shared/scope.ts';
import { mergeRuns, stopTarget, stopObservation } from '../src/shared/projections.ts';
import { createProjectionCache } from '../src/shared/cache.ts';
import type { OperatorSession, RunSession, RunSessionPage, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const auth: OperatorSession = { principal_id: 'operator', scopes: [scope], csrf_token: 'c'.repeat(43), expires_at: '2099-01-01T00:00:00Z' };
const run: RunSession = { id: 'run-1', scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1', state: 'ACTIVE', activity: 'OPEN', autonomy: 'observation', gm_paused: true, voice_enabled: false, control_epoch: 2, version: 4, created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z' };
const page = (items = [run]): RunSessionPage => ({ items, limit: 50, next_cursor: null, complete: true });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) =>
  callback as unknown as typeof fetch;
test('read uses the four exact partition fields, same-origin cookie and abort signal', async () => {
  let call: [string, RequestInit] | undefined;
  const api = new PanelApi(fetcher(async (path, init) => { call = [path, init]; return json(page()); }));
  assert.equal((await api.runs(scope)).items[0].run_id, 'run-1');
  const [path, init] = call!;
  const url = new URL(path, 'http://127.0.0.1:1');
  assert.equal(url.searchParams.get('persona_id'), 'alpha');
  assert.equal(url.searchParams.size, 5);
  assert.equal(init.credentials, 'same-origin');
  assert.equal(init.redirect, 'error');
  assert.equal(init.cache, 'no-store');
  assert.equal(init.method, 'GET');
});
test('login does not persist password or put credentials in URLs; STOP carries only RAM nonce', async () => {
  const calls: [string, RequestInit][] = [];
  const api = new PanelApi(fetcher(async (path, init) => {
    calls.push([path, init]);
    if (path.includes('/login')) return json(auth);
    const body = JSON.parse(String(init.body));
    return json({ scope: { ...scope, agent_id: 'primary' }, request_id: body.request_id, state: 'STOPPING', status: 'waiting',
      control_epoch: 3, run_id: 'run-1', unconfirmed_resource_ids: ['worker'] });
  }));
  api.authorize(await api.login('synthetic-password'));
  const receipt = await api.stop(scope, 'primary', 'stop-request');
  assert.equal(receipt.state, 'STOPPING');
  assert.equal(receipt.unconfirmed_resource_ids?.[0], 'worker');
  assert.equal(calls[0][0], '/api/v1/auth/login');
  assert.deepEqual(JSON.parse(String(calls[0][1].body)), { password: 'synthetic-password' });
  assert.deepEqual(JSON.parse(String(calls[1][1].body)), {
    type: 'agent.stop', request_id: 'stop-request', scope: { ...scope, agent_id: 'primary' }, target: 'primary',
  });
  assert.equal((calls[1][1].headers as Record<string, string>)['X-CSRF-Token'], auth.csrf_token);
  assert.equal(JSON.stringify(api).includes('synthetic-password'), false);
  assert.equal(JSON.stringify(api).includes(auth.csrf_token), false);
});
test('foreign page is rejected before entering projection cache', async () => {
  const api = new PanelApi(fetcher(async () => json(page([{ ...run, scope: { ...scope, memory_mode: 'test' } }]))));
  await assert.rejects(api.runs(scope));
});
test('STOP validates request identity and scope instead of claiming false success', async () => {
  const api = new PanelApi(fetcher(async () => json({ request_id: 'someone-else', scope, state: 'STOPPED' })));
  await assert.rejects(api.stop(scope, 'primary', 'own-request'));
});
test('raw errors and unrecognised codes never reach user-facing exceptions', async () => {
  const api = new PanelApi(fetcher(async () => json({ code: 'SERVICE_UNAVAILABLE', message: 'private-body', password: 'private-password' }, 503)));
  await assert.rejects(api.runs(scope), (error: unknown) =>
    error instanceof ApiFailure && !error.message.includes('private') && error.code === 'SERVICE_UNAVAILABLE');
});
test('mutations never retry an unknown network result', async () => {
  let calls = 0;
  const api = new PanelApi(fetcher(async () => { calls++; throw new Error('private provider details'); }));
  await assert.rejects(api.stop(scope, 'primary'));
  assert.equal(calls, 1);
});
test('401 revokes RAM credentials and notifies authenticated screen', async () => {
  let expired = 0;
  const api = new PanelApi(fetcher(async () => json({}, 401)), () => expired++);
  api.authorize(auth);
  await assert.rejects(api.runs(scope));
  assert.equal(api.authenticated, false); assert.equal(expired, 1);
});
test('late successful and late 401 responses from old session do not affect a new session', async () => {
  for (const status of [200, 401]) {
    let release: ((response: Response) => void) | undefined; let expired = 0;
    const api = new PanelApi(fetcher(() => new Promise((resolve) => { release = resolve; })), () => expired++);
    api.authorize(auth); const pending = api.runs(scope);
    api.authorize({ ...auth, csrf_token: 'n'.repeat(43) });
    release!(json(page(), status));
    await assert.rejects(pending);
    assert.equal(expired, 0); assert.equal(api.authenticated, true); api.clear();
  }
});
test('clear aborts in-flight reads; STOP can finish while ordinary read is blocked', async () => {
  let signal: AbortSignal | undefined;
  const api = new PanelApi(fetcher(async (path, init) => {
    if (path.includes('/sessions?')) {
      signal = init.signal!;
      return new Promise((_, reject) => signal!.addEventListener('abort', () => reject(new Error('aborted'))));
    }
    return json({ scope: { ...scope, agent_id: 'primary' }, request_id: 'stop-now', run_id: 'run-1', control_epoch: 3,
      state: 'STOPPING', status: 'waiting' });
  }));
  api.authorize(auth);
  const pending = api.runs(scope);
  assert.equal((await api.stop(scope, 'primary', 'stop-now')).state, 'STOPPING');
  api.clear();
  assert.equal(signal!.aborted, true); await assert.rejects(pending);
});
test('projection responses have a finite byte limit', async () => {
  const api = new PanelApi(fetcher(async () => new Response('x'.repeat(1048577))));
  await assert.rejects(api.runs(scope), ApiFailure);
});
test('partition keys distinguish workspace, persona, memory and test; optional run filter is checked', () => {
  for (const changed of [{ workspace_id: 'other' }, { persona_id: 'beta' }, { memory_space_id: 'other' }, { memory_mode: 'test' as const }]) {
    assert.notEqual(scopeKey(scope), scopeKey({ ...scope, ...changed }));
    assert.equal(permits(scope, { ...scope, ...changed }), false);
  }
  assert.equal(permits({ ...scope, run_id: 'new' }, { ...scope, run_id: 'old' }), false);
});
test('late read cannot overwrite newer version; new run is independent', () => {
  assert.equal(mergeRuns(page(), page([{ ...run, version: 3 }])).items[0].version, 4);
  assert.equal(mergeRuns(page(), page([{ ...run, id: 'new', run_id: 'new', version: 1 }])).items[0].version, 1);
});
test('STOP chooses active owner rather than viewed profile and refuses ambiguous owners', () => {
  const other = { ...run, id: 'other', run_id: 'other', scope: { ...scope, persona_id: 'beta' }, state: 'STOPPED' as const };
  assert.equal(stopTarget([other, run])?.agent_id, 'primary');
  assert.equal(stopTarget([{ ...run, state: 'STOPPED' }])?.id, run.id);
  assert.equal(stopTarget([run, { ...other, state: 'ACTIVE' }]), null);
});
test('query cache isolates partition; no automatic retries/refetches and logout clears data', () => {
  const cache = createProjectionCache();
  cache.setQueryData(['projection', scopeKey(scope), 'sessions'], page());
  assert.equal(cache.getQueryData(['projection', scopeKey({ ...scope, memory_mode: 'test' }), 'sessions']), undefined);
  assert.equal(cache.getDefaultOptions().queries?.retry, false);
  assert.equal(cache.getDefaultOptions().mutations?.retry, false);
  cache.clear(); assert.equal(cache.getQueryCache().getAll().length, 0);
});

test('later owner confirmation replaces STOPPING while a different run cannot inherit it', () => {
  const receipt = { scope, request_id: 'stop', run_id: run.run_id, control_epoch: 2,
    state: 'STOPPING' as const, status: 'waiting' as const, version: 3, unconfirmed_resource_ids: ['worker'] };
  const current = { ...run, state: 'STOPPED' as const, unconfirmed_resource_ids: [] };
  assert.equal(stopObservation(receipt, current)?.state, 'STOPPED');
  assert.deepEqual(stopObservation(receipt, current)?.unconfirmed_resource_ids, []);
  assert.equal(stopObservation(receipt, { ...current, run_id: 'new-run' }), null);
});
test('a malformed receipt cannot report STOPPED while resources remain unconfirmed', async () => {
  const api = new PanelApi(fetcher(async () => json({ scope: { ...scope, agent_id: 'primary' },
    request_id: 'stop-request', run_id: 'run-1', control_epoch: 3, state: 'STOPPED',
    status: 'waiting', unconfirmed_resource_ids: ['worker'] })));
  await assert.rejects(api.stop(scope, 'primary', 'stop-request'));
});
test('general command port rejects a receipt for another request instead of casting it', async () => {
  const api = new PanelApi(fetcher(async () => json({ scope, request_id: 'foreign', status: 'completed' })));
  await assert.rejects(api.command({ type: 'agent.pause', scope, target: 'primary',
    request_id: 'own', expected_version: 1, arguments: {} }));
});
