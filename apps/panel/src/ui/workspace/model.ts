import type {
  Capability, Connection, ConsequencePlan, ControlCommand, EditCommand, ExecutePlanCommand, OperationProjection,
  InitialAccessStatus, Permission, PlanCommand, Receipt, RunSession, Scope,
} from '../../shared/contracts.ts';

// Pure workspace logic: run indicators and controls, operations, rights, recovery plans. No I/O.

export const runLabels = { STOPPED: 'Остановлен', STARTING: 'Запускается', ACTIVE: 'Работает', STOPPING: 'Останавливается', FAULTED: 'Требуется проверка' } as const;
export const activityLabels = { OPEN: 'Деятельность разрешена', PAUSING: 'Переходит на паузу', PAUSED: 'Деятельность на паузе' } as const;
export type Indicator = { label: string; value: string; tone: 'ok' | 'wait' | 'warn' | 'off' };

// Separate indicators: one word never stands for several independent states.
export function indicators(run: RunSession): Indicator[] {
  return [
    { label: 'Запуск', value: runLabels[run.state], tone: run.state === 'ACTIVE' ? 'ok' : run.state === 'STOPPED' ? 'off' : run.state === 'FAULTED' ? 'warn' : 'wait' },
    { label: 'Деятельность', value: activityLabels[run.activity], tone: run.activity === 'OPEN' ? 'ok' : 'wait' },
    { label: 'Режим', value: run.autonomy === 'observation' ? 'Наблюдение' : 'Автономная работа', tone: 'ok' },
    // Text MVP: voice has no adapter; "enabled" only reports the owner's flag, never a working voice.
    { label: 'Голос', value: run.voice_enabled ? 'Включён по данным ядра' : 'Не подключён / выключен', tone: 'off' },
    { label: 'Гейммастер', value: run.gm_paused ? 'На паузе' : 'Пауза снята по данным ядра', tone: run.gm_paused ? 'off' : 'ok' },
  ];
}
export function runNotice(run: RunSession): string | null {
  if (run.state === 'STOPPING') return 'Допуски отозваны, но освобождение ресурсов ещё не подтверждено. Это не STOPPED.';
  if (run.state === 'FAULTED') return 'Состояние неизвестно: запуск закрыт до сверки ресурсов владельцем.';
  if (run.state === 'STOPPED') return 'Агент остановлен. Возобновление требует отдельного действия «Запустить»: восстановление или перезапуск его не запускают.';
  if (run.state === 'STARTING') return 'Запуск выполняется; допуск выдаётся только после проверки готовности.';
  return null;
}

export type ControlKind = 'agent.start' | 'agent.pause' | 'agent.resume' | 'session.end';
export const controlLabels: Record<ControlKind, string> = {
  'agent.start': 'Запустить', 'agent.pause': 'Пауза деятельности', 'agent.resume': 'Возобновить деятельность', 'session.end': 'Завершить сессию',
};
export type Availability = { enabled: boolean; reason: string };
export function controlAvailability(run: RunSession | undefined, kind: ControlKind): Availability {
  if (!run) return { enabled: false, reason: 'Нет подтверждённого запуска в этой области.' };
  if (run.state === 'FAULTED') return { enabled: false, reason: 'Требуется сверка ресурсов; управление закрыто.' };
  if (run.state === 'STOPPING' || run.state === 'STARTING') return { enabled: false, reason: 'Идёт переход состояния; дождитесь подтверждения.' };
  const ok = { enabled: true, reason: '' };
  switch (kind) {
    case 'agent.start': return run.state === 'STOPPED' ? ok : { enabled: false, reason: 'Агент уже запущен.' };
    case 'agent.pause': return run.state === 'ACTIVE' && run.activity === 'OPEN' ? ok : { enabled: false, reason: 'Пауза возможна только для работающей деятельности.' };
    case 'agent.resume': return run.state === 'ACTIVE' && run.activity === 'PAUSED' ? ok : { enabled: false, reason: 'Возобновлять нечего: деятельность не на паузе.' };
    case 'session.end': return run.state === 'ACTIVE' ? ok : { enabled: false, reason: 'Сессия не активна.' };
  }
}
// ASSUMPTION (owner decision pending): start/pause/resume address the agent, session.end the session.
export function controlCommand(run: RunSession, kind: ControlKind, autonomy: 'observation' | 'autonomous' | null, requestId: string): ControlCommand {
  return { type: kind, request_id: requestId, scope: run.scope, target: run.agent_id,
    expected_version: run.version, arguments: kind === 'agent.start' && autonomy ? { autonomy } : {} };
}
export type Control =
  | { phase: 'idle' } | { phase: 'sending'; command: ControlCommand } | { phase: 'unknown'; command: ControlCommand }
  | { phase: 'accepted'; receipt: Receipt } | { phase: 'refused'; code: string };
export function afterControl(command: ControlCommand, outcome: { receipt: Receipt } | { code: string }): Control {
  if ('receipt' in outcome) return outcome.receipt.status === 'rejected' ? { phase: 'refused', code: 'ACCESS_DENIED' } : { phase: 'accepted', receipt: outcome.receipt };
  return ['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(outcome.code) ? { phase: 'unknown', command } : { phase: 'refused', code: outcome.code };
}

// ---- operations ---------------------------------------------------------------------------------
const cancellable = new Set(['PROPOSED', 'AWAITING_APPROVAL', 'READY', 'QUEUED', 'RUNNING', 'WAITING']);
const terminal = new Set(['SUCCEEDED', 'REJECTED', 'FAILED', 'CANCELLED', 'EXPIRED']);
export const operationLabels: Record<OperationProjection['status'], string> = {
  PROPOSED: 'Предложена', AWAITING_APPROVAL: 'Ждёт решения', READY: 'Готова', QUEUED: 'В очереди', RUNNING: 'Выполняется', WAITING: 'Ждёт события',
  SUCCEEDED: 'Выполнена', REJECTED: 'Отклонена', FAILED: 'Ошибка', CANCELLED: 'Отменена', EXPIRED: 'Срок истёк', OUTCOME_UNKNOWN: 'Исход неизвестен',
};
export const canCancel = (op: OperationProjection) => cancellable.has(op.status);
export const needsReconciliation = (op: OperationProjection) => op.status === 'OUTCOME_UNKNOWN';
export function overdue(op: OperationProjection, now: number): boolean {
  return !terminal.has(op.status) && Date.parse(op.deadline) < now;
}
// Open and doubtful work first (unknown outcomes need a human), then by deadline; finished last.
export function orderOperations(rows: OperationProjection[]): OperationProjection[] {
  const rank = (op: OperationProjection) => (op.status === 'OUTCOME_UNKNOWN' ? 0 : terminal.has(op.status) ? 2 : 1);
  return [...rows].sort((a, b) => rank(a) - rank(b) || Date.parse(a.deadline) - Date.parse(b.deadline) || a.operation_id.localeCompare(b.operation_id));
}
// ASSUMPTION (owner decision pending): payload schema ids are not defined by the backend yet.
export const WORKSPACE_SCHEMA = { cancel: 'operation.cancel', permission: 'permission.edit', backup: 'workspace.backup', restore: 'workspace.restore' } as const;
export function cancelCommand(scope: Scope, target: string, op: OperationProjection, requestId: string): EditCommand {
  return { type: 'operation.cancel', request_id: requestId, scope, target, expected_version: op.version,
    arguments: { schema_id: WORKSPACE_SCHEMA.cancel, data: { operation_id: op.operation_id } } };
}

// ---- capabilities, connections, rights ------------------------------------------------------------
export const healthLabels = { ready: 'Готова', degraded: 'Деградирует', unavailable: 'Недоступна' } as const;
export const effectLabels = { none: 'Без воздействия', local_write: 'Локальная запись', external_effect: 'Внешнее воздействие', public_output: 'Публичный выпуск' } as const;
export const verificationLabels = { unverified: 'Не проверено', verified: 'Проверено', failed: 'Проверка не прошла' } as const;
export function degradation(capabilities: Capability[], connections: Connection[]): string[] {
  return [
    ...capabilities.filter((c) => c.health !== 'ready').map((c) => `Возможность ${c.id}: ${healthLabels[c.health].toLowerCase()}`),
    ...connections.filter((c) => !c.configured).map((c) => `Подключение ${c.name}: не настроено`),
    ...connections.filter((c) => c.configured && c.verification !== 'verified').map((c) => `Подключение ${c.name}: ${verificationLabels[c.verification].toLowerCase()}`),
  ];
}
export const levelLabels = { denied: 'Запрещено', prepare_only: 'Только подготовка', operator_decision: 'По решению оператора', automatic: 'Автоматически' } as const;
const levelRank = { denied: 0, prepare_only: 1, operator_decision: 2, automatic: 3 } as const;
export const levels = Object.keys(levelRank) as Permission['level'][];
export function isExpansion(from: Permission['level'], to: Permission['level']): boolean { return levelRank[to] > levelRank[from]; }
export function permissionState(permission: Permission, now: number): 'revoked' | 'expired' | 'active' {
  if (permission.revoked_at) return 'revoked';
  return Date.parse(permission.expires_at) <= now ? 'expired' : 'active';
}
// Widening needs an explicit tick; an external or public capability at "automatic" is spelled out.
export function permissionWarning(permission: Permission, to: Permission['level'], capability: Capability | undefined): string | null {
  if (!isExpansion(permission.level, to)) return null;
  const risky = capability && (capability.effect === 'external_effect' || capability.effect === 'public_output');
  return risky && to === 'automatic' ?
    `Вы расширяете права до «Автоматически» для возможности с эффектом «${effectLabels[capability.effect]}». Это действие может происходить без вашего решения.` :
    `Вы расширяете права: «${levelLabels[permission.level]}» → «${levelLabels[to]}».`;
}
export function permissionCommand(scope: Scope, target: string, permission: Permission, level: Permission['level'], requestId: string): EditCommand {
  return { type: 'permission.edit', request_id: requestId, scope, target, expected_version: permission.version,
    arguments: { schema_id: WORKSPACE_SCHEMA.permission, data: { permission_id: permission.id, level } } };
}

// ---- backup and restore plans ----------------------------------------------------------------------
export const backupIdPattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/;
export type Recovery = 'backup' | 'restore';
export function recoveryPreview(scope: Scope, target: string, kind: Recovery, backupId: string, version: number, requestId: string): PlanCommand {
  return { type: kind === 'backup' ? 'workspace.backup.preview' : 'workspace.restore.preview', request_id: requestId, scope, target, expected_version: version,
    arguments: { schema_id: WORKSPACE_SCHEMA[kind], data: kind === 'restore' ? { backup_id: backupId } : {} } };
}
export function recoveryExecute(scope: Scope, target: string, kind: Recovery, plan: ConsequencePlan, requestId: string): ExecutePlanCommand {
  return { type: kind === 'backup' ? 'workspace.backup.execute' : 'workspace.restore.execute', request_id: requestId, scope, target,
    expected_version: plan.version, arguments: { plan_id: plan.id, plan_version: plan.version } };
}
// A restore plan must name the copy the operator typed; every plan must still be in force.
export function recoveryProblem(plan: ConsequencePlan, kind: Recovery, backupId: string, now: number): string | null {
  if (kind === 'restore' && !plan.dependencies.some((dep) => dep.resource_id === backupId)) return 'План не содержит выбранную копию. Подготовьте его заново.';
  if (Date.parse(plan.expires_at) <= now) return 'Срок плана истёк. Подготовьте его заново.';
  return null;
}
// OC-05: one decision for the whole configured set; the owner re-checks ceiling and versions.
export const DAY = 86400;
export function accessPreview(scope: Scope, days: number, constraints: string, requestId: string): PlanCommand {
  return { type: 'workplace.access.preview', request_id: requestId, scope, target: 'initial-access', expected_version: 1,
    arguments: { schema_id: 'workplace.access-args', data: { duration_seconds: days * DAY, constraints } } };
}
export function accessExecute(scope: Scope, plan: ConsequencePlan, requestId: string): ExecutePlanCommand {
  return { type: 'workplace.access.execute', request_id: requestId, scope, target: 'initial-access', expected_version: 1,
    arguments: { plan_id: plan.id, plan_version: plan.version } };
}
export function accessBlocked(status: InitialAccessStatus | undefined, days: number): string | null {
  if (!status) return 'Сведения о наборе ещё не получены.';
  if (!status.configured) return 'Набор начального доступа на этом хосте не настроен.';
  if (!status.ready) return 'Не все возможности набора готовы к работе: допуск сейчас выдать нельзя.';
  if (!Number.isSafeInteger(days) || days < 1 || days * DAY > status.max_duration_seconds) return 'Срок вне потолка хоста.';
  return null;
}
export function restoreBlocked(run: RunSession | undefined, backupId: string): string | null {
  if (!run) return 'Нет подтверждённого запуска: невозможно убедиться, что агент остановлен.';
  if (run.state !== 'STOPPED') return 'Восстановление возможно только при остановленном агенте (сейчас: ' + runLabels[run.state].toLowerCase() + ').';
  if (!backupIdPattern.test(backupId)) return 'Укажите идентификатор копии (латиница, цифры, . _ : -).';
  return null;
}
