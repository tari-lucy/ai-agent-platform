import type {
  SettingsTransferArguments, SettingsTransferPreview, SettingsTransferOperation,
  AcknowledgeCommand, ConfigureGM, EditCommand, GmRuntimeState, LaboratoryScenario,
  LaboratoryStartArguments, PersonaApplyArguments, PersonaValidateArguments,
  ReflectCommand, RunSession, Scope, Utterance,
} from '../../../../packages/contracts/generated/contracts.ts';

export function validateProfile(scope: Scope, version: number, markdown: string, requestId: string): EditCommand {
  const data: PersonaValidateArguments = { markdown };
  return { type: 'persona.validate', request_id: requestId, scope, target: scope.persona_id,
    expected_version: version, arguments: { schema_id: 'persona.validate-args', data } };
}
export function applyProfile(scope: Scope, version: number, markdown: string, digest: string, requestId: string): EditCommand {
  const data: PersonaApplyArguments = { markdown, revision_digest: digest };
  return { type: 'persona.apply', request_id: requestId, scope, target: scope.persona_id,
    expected_version: version, arguments: { schema_id: 'persona.apply-args', data } };
}
export function configureGM(scope: Scope, settings: GmRuntimeState, data: ConfigureGM, requestId: string): EditCommand {
  return { type: 'gm.configure', request_id: requestId, scope, target: settings.id,
    expected_version: settings.version, arguments: { schema_id: 'gm.configure-args', data } };
}
export function startLaboratory(run: RunSession, scenario: LaboratoryScenario, labScope: Scope, seed: string, requestId: string): EditCommand {
  const data: LaboratoryStartArguments = { scenario_id: scenario.scenario_id,
    scenario_version: scenario.version, lab_scope: labScope, seed, mode: 'mock' };
  return { type: 'laboratory.start', request_id: requestId, scope: run.scope, target: 'laboratory',
    expected_version: run.version, arguments: { schema_id: 'laboratory.start-args', data } };
}
export function acknowledgeReply(utterance: Utterance, requestId: string): AcknowledgeCommand {
  return { type: 'dialogue.acknowledge', request_id: requestId, scope: utterance.scope, target: utterance.id,
    expected_version: utterance.version, arguments: { revision: utterance.revision } };
}
export function reflectSession(run: RunSession, episodeIds: string[], requestId: string): ReflectCommand {
  return { type: 'session.reflect', request_id: requestId, scope: run.scope, target: run.session_id,
    expected_version: run.version, arguments: { episode_ids: episodeIds } };
}

export type TransferKey = SettingsTransferArguments['keys'][number];
export function previewSettingsTransfer(target: Scope, source: Scope, keys: TransferKey[], version: number, operationId: string, requestId: string): EditCommand {
  const data: SettingsTransferArguments = { source_scope: source, action: 'preview', operation_id: operationId, keys };
  return { type: 'laboratory.transfer_settings', target: 'laboratory', scope: target, expected_version: version,
    request_id: requestId, arguments: { schema_id: 'laboratory.transfer-settings-args', data } };
}
export function applySettingsTransfer(plan: SettingsTransferPreview, requestId: string): EditCommand {
  const data: SettingsTransferArguments = { source_scope: plan.source_scope, action: 'apply', operation_id: plan.operation_id,
    keys: plan.keys, preview_digest: plan.preview_digest, plan_id: plan.id, plan_version: plan.version };
  return { type: 'laboratory.transfer_settings', target: 'laboratory', scope: plan.scope, expected_version: plan.target_version,
    request_id: requestId, arguments: { schema_id: 'laboratory.transfer-settings-args', data } };
}
export function acknowledgeSettingsTransfer(row: SettingsTransferOperation, requestId: string): EditCommand {
  if (!row.operation_id) throw new Error('Legacy transfer identity unavailable');
  const data: SettingsTransferArguments = { source_scope: row.source_scope, action: 'acknowledge', operation_id: row.operation_id,
    keys: row.selected_keys, preview_digest: row.preview_digest };
  return { type: 'laboratory.transfer_settings', target: 'laboratory', scope: row.scope, expected_version: row.version,
    request_id: requestId, arguments: { schema_id: 'laboratory.transfer-settings-args', data } };
}
