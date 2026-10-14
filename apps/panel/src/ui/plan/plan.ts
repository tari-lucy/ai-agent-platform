import type { ConsequencePlan, ExecutePlanCommand, OperationProjection, Receipt } from '../../shared/contracts.ts';

// Shared plan flow logic (memory deletion/export, backup, restore): preview → confirm → execute → progress.
export type PlanPhase =
  | { phase: 'idle' } | { phase: 'previewing' }
  | { phase: 'ready'; plan: ConsequencePlan } | { phase: 'executing'; plan: ConsequencePlan; command: ExecutePlanCommand }
  | { phase: 'unknown'; plan: ConsequencePlan; command: ExecutePlanCommand }
  | { phase: 'running'; plan: ConsequencePlan; receipt: Receipt } | { phase: 'refused'; code: string };
export function afterExecute(plan: ConsequencePlan, command: ExecutePlanCommand, outcome: { receipt: Receipt } | { code: string }): PlanPhase {
  if ('receipt' in outcome) {
    return outcome.receipt.status === 'rejected' ? { phase: 'refused', code: 'ACCESS_DENIED' } : { phase: 'running', plan, receipt: outcome.receipt };
  }
  return ['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(outcome.code) ?
    { phase: 'unknown', plan, command } : { phase: 'refused', code: outcome.code };
}
export type Progress = { done: string[]; pending: string[]; text: string };
const finished = new Set(['SUCCEEDED']);
const failed = new Set(['REJECTED', 'FAILED', 'CANCELLED', 'EXPIRED']);
// Executed and still-pending parts as the owner reports them. Unknown is never shown as done.
export function progress(operation: OperationProjection | null, operationLabel: string): Progress {
  if (!operation) return { done: [], pending: [operationLabel], text: 'Ожидаем подтверждение от владельца данных.' };
  if (finished.has(operation.status)) return { done: [operationLabel], pending: [], text: 'Выполнено.' };
  if (failed.has(operation.status)) return { done: [], pending: [operationLabel], text: 'Не выполнено: ' + operation.status + '.' };
  if (operation.status === 'OUTCOME_UNKNOWN') return { done: [], pending: [operationLabel], text: 'Результат неизвестен: требуется сверка, не повторяйте вслепую.' };
  return { done: [], pending: [operationLabel], text: operation.waiting_reason ?? 'Выполняется.' };
}
