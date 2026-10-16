import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi } from '../src/shared/api.ts';
import { recall } from '../src/shared/validation.ts';
import type { Scope, RecallResult } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'work', memory_mode: 'work' };
const found: RecallResult = { scope, complete: false, index_state: 'degraded', missing_source_ids: [], fragments: [{
  source: { source_id: 'old-source', version: 1, scope, availability: 'available' }, content: 'Вымышленный маяк.', attribution: 'operator_input', conflicts: [],
}] };

test('memory search is a bounded read-only POST; query stays out of URLs and uses explicit transfer purpose', async () => {
  const calls: {path: string; body: unknown; init: RequestInit}[] = [];
  const api = new PanelApi((async (path: string, init: RequestInit) => {
    calls.push({ path, init, body: JSON.parse(String(init.body)) }); return new Response(JSON.stringify(found));
  }) as unknown as typeof fetch);
  api.authorize({ principal_id: 'operator', scopes: [scope], expires_at: '2099-01-01T00:00:00Z', csrf_token: 'c'.repeat(43) });
  assert.equal((await api.recall({ ...scope, agent_id: 'agent', run_id: 'run' }, 'маяк')).fragments.length, 1);
  assert.equal(calls[0].path, '/api/v1/memory/recall');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal((calls[0].init.headers as Record<string, string>)['X-CSRF-Token'], 'c'.repeat(43));
  assert.deepEqual(calls[0].body, { scope, query: 'маяк', limit: 12, context_token_limit: 2000, purpose: 'api_transfer' });
  await assert.rejects(api.recall(scope, ' ')); await assert.rejects(api.recall(scope, 'x'.repeat(1001)));
  assert.equal(calls.length, 1);
});

test('recall rejects foreign scope, unavailable references, excessive fragments and hidden diagnostic ids', () => {
  assert.equal(recall(found, scope).index_state, 'degraded');
  for (const value of [
    { ...found, scope: { ...scope, persona_id: 'foreign' } },
    { ...found, missing_source_ids: ['hidden-source'] },
    { ...found, fragments: Array(13).fill(found.fragments[0]) },
    { ...found, fragments: [{ ...found.fragments[0], source: { ...found.fragments[0].source, availability: 'deleted' } }] },
    { ...found, fragments: [{ ...found.fragments[0], source: { ...found.fragments[0].source, scope: { ...scope, memory_mode: 'test' } } }] },
  ]) assert.throws(() => recall(value, scope));
});
