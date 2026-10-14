import test from 'node:test';
import assert from 'node:assert/strict';
import { previewSettingsTransfer, applySettingsTransfer, acknowledgeSettingsTransfer } from '../src/shared/owner-commands.ts';
import { settingsTransferPreview, settingsTransferStatus } from '../src/shared/validation.ts';
import type { Scope, SettingsTransferOperation } from '../src/shared/contracts.ts';

const target: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_mode: 'work', memory_space_id: 'work' };
const source: Scope = { ...target, memory_mode: 'test', memory_space_id: 'lab' };
const command = previewSettingsTransfer(target, source, ['gm_intensity'], 7, 'operation-1', 'preview-1');
const plan = {
  id: 'plan-1', version: 1, scope: target, source_scope: source, operator_id: 'operator',
  request_id: 'preview-1', operation_id: 'operation-1', keys: ['gm_intensity'], target_version: 7,
  preview_digest: 'a'.repeat(64), expires_at: '2026-10-15T12:05:00Z', blocked: false,
  items: [{ key: 'gm_intensity', before: 0.5, after: 0.9, changes: [], blockers: [] }], never_transferred: ['episodes'],
};
test('apply binds the reviewed plan, source, selection, digest and owner version', () => {
  const checked = settingsTransferPreview(plan, command);
  const apply = applySettingsTransfer(checked, 'apply-1');
  assert.equal(apply.expected_version, 7);
  assert.deepEqual(apply.arguments.data, { source_scope: source, action: 'apply', operation_id: 'operation-1',
    keys: ['gm_intensity'], preview_digest: plan.preview_digest, plan_id: 'plan-1', plan_version: 1 });
});
test('foreign source, unreviewed selection and concealed blockers fail closed', () => {
  for (const change of [
    { source_scope: { ...source, persona_id: 'other' } }, { keys: ['profile'] },
    { items: [{ ...plan.items[0], blockers: ['PROJECT_RULE_REMOVAL'] }] }, { target_version: 8 },
    { request_id: 'other-request' }, { keys: ['episodes'] },
  ]) assert.throws(() => settingsTransferPreview({ ...plan, ...change }, command));
});
const row = { id: 'progress-1', version: 3, scope: target, source_scope: source, operation_id: 'operation-1',
  operator_id: 'operator', preview_digest: plan.preview_digest, selected_keys: ['gm_intensity'], applied_keys: [],
  status: 'reconciliation_required' } as unknown as SettingsTransferOperation;
test('unknown outcome is distinct from success; reconciliation uses progress CAS', () => {
  assert.equal(settingsTransferStatus({ scope: target, operation_id: 'operation-1', record: null }, target, 'operation-1').record, null);
  const checked = settingsTransferStatus({ scope: target, operation_id: 'operation-1', record: row }, target, 'operation-1');
  assert.equal(acknowledgeSettingsTransfer(checked.record!, 'ack-1').expected_version, 3);
  assert.throws(() => settingsTransferStatus({ scope: target, operation_id: 'operation-1', record: { ...row, status: 'completed' } }, target, 'operation-1'));
  assert.throws(() => settingsTransferStatus({ scope: target, operation_id: 'operation-1', record: { ...row, operation_id: 'other' } }, target, 'operation-1'));
  assert.throws(() => acknowledgeSettingsTransfer({ ...row, operation_id: null }, 'ack-1'));
});
