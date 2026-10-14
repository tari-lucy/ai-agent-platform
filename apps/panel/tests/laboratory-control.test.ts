import test from 'node:test';
import assert from 'node:assert/strict';
import { stopLaboratory, eventLaboratory } from '../src/shared/owner-commands.ts';
import { laboratoryRuns } from '../src/shared/validation.ts';
import type { LaboratoryRun, Scope } from '../src/shared/contracts.ts';
const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_mode: 'work', memory_space_id: 'work', run_id: 'run', agent_id: 'primary' };
const row: LaboratoryRun = { scope, lab_scope: { ...scope, memory_mode: 'test', memory_space_id: 'lab' },
  operation_id: 'job-1', version: 9, scenario_id: 'quiet', scenario_version: 2,
  status: 'RUNNING', ready: true, virtual_time: '2026-10-15T10:00:00Z', accepted_events: 0 };
test('controls bind Job CAS, current run and exact TEST scope rather than a report', () => {
  const cancel = stopLaboratory(row, 'stop-1');
  assert.equal(cancel.expected_version, 9);
  assert.deepEqual(cancel.scope, scope);
  assert.deepEqual(cancel.arguments, { schema_id: 'laboratory.stop-args', data: { operation_id: 'job-1', lab_scope: row.lab_scope } });
  assert.deepEqual(eventLaboratory(row, 'Вымышленное наблюдение', 'event-1').arguments.data,
    { operation_id: 'job-1', lab_scope: row.lab_scope, text: 'Вымышленное наблюдение' });
});
test('projection rejects wrong partition, impossible ready and unbounded events', () => {
  const page = { scope, items: [row], complete: true, next_cursor: null, limit: 50 };
  assert.equal(laboratoryRuns(page, scope).items.length, 1);
  for (const change of [{ lab_scope: scope }, { status: 'CANCELLED' }, { accepted_events: 33 },
    { scope: { ...scope, persona_id: 'other' } }]) assert.throws(() => laboratoryRuns({ ...page, items: [{ ...row, ...change }] }, scope));
});
