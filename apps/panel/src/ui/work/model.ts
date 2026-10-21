import type { CreateCommand, EditCommand, Episode, PrivateApiStatus, Scope, Ticket, WorkAttempt, WorkStep, WorkTask } from '../../shared/contracts.ts';

// Pure work-screen logic: grouping, whose turn, labels and the two operator commands. No I/O.
// A task is a goal; an attempt is one try. "Own conclusion" is never shown as a confirmed result.
export const originLabels: Record<WorkTask['origin'], string> = {
  assignment: 'Поручение', own_goal: 'Своя цель', commitment: 'Обязательство', ticket_followup: 'По обращению',
};
export const stateLabels: Record<WorkTask['state'], string> = {
  open: 'Ждёт шага', active: 'В работе', waiting: 'Ждёт', done: 'Завершена', abandoned: 'Отменена',
};
export const stepLabels: Record<WorkStep['status'], string> = { succeeded: 'выполнено', failed: 'отказ', unknown: 'исход неизвестен' };
export const attemptLabels: Record<WorkAttempt['state'], string> = {
  prepared: 'готовится', submitted: 'выполняется', succeeded: 'завершена', failed: 'не удалась',
  cancelled: 'отменена', unknown: 'исход неизвестен — сверяется по заданию',
};
const area = (scope: Scope): Scope => ({
  workspace_id: scope.workspace_id, persona_id: scope.persona_id, memory_space_id: scope.memory_space_id, memory_mode: scope.memory_mode,
});
export const finished = (task: WorkTask) => task.state === 'done' || task.state === 'abandoned';

// Whose move it is now. A waiting task names the ticket side; STOPPED means nobody moves yet.
export function turn(task: WorkTask, tickets: Ticket[], running: boolean): string {
  if (finished(task)) return 'Завершена';
  if (task.state === 'waiting') {
    const waiting = tickets.filter((ticket) => (task.waiting_ticket_ids ?? []).includes(ticket.id));
    if (waiting.some((ticket) => ticket.next_turn === 'operator')) return 'Нужен ваш ответ в обращении';
    return 'Ждёт ответа или возможности';
  }
  return running ? 'Ход агента' : 'Ход агента после запуска';
}
export function groups(tasks: WorkTask[], tickets: Ticket[]): { mine: WorkTask[]; working: WorkTask[]; done: WorkTask[] } {
  const newest = (list: WorkTask[]) => [...list].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
  const mine = tasks.filter((task) => turn(task, tickets, true) === 'Нужен ваш ответ в обращении');
  return {
    mine: newest(mine),
    working: newest(tasks.filter((task) => !finished(task) && !mine.includes(task))),
    done: newest(tasks.filter(finished)),
  };
}
export const attemptsOf = (task: WorkTask, attempts: WorkAttempt[]) =>
  attempts.filter((row) => row.task_id === task.id).sort((a, b) => a.attempt_no - b.attempt_no);
// Confirmed experience of this task: one episode per succeeded step that wrote files.
export function experienceOf(task: WorkTask, episodes: Episode[]): Episode[] {
  const keys = new Set((task.progress ?? []).map((step) => 'work-' + step.attempt_id));
  return episodes.filter((episode) => keys.has(episode.operation_id));
}
export function spend(status: PrivateApiStatus | undefined): string | null {
  if (!status || !status.activity_daily_calls) return null;
  return `Деятельность сегодня: ${status.activity_calls_used ?? 0} из ${status.activity_daily_calls} вызовов модели`;
}

export const MAX_GOAL = 4000;
export function goalProblem(goal: string): string | null {
  if (!goal.trim()) return 'Опишите поручение.';
  if (goal.length > MAX_GOAL) return `Поручение длиннее ${MAX_GOAL} символов.`;
  return null;
}
export const assignCommand = (scope: Scope, goal: string, reason: string, requestId: string): CreateCommand => ({
  type: 'work.assign', request_id: requestId, scope: area(scope), target: 'work',
  arguments: { schema_id: 'work.assign-args', data: { goal: goal.trim(), reason: reason.trim() } },
});
export const abandonCommand = (scope: Scope, task: WorkTask, reason: string, requestId: string): EditCommand => ({
  type: 'work.abandon', request_id: requestId, scope: area(scope), target: task.id, expected_version: task.version,
  arguments: { schema_id: 'work.abandon-args', data: { task_id: task.id, reason: reason.trim() } },
});
