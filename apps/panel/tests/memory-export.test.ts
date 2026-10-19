import assert from 'node:assert/strict';
import test from 'node:test';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';

const scope = { workspace_id: 'w', persona_id: 'p', memory_space_id: 'm', memory_mode: 'work' as const };
const document = { schema_id: 'memory.source-export', schema_version: '1.0', exported_at: '2026-10-19T00:00:00Z',
  source: { id: 's', source_id: 's', scope, version: 1, created_at: '2026-10-19T00:00:00Z', updated_at: '2026-10-19T00:00:00Z',
    provenance: 'operator_input', source_type: 'fixture', adapter_id: 'fixture', adapter_version: '1',
    occurred_at: '2026-10-19T00:00:00Z', registered_at: '2026-10-19T00:00:00Z', content: 'Вымышленный текст.',
    availability: 'available', retention: { policy_id: 'p', expires_at: null, protected: false },
    read_allowed: true, api_transfer_allowed: true, public_disclosure_allowed: false } };
async function response(body: unknown = document, checksum = true) {
  const text = JSON.stringify(body);
  const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))),
    (b) => b.toString(16).padStart(2, '0')).join('');
  return new Response(text, { headers: { 'Content-Type': 'application/json', ETag: `"${checksum ? hash : '0'.repeat(64)}"` } });
}
test('memory export is one scoped GET and a validated ephemeral JSON blob', async () => {
  const calls: string[] = [];
  const api = new PanelApi(async (url) => { calls.push(String(url)); return response(); });
  const blob = await api.memoryExport(scope, 'owner-op-fixture');
  assert.equal(blob.type, 'application/json');
  assert.deepEqual(JSON.parse(await blob.text()), document);
  assert.equal(calls.length, 1);
  assert.match(calls[0], /^\/api\/v1\/memory\/exports\/owner-op-fixture\/content\?/);
  assert.doesNotMatch(calls[0], /Вымышленный/);
});
test('corrupt, foreign, unavailable or unconfirmed exports never become a download', async () => {
  const cases = [
    () => response(document, false),
    () => response({ ...document, source: { ...document.source, scope: { ...scope, persona_id: 'foreign' } } }),
    () => response({ ...document, source: { ...document.source, availability: 'deleted', content: 'stale' } }),
    async () => new Response(JSON.stringify({ code: 'ACCESS_DENIED' }), { status: 403 }),
  ];
  for (const make of cases) {
    const api = new PanelApi(make);
    await assert.rejects(api.memoryExport(scope, 'owner-op-fixture'), ApiFailure);
  }
});
