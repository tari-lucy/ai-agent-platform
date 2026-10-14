import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi, ApiFailure } from '../src/shared/api.ts';
import type { LaboratoryReport, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'lab', memory_mode: 'test' };
async function fixture(extra = {}) {
  const body = JSON.stringify({ scenario_id: 'synthetic', scenario_version: 1, mock: true, log: [], ...extra });
  const bytes = new TextEncoder().encode(body);
  const checksum = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), (b) => b.toString(16).padStart(2, '0')).join('');
  const report: LaboratoryReport = { id: 'report-1', scope, version: 1, schema_version: '1.0',
    created_at: '2026-10-15T00:00:00Z', updated_at: '2026-10-15T00:00:00Z',
    scenario_id: 'synthetic', scenario_version: 1, operation_id: 'op-1', status: 'passed', summary: 'Synthetic',
    report_artifact: { artifact_id: 'artifact-1', version: 1, scope, checksum, size_bytes: bytes.length, media_type: 'application/json' } };
  const headers = { 'Content-Type': 'application/json', ETag: `"${checksum}"` };
  return { body, report, headers };
}

test('download returns exact verified JSON blob, including reports above the projection limit', async () => {
  const f = await fixture({ detail: 'x'.repeat(1050000) });
  const api = new PanelApi((async (path, options) => {
    assert.equal(path, '/api/v1/laboratory-reports/report-1/content?workspace_id=synthetic&persona_id=alpha&memory_space_id=lab&memory_mode=test');
    assert.equal(options?.credentials, 'same-origin');
    assert.equal(options?.cache, 'no-store');
    assert.equal(options?.redirect, 'error');
    return new Response(f.body, { headers: f.headers });
  }) as typeof fetch);
  const blob = await api.laboratoryReportContent(scope, f.report);
  assert.equal(blob.type, 'application/json');
  assert.equal(await blob.text(), f.body);
});

test('invalid refs and foreign partitions are rejected before IO', async () => {
  const f = await fixture(); let calls = 0;
  const api = new PanelApi((async () => { calls++; return new Response(); }) as typeof fetch);
  for (const change of [
    { size_bytes: 2000001 }, { size_bytes: 0 }, { checksum: 'bad' }, { version: 0 },
    { media_type: 'text/html' }, { scope: { ...scope, memory_space_id: 'foreign' } },
  ]) {
    await assert.rejects(() => api.laboratoryReportContent(scope, {
      ...f.report, report_artifact: { ...f.report.report_artifact!, ...change },
    }));
  }
  await assert.rejects(() => api.laboratoryReportContent({ ...scope, memory_space_id: 'foreign' }, f.report));
  assert.equal(calls, 0);
});

test('truncation, overflow, changed bytes, wrong media and mismatched ETag fail closed', async () => {
  const f = await fixture();
  for (const [body, headers] of [
    [f.body.slice(1), f.headers], [f.body + ' ', f.headers], [' ' + f.body.slice(1), f.headers],
    [f.body, { ...f.headers, 'Content-Type': 'text/html' }],
    [f.body, { ...f.headers, ETag: '"wrong"' }],
  ] as const) {
    const api = new PanelApi((async () => new Response(body, { headers })) as typeof fetch);
    await assert.rejects(() => api.laboratoryReportContent(scope, f.report), ApiFailure);
  }
});

test('even checksum-valid content must match the selected mock scenario', async () => {
  for (const extra of [{ mock: false }, { scenario_id: 'foreign' }, { scenario_version: 2 }]) {
    const f = await fixture(extra);
    const api = new PanelApi((async () => new Response(f.body, { headers: f.headers })) as typeof fetch);
    await assert.rejects(() => api.laboratoryReportContent(scope, f.report), ApiFailure);
  }
});

test('clearing auth during body IO discards the pending download without retry', async () => {
  const f = await fixture(); let calls = 0;
  const api = new PanelApi((async () => {
    calls++;
    return new Response(new ReadableStream({
      pull(controller) { api.clear(); controller.enqueue(new TextEncoder().encode(f.body)); controller.close(); },
    }), { headers: f.headers });
  }) as typeof fetch);
  await assert.rejects(() => api.laboratoryReportContent(scope, f.report), ApiFailure);
  assert.equal(calls, 1);
});

test('server refusal stays a safe API error and is never offered as report bytes', async () => {
  const f = await fixture();
  const api = new PanelApi((async () => new Response(JSON.stringify({ code: 'ACCESS_DENIED' }), { status: 403 })) as typeof fetch);
  await assert.rejects(() => api.laboratoryReportContent(scope, f.report),
    (error: unknown) => error instanceof ApiFailure && error.code === 'ACCESS_DENIED' && error.status === 403);
});
