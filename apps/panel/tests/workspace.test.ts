import test from 'node:test';
import assert from 'node:assert/strict';
import { PanelApi } from '../src/shared/api.ts';
import { capabilities, connections, permissions, workspaces } from '../src/shared/validation.ts';
import {
  afterControl, backupIdPattern, canCancel, cancelCommand, controlAvailability, controlCommand, degradation, indicators, isExpansion,
  needsReconciliation, orderOperations, overdue, permissionCommand, permissionState, permissionWarning, recoveryExecute, recoveryPreview,
  recoveryProblem, restoreBlocked, runNotice,
} from '../src/ui/workspace/model.ts';
import type { Capability, Connection, ConsequencePlan, OperationProjection, Permission, Receipt, RunSession, Scope } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-14T00:${String(minute).padStart(2, '0')}:00Z`;
const NOW = Date.parse(T(30));
const base = { schema_version: '1.0', scope, created_at: T(1), updated_at: T(1), version: 1 };
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200 });
const fetcher = (callback: (path: string, init: RequestInit) => Promise<Response>) => callback as unknown as typeof fetch;
const run = (over: object = {}): RunSession => ({ ...base, id: 'run-1', scope: runScope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1', state: 'ACTIVE',
  activity: 'OPEN', autonomy: 'observation', gm_paused: true, voice_enabled: false, control_epoch: 2, version: 4, ...over } as unknown as RunSession);
const operation = (over: object = {}): OperationProjection => ({ ...base, id: 'o1', operation_id: 'o1', status: 'RUNNING', deadline: T(40), ...over } as unknown as OperationProjection);
const capability = (over: object = {}): Capability => ({ ...base, id: 'cap-1', adapter_id: 'chat', adapter_version: '1', input_schema_id: 'in', output_schema_id: 'out',
  effect: 'local_write', required_permissions: [], resource_ids: [], timeout_ms: 1000, cancellation_supported: true, idempotency: 'supported', health: 'ready',
  setting_references: [], ...over } as unknown as Capability);
const connection = (over: object = {}): Connection => ({ ...base, id: 'conn-1', name: 'Провайдер', adapter_id: 'openai', adapter_version: '1', endpoint_setting: 'AI_PLATFORM_URL',
  credential_setting: 'AI_PLATFORM_KEY', configured: true, verification: 'verified', capabilities: [], ...over } as unknown as Connection);
const permission = (over: object = {}): Permission => ({ ...base, id: 'perm-1', version: 3, principal_id: 'agent', capability_id: 'cap-1', operation_type: 'speak', binding_id: 'b1',
  level: 'operator_decision', conditions: { schema_id: 'c', data: {} }, limits: { schema_id: 'l', data: {} }, policy_version: 1, author_id: 'operator',
  expires_at: T(50), revoked_at: null, ...over } as unknown as Permission);
const plan = (over: object = {}): ConsequencePlan => ({ ...base, id: 'plan-1', operation_type: 'workspace.restore', consequences: 'Текущее состояние будет заменено.',
  dependencies: [{ resource_id: 'bk-1', version: 1 }], expires_at: T(50), ...over } as unknown as ConsequencePlan);

test('independent indicators never merge run, activity, voice and GM into one word', () => {
  const items = indicators(run());
  assert.deepEqual(items.map((item) => item.label), ['Запуск', 'Деятельность', 'Режим', 'Голос', 'Гейммастер']);
  assert.equal(items[0].value, 'Работает');
  assert.equal(items[3].value, 'Не подключён / выключен');
  assert.equal(items[4].value, 'На паузе');
  assert.equal(indicators(run({ gm_paused: false }))[4].value, 'Пауза снята по данным ядра');
  assert.equal(indicators(run({ state: 'FAULTED' }))[0].tone, 'warn');
  assert.equal(indicators(run({ state: 'STOPPED' }))[0].tone, 'off');
  assert.equal(indicators(run({ activity: 'PAUSED' }))[1].tone, 'wait');
});
test('STOPPING and FAULTED are not presented as stopped; a stopped agent needs an explicit start', () => {
  assert.match(runNotice(run({ state: 'STOPPING' }))!, /не STOPPED/);
  assert.match(runNotice(run({ state: 'FAULTED' }))!, /закрыт до сверки/);
  assert.match(runNotice(run({ state: 'STOPPED' }))!, /отдельного действия/);
  assert.match(runNotice(run({ state: 'STARTING' }))!, /готовности/);
  assert.equal(runNotice(run()), null);
});
test('control availability follows the state; transitions and faults close every control', () => {
  const on = (state: string, activity = 'OPEN') => (['agent.start', 'agent.pause', 'agent.resume', 'session.end'] as const).map((kind) => controlAvailability(run({ state, activity }), kind).enabled);
  assert.deepEqual(on('STOPPED'), [true, false, false, false]);
  assert.deepEqual(on('ACTIVE'), [false, true, false, true]);
  assert.deepEqual(on('ACTIVE', 'PAUSED'), [false, false, true, true]);
  assert.deepEqual(on('ACTIVE', 'PAUSING'), [false, false, false, true]);
  assert.deepEqual(on('STARTING'), [false, false, false, false]);
  assert.deepEqual(on('STOPPING'), [false, false, false, false]);
  assert.deepEqual(on('FAULTED'), [false, false, false, false]);
  assert.equal(controlAvailability(undefined, 'agent.start').enabled, false);
});
test('control commands carry the version seen; autonomy only on start; session end targets the session', () => {
  const start = controlCommand(run({ state: 'STOPPED' }), 'agent.start', 'autonomous', 'req-1');
  assert.deepEqual([start.type, start.expected_version, start.target, start.arguments], ['agent.start', 4, 'primary', { autonomy: 'autonomous' }]);
  assert.deepEqual(controlCommand(run(), 'agent.pause', 'autonomous', 'req-2').arguments, {});
  assert.equal(controlCommand(run(), 'session.end', null, 'req-3').target, 'session-1');
  assert.deepEqual(start.scope, runScope);
});
test('an unknown control outcome keeps the identical command; a refusal does not', () => {
  const command = controlCommand(run(), 'agent.pause', null, 'req-1');
  for (const code of ['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT']) assert.deepEqual(afterControl(command, { code }), { phase: 'unknown', command });
  assert.deepEqual(afterControl(command, { code: 'VERSION_CONFLICT' }), { phase: 'refused', code: 'VERSION_CONFLICT' });
  const receipt = { request_id: 'req-1', scope: runScope, status: 'accepted' } as Receipt;
  assert.equal(afterControl(command, { receipt }).phase, 'accepted');
  assert.equal(afterControl(command, { receipt: { ...receipt, status: 'rejected' } }).phase, 'refused');
});
test('operations: doubtful first, then open by deadline, finished last; only open ones can be cancelled', () => {
  const rows = [operation({ id: 'a', operation_id: 'a', status: 'SUCCEEDED', deadline: T(1) }), operation({ id: 'b', operation_id: 'b', status: 'RUNNING', deadline: T(20) }),
    operation({ id: 'c', operation_id: 'c', status: 'OUTCOME_UNKNOWN', deadline: T(50) }), operation({ id: 'd', operation_id: 'd', status: 'QUEUED', deadline: T(10) })];
  assert.deepEqual(orderOperations(rows).map((row) => row.operation_id), ['c', 'd', 'b', 'a']);
  for (const status of ['PROPOSED', 'AWAITING_APPROVAL', 'READY', 'QUEUED', 'RUNNING', 'WAITING']) assert.equal(canCancel(operation({ status })), true);
  for (const status of ['SUCCEEDED', 'REJECTED', 'FAILED', 'CANCELLED', 'EXPIRED', 'OUTCOME_UNKNOWN']) assert.equal(canCancel(operation({ status })), false);
  assert.equal(needsReconciliation(operation({ status: 'OUTCOME_UNKNOWN' })), true);
  assert.equal(needsReconciliation(operation({ status: 'FAILED' })), false);
  assert.equal(overdue(operation({ deadline: T(10) }), NOW), true);
  assert.equal(overdue(operation({ deadline: T(10), status: 'SUCCEEDED' }), NOW), false);
  assert.equal(overdue(operation({ deadline: T(40) }), NOW), false);
  const cancel = cancelCommand(runScope, 'session-1', operation({ version: 7 }), 'req-c');
  assert.deepEqual([cancel.type, cancel.expected_version, cancel.arguments.data], ['operation.cancel', 7, { operation_id: 'o1' }]);
});
test('degradation names every unhealthy capability and every unconfigured or unverified connection', () => {
  assert.deepEqual(degradation([capability()], [connection()]), []);
  const reasons = degradation([capability({ health: 'degraded' }), capability({ id: 'cap-2', health: 'unavailable' })],
    [connection({ configured: false }), connection({ id: 'c2', name: 'Второй', verification: 'failed' }), connection({ id: 'c3', name: 'Третий', verification: 'unverified' })]);
  assert.equal(reasons.length, 5);
  assert.match(reasons[0], /cap-1.*деградирует/);
  assert.match(reasons[1], /cap-2.*недоступна/);
  assert.match(reasons[2], /Провайдер.*не настроено/);
  assert.match(reasons[3], /Второй.*проверка не прошла/);
  assert.match(reasons[4], /Третий.*не проверено/);
});
test('widening rights is detected, spelled out for external effects, and bound to the version seen', () => {
  assert.equal(isExpansion('denied', 'prepare_only'), true);
  assert.equal(isExpansion('operator_decision', 'automatic'), true);
  assert.equal(isExpansion('automatic', 'operator_decision'), false);
  assert.equal(isExpansion('prepare_only', 'prepare_only'), false);
  assert.equal(permissionWarning(permission({ level: 'denied' }), 'denied', capability()), null);
  assert.equal(permissionWarning(permission({ level: 'automatic' }), 'denied', capability()), null);
  assert.match(permissionWarning(permission(), 'automatic', capability())!, /расширяете права/);
  assert.match(permissionWarning(permission(), 'automatic', capability({ effect: 'public_output' }))!, /без вашего решения/);
  assert.match(permissionWarning(permission(), 'automatic', capability({ effect: 'external_effect' }))!, /без вашего решения/);
  assert.doesNotMatch(permissionWarning(permission(), 'automatic', capability({ effect: 'none' }))!, /без вашего решения/);
  assert.doesNotMatch(permissionWarning(permission({ level: 'denied' }), 'operator_decision', capability({ effect: 'public_output' }))!, /без вашего решения/);
  const cmd = permissionCommand(runScope, 'session-1', permission(), 'prepare_only', 'req-p');
  assert.deepEqual([cmd.type, cmd.expected_version, cmd.arguments.data], ['permission.edit', 3, { permission_id: 'perm-1', level: 'prepare_only' }]);
});
test('permission state distinguishes revoked, expired and active', () => {
  assert.equal(permissionState(permission(), NOW), 'active');
  assert.equal(permissionState(permission({ expires_at: T(10) }), NOW), 'expired');
  assert.equal(permissionState(permission({ revoked_at: T(5) }), NOW), 'revoked');
  assert.equal(permissionState(permission({ revoked_at: T(5), expires_at: T(10) }), NOW), 'revoked');
});
test('restore is closed unless the agent is stopped and a valid copy id is named', () => {
  assert.match(restoreBlocked(undefined, 'bk-1')!, /остановлен/);
  for (const state of ['ACTIVE', 'STARTING', 'STOPPING', 'FAULTED']) assert.match(restoreBlocked(run({ state }), 'bk-1')!, /только при остановленном/);
  assert.match(restoreBlocked(run({ state: 'STOPPED' }), '')!, /идентификатор/i);
  assert.match(restoreBlocked(run({ state: 'STOPPED' }), '../x')!, /идентификатор/i);
  assert.equal(restoreBlocked(run({ state: 'STOPPED' }), 'bk-20261014T120000-daily'), null);
  assert.equal(backupIdPattern.test('bk-1'), true);
  assert.equal(backupIdPattern.test('a b'), false);
  assert.equal(backupIdPattern.test('a'.repeat(129)), false);
});
test('recovery commands are bound to the plan and the copy; a plan for another copy or an expired one is refused', () => {
  const preview = recoveryPreview(runScope, 'session-1', 'restore', 'bk-1', 5, 'req-r');
  assert.deepEqual([preview.type, preview.expected_version, preview.arguments.data], ['workspace.restore.preview', 5, { backup_id: 'bk-1' }]);
  const backup = recoveryPreview(runScope, 'session-1', 'backup', 'ignored', 5, 'req-b');
  assert.deepEqual([backup.type, backup.arguments.data], ['workspace.backup.preview', {}]);
  const run_ = recoveryExecute(runScope, 'session-1', 'restore', plan({ version: 2 }), 'req-e');
  assert.deepEqual([run_.type, run_.expected_version, run_.arguments], ['workspace.restore.execute', 2, { plan_id: 'plan-1', plan_version: 2 }]);
  assert.equal(recoveryExecute(runScope, 'session-1', 'backup', plan(), 'req-f').type, 'workspace.backup.execute');
  assert.equal(recoveryProblem(plan(), 'restore', 'bk-1', NOW), null);
  assert.match(recoveryProblem(plan(), 'restore', 'bk-2', NOW)!, /не содержит/);
  assert.match(recoveryProblem(plan({ expires_at: T(10) }), 'restore', 'bk-1', NOW)!, /истёк/);
  assert.equal(recoveryProblem(plan({ dependencies: [] }), 'backup', '', NOW), null);
  assert.match(recoveryProblem(plan({ expires_at: T(10) }), 'backup', '', NOW)!, /истёк/);
});
test('workspace projections from another partition or with bad enumerations are rejected', () => {
  assert.throws(() => connections(page([{ ...connection(), scope: { ...scope, memory_mode: 'test' } }]), scope));
  assert.throws(() => connections(page([connection({ verification: 'trusted' })]), scope));
  assert.throws(() => connections(page([connection({ configured: 'yes' })]), scope));
  assert.throws(() => capabilities(page([capability({ health: 'great' })]), scope));
  assert.throws(() => capabilities(page([capability({ effect: 'magic' })]), scope));
  assert.throws(() => capabilities(page([capability({ idempotency: 'maybe' })]), scope));
  assert.throws(() => permissions(page([permission({ level: 'root' })]), scope));
  assert.throws(() => permissions(page([permission({ expires_at: 'never' })]), scope));
  assert.throws(() => workspaces(page([{ ...base, id: 'w1', active_agent_id: 'bad id', policy_version: 1, profile_ids: [], connection_ids: [], settings: {} }]), scope));
  assert.equal(connections(page([connection()]), scope).items.length, 1);
  assert.equal(capabilities(page([capability()]), scope).items.length, 1);
  assert.equal(permissions(page([permission()]), scope).items.length, 1);
  assert.equal(workspaces(page([{ ...base, id: 'w1', active_agent_id: null, policy_version: 1, profile_ids: ['p1'], connection_ids: [], settings: {} }]), scope).items.length, 1);
});
test('workspace reads use the allowlisted resources and the exact partition', async () => {
  const paths: string[] = [];
  const api = new PanelApi(fetcher(async (path) => { paths.push(path); return json(page([])); }));
  await api.connections(scope); await api.capabilities(scope); await api.permissions(scope); await api.workspaces(scope);
  assert.deepEqual(paths.map((path) => new URL(path, 'http://x').pathname), ['/api/v1/connections', '/api/v1/capabilities', '/api/v1/permissions', '/api/v1/workspaces']);
  for (const path of paths) assert.equal(new URL(path, 'http://x').searchParams.get('workspace_id'), 'synthetic');
});
