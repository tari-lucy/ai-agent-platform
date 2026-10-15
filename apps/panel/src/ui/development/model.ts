import { configureGM } from '../../shared/owner-commands.ts';
import type {
  GmRuntimeState, ChangeState, ConsequencePlan, EditCommand, ExecutePlanCommand, Opportunity, PlanCommand, RunSession, SourceRef, StateChange, StoryArc,
} from '../../shared/contracts.ts';

// Pure development-tab logic: arcs, opportunities, change cards, attention queue, path and commands. No I/O.
// Four things stay apart: a possibility (offered), a lived event (resolved), an interpretation, an applied change.

export const arcLabels = { open: 'Активная', paused: 'Приостановлена', closed: 'Закрыта' } as const;
export type Opp = Opportunity['status'];
export const opportunityLabels: Record<Opp, string> = {
  DRAFT: 'Черновик', SCREENED: 'Проверена правилами', OFFERED: 'Предложена', ACCEPTED: 'Принята', REALIZING: 'Разыгрывается',
  RESOLVED: 'Пережито', BLOCKED: 'Заблокирована границей', DECLINED: 'Отклонена', EXPIRED: 'Срок истёк', CANCELLED: 'Отменена',
};
// Only these have not happened: they are possibilities, never a predicted future or a biography.
const possible = new Set<Opp>(['DRAFT', 'SCREENED', 'OFFERED', 'ACCEPTED']);
const cancellable = new Set<Opp>(['DRAFT', 'SCREENED', 'OFFERED', 'ACCEPTED']);
export const isPossibility = (opp: Opportunity) => possible.has(opp.status);
export const isLived = (opp: Opportunity) => opp.status === 'RESOLVED';
export const canCancelOpportunity = (opp: Opportunity) => cancellable.has(opp.status);
export const canPauseArc = (arc: StoryArc) => arc.status === 'open';

export function opportunitiesOf(arc: StoryArc, all: Opportunity[]): { possible: Opportunity[]; lived: Opportunity[]; blocked: Opportunity[]; other: Opportunity[] } {
  const own = all.filter((opp) => opp.arc_id === arc.id).sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
  return {
    possible: own.filter(isPossibility), lived: own.filter(isLived), blocked: own.filter((opp) => opp.status === 'BLOCKED'),
    other: own.filter((opp) => !isPossibility(opp) && !isLived(opp) && opp.status !== 'BLOCKED'),
  };
}
export function arcStateNote(arc: StoryArc, all: Opportunity[]): string {
  const blocked = all.filter((opp) => opp.arc_id === arc.id && opp.status === 'BLOCKED').length;
  if (arc.status === 'paused') return 'Линия приостановлена: ожидающие испытания отменены, позднее предложение её не оживляет.';
  if (arc.status === 'closed') return 'Линия закрыта.';
  return blocked ? `Есть заблокированные границей испытания: ${blocked}. Нужна остановка или пересмотр.` : 'Линия развивается автоматически; ручной разбор не нужен.';
}

// ---- change cards -------------------------------------------------------------------------------------
export const changeLabels: Record<ChangeState, string> = {
  PROPOSED: 'Предложено', VALIDATING: 'Проверяется', READY: 'Готово к применению', AWAITING_APPROVAL: 'Ожидает оператора',
  APPLIED: 'Применено', REJECTED: 'Отклонено', CONFLICT: 'Конфликт версий', CANCELLED: 'Отменено',
};
export type PatchView = { property: string; before: string; after: string; rollback: boolean } | null;
const text = (value: unknown) => (typeof value === 'number' ? String(value) : typeof value === 'string' ? value : null);
// The patch is an extension payload: read it defensively and say so when its shape is not recognised.
export function patchOf(change: StateChange): PatchView {
  const data = change.patch.data;
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null;
  const record = data as Record<string, unknown>;
  const [before, after] = [text(record.before), text(record.after)];
  if (typeof record.property !== 'string' || before === null || after === null) return null;
  return { property: record.property, before, after, rollback: record.kind === 'rollback' };
}
export const awaitingOperator = (change: StateChange) => change.status === 'AWAITING_APPROVAL';
export const canCorrect = (change: StateChange) => ['READY', 'AWAITING_APPROVAL', 'APPLIED'].includes(change.status);
export const canRollback = (change: StateChange) => change.status === 'APPLIED' && patchOf(change)?.rollback !== true;
export function versions(change: StateChange): string {
  return change.new_version == null ? `основа v${change.base_version}, ещё не применено` : `v${change.base_version} → v${change.new_version}`;
}
export function evidenceSummary(refs: SourceRef[]): string {
  const count = (state: SourceRef['availability']) => refs.filter((ref) => ref.availability === state).length;
  const lost = refs.length - count('available');
  return lost ? `${refs.length} основ., недоступно: ${lost}` : `${refs.length} основ.`;
}
// A change whose evidence is gone must not look confirmed.
export const evidenceLost = (change: StateChange) => change.evidence.length > 0 && change.evidence.every((ref) => ref.availability !== 'available');

export type Attention = { key: string; kind: 'approval' | 'conflict' | 'blocked'; text: string };
// What needs a human: waiting approvals (expiry is never consent), version conflicts, boundary blocks.
export function attention(changes: StateChange[], opps: Opportunity[]): Attention[] {
  return [
    ...changes.filter(awaitingOperator).map((change): Attention => ({ key: 'c:' + change.id, kind: 'approval', text: `Решение по изменению ${patchOf(change)?.property ?? change.proposal_id}` })),
    ...changes.filter((change) => change.status === 'CONFLICT').map((change): Attention => ({ key: 'x:' + change.id, kind: 'conflict', text: `Конфликт версий: ${patchOf(change)?.property ?? change.proposal_id}` })),
    ...opps.filter((opp) => opp.status === 'BLOCKED').map((opp): Attention => ({ key: 'o:' + opp.id, kind: 'blocked', text: `Граница: ${opp.reason_code ?? 'причина не указана'}` })),
  ];
}

export type PathItem = { key: string; at: string; kind: 'lived' | 'change' | 'rollback'; title: string; detail: string };
// The path: chosen and lived events and applied changes in time order; possibilities are not on it.
export function pathItems(changes: StateChange[], opps: Opportunity[]): PathItem[] {
  return [
    ...opps.filter(isLived).map((opp): PathItem => ({ key: 'o:' + opp.id, at: opp.updated_at, kind: 'lived', title: opp.choice ?? 'Выбор не записан', detail: opp.actual_outcome ?? 'Исход не записан' })),
    ...changes.filter((change) => change.status === 'APPLIED').map((change): PathItem => {
      const patch = patchOf(change);
      return { key: 'c:' + change.id, at: change.updated_at, kind: patch?.rollback ? 'rollback' : 'change',
        title: patch ? `${patch.property}: ${patch.before} → ${patch.after}` : 'Изменение применено (формат правки не распознан)', detail: change.interpretation };
    }),
  ].sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.key.localeCompare(b.key));
}

// ---- commands ---------------------------------------------------------------------------------------------
// Canonical payloads; owner resource IDs and versions are independent of RunSession.
export const DEV_SCHEMA = {
  gm: 'gm.configure-args', cancel: 'opportunity.cancel-args', pause: 'arc.pause-args', decision: 'change.decision-args', interpretation: 'change.interpretation-args', rollback: 'change.rollback-args',
} as const;
type OperatorContext = Pick<RunSession, 'scope'>;
const edit = (type: EditCommand['type'], run: OperatorContext, version: number, target: string, schema: string, data: Record<string, string | number | boolean>, requestId: string): EditCommand =>
  ({ type, request_id: requestId, scope: run.scope, target, expected_version: version, arguments: { schema_id: schema, data } });
export const gmPauseCommand = (run: OperatorContext, settings: GmRuntimeState, paused: boolean, requestId: string) => configureGM(run.scope, settings, { paused }, requestId);
export const intensities = { low: 'Низкая', normal: 'Обычная', high: 'Высокая' } as const;
export type Intensity = keyof typeof intensities;
export const gmIntensityCommand = (run: OperatorContext, settings: GmRuntimeState, intensity: Intensity, requestId: string) => configureGM(run.scope, settings, { intensity: { low: 0.2, normal: 0.5, high: 0.8 }[intensity] }, requestId);
export const cancelOpportunityCommand = (run: OperatorContext, opp: Opportunity, requestId: string) => edit('opportunity.cancel', run, opp.version, opp.id, DEV_SCHEMA.cancel, {}, requestId);
export const pauseArcCommand = (run: OperatorContext, arc: StoryArc, requestId: string) => edit('arc.pause', run, arc.version, arc.id, DEV_SCHEMA.pause, {}, requestId);
export type Decision = 'accept' | 'reject';
export const decisionCommand = (run: OperatorContext, change: StateChange, decision: Decision, reason: string, requestId: string) =>
  edit('change.decision', run, change.version, change.id, DEV_SCHEMA.decision, { decision, reason: reason.trim() }, requestId);
export function validInterpretation(change: StateChange, text: string, reason: string): string | null {
  if (!canCorrect(change)) return 'Изменение в этом состоянии исправлять нельзя.';
  if (!text.trim()) return 'Введите исправленную интерпретацию.';
  if (text.length > 1500) return 'Интерпретация длиннее 1500 символов.';
  if (text.trim() === change.interpretation.trim()) return 'Текст не отличается от текущей интерпретации.';
  if (!reason.trim()) return 'Укажите основание исправления.';
  if (reason.length > 1000) return 'Основание длиннее 1000 символов.';
  return null;
}
export const interpretationCommand = (run: OperatorContext, change: StateChange, text: string, reason: string, requestId: string) =>
  edit('change.interpretation', run, change.version, change.id, DEV_SCHEMA.interpretation, { interpretation: text, reason: reason.trim() }, requestId);
export const rollbackPreview = (run: OperatorContext, change: StateChange, requestId: string): PlanCommand =>
  ({ type: 'change.rollback.preview', request_id: requestId, scope: run.scope, target: change.id, expected_version: change.version,
    arguments: { schema_id: DEV_SCHEMA.rollback, data: {} } });
export const rollbackExecute = (run: OperatorContext, change: StateChange, plan: ConsequencePlan, requestId: string): ExecutePlanCommand =>
  ({ type: 'change.rollback.execute', request_id: requestId, scope: run.scope, target: change.id, expected_version: plan.version,
    arguments: { plan_id: plan.id, plan_version: plan.version } });
// A rollback plan must name the very change; an expired plan is not run.
export function rollbackProblem(plan: ConsequencePlan, change: StateChange, now: number): string | null {
  if (!plan.dependencies.some((dep) => dep.resource_id === change.id)) return 'План не содержит выбранное изменение. Подготовьте его заново.';
  if (Date.parse(plan.expires_at) <= now) return 'Срок плана истёк. Подготовьте его заново.';
  return null;
}
