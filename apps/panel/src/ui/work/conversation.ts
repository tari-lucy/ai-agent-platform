import type { CreateCommand, EditCommand, Scope, WorkSession, WorkSessionProgress, WorkSessionTranscript } from '../../shared/contracts.ts';

// Pure work-conversation logic: commands, whose turn and labels. No I/O.
// One identity: the person answers here through its own working session, not a second assistant.
export type SessionState = NonNullable<WorkSession['state']>;
export const sessionStateLabels: Record<SessionState, string> = {
  open: 'Открыт', waiting: 'Ждёт ответа на обращение', closed: 'Закрыт', invalidated: 'Контекст сброшен',
};
const area = (scope: Scope): Scope => ({
  workspace_id: scope.workspace_id, persona_id: scope.persona_id, memory_space_id: scope.memory_space_id, memory_mode: scope.memory_mode,
});
export const MAX_MESSAGE = 16000;
export function messageProblem(text: string): string | null {
  if (!text.trim()) return 'Напишите сообщение.';
  if (text.length > MAX_MESSAGE) return `Сообщение длиннее ${MAX_MESSAGE} символов.`;
  return null;
}
export function titleProblem(title: string): string | null {
  if (!title.trim()) return 'Назовите разговор.';
  if (title.length > 200) return 'Название длиннее 200 символов.';
  return null;
}
// Messages the person's session has not taken yet: stored, waiting for execution.
export const pending = (transcript: WorkSessionTranscript, sequence: number, speaker: string) =>
  speaker !== 'character' && sequence > (transcript.processed_sequence ?? 0);
// Whose move. A stored message is not an answer: no reply is promised before the session runs it.
export function turn(transcript: WorkSessionTranscript, running: boolean): string {
  if (transcript.state === 'closed') return 'Разговор закрыт';
  if (transcript.state === 'invalidated') return transcript.invalidated_reason ? 'Разговор остановлен: начните новый разговор' : 'Контекст сброшен: начните новый разговор';
  // Shown even while STOPPED: a start would not execute this message either.
  if (transcript.waiting_reason === 'message_unavailable') return waitingLabels.message_unavailable;
  if (transcript.state === 'waiting') return 'Ждёт ответа на обращение';
    if (transcript.pending_reply) return transcript.pending_reply.withheld ? 'Ответ скрыт: основания недоступны' :
      transcript.pending_reply.acknowledgeable ? 'Ответ готов, ждёт получения' : 'Ответ подготовлен; подтверждение пока недоступно';
  const ended = transcript.progress && endedStates.has(transcript.progress.state) ? progressLabels[transcript.progress.state] : null;
  if (ended) return ended;
  if (transcript.entries.some((entry) => pending(transcript, entry.sequence, entry.speaker))) {
    // STOPPED: nothing runs anyway; the reason matters once the person could take the message.
    const notice = running ? waitingNotice(transcript) : null;
    if (notice) return notice;
    return running ? 'Сообщение сохранено и ожидает исполнения' : 'Сообщение сохранено; исполнение начнётся только после запуска';
  }
  const last = transcript.entries[transcript.entries.length - 1];
  if (!last || last.speaker === 'character') return 'Ваш ход';
  return 'Сообщение учтено агентом';
}
// Why stored messages wait, as the platform reports it; the access request is a technical one.
export const waitingLabels: Record<NonNullable<WorkSessionTranscript['waiting_reason']>, string> = {
  unavailable: 'Рабочий контур не подключён на этом хосте',
  access_required: 'Для рабочего разговора требуется разрешение; запрос появится после запуска',
  access_requested: 'Для рабочего разговора требуется разрешение: решите запрос в обращениях',
  access_refused: 'Разрешение на рабочий контур отклонено; сообщения не исполняются',
  message_unavailable: 'Сообщение оператора отозвано, удалено или истекло и не будет исполнено; начните новый разговор',
};
export function waitingNotice(transcript: WorkSessionTranscript): string | null {
  if (!transcript.waiting_reason) return null;
  const label = waitingLabels[transcript.waiting_reason];
  return transcript.waiting_reason === 'unavailable' && transcript.waiting_detail ? `${label} (${transcript.waiting_detail})` : label;
}
// Why the owner stopped this conversation and how to continue: the existing new-conversation form.
export const invalidatedLabels: Record<NonNullable<WorkSessionTranscript['invalidated_reason']>, string> = {
  message_unavailable: 'Сообщение оператора отозвано, удалено или истекло, поэтому оно не исполнялось. '
    + 'Этот разговор остановлен; чтобы продолжить, начните новый разговор в поле «Новый разговор о работе» '
    + 'и при необходимости повторите задачу заново.',
};
export function stopNotice(transcript: WorkSessionTranscript): string | null {
  if (transcript.state !== 'invalidated') return null;
  return transcript.invalidated_reason ? invalidatedLabels[transcript.invalidated_reason]
    : 'Контекст этого разговора сброшен. Чтобы продолжить, начните новый разговор.';
}
export const writable = (transcript: WorkSessionTranscript) => transcript.state === 'open' || transcript.state === 'waiting';
export const openCommand = (scope: Scope, title: string, requestId: string, taskId?: string): CreateCommand => ({
  type: 'work.session.open', request_id: requestId, scope: area(scope), target: 'work',
  arguments: { schema_id: 'work.session.open-args', data: taskId ? { title: title.trim(), task_id: taskId } : { title: title.trim() } },
});
// The conversation about one task: its newest still-open one, if any.
export function sessionFor(taskId: string, rows: WorkSession[]): WorkSession | undefined {
  return newestFirst(rows).find((row) => (row.task_ids ?? []).includes(taskId) && (row.state === 'open' || row.state === 'waiting'));
}
export const discussTitle = (goal: string) => {
  const title = 'Обсуждение: ' + goal.trim();
  return title.length > 200 ? title.slice(0, 199) + '…' : title;
};
export const messageCommand = (scope: Scope, transcript: WorkSessionTranscript, text: string, requestId: string): EditCommand => ({
  type: 'work.session.message', request_id: requestId, scope: area(scope), target: transcript.session_id,
  expected_version: transcript.session_version,
  // The text is sent as written; trimming would change what the owner stores and replays.
  arguments: { schema_id: 'work.session.message-args', data: { session_id: transcript.session_id, text } },
});
export const closeCommand = (scope: Scope, transcript: WorkSessionTranscript, requestId: string): EditCommand => ({
  type: 'work.session.close', request_id: requestId, scope: area(scope), target: transcript.session_id,
  expected_version: transcript.session_version,
  arguments: { schema_id: 'work.session.close-args', data: { session_id: transcript.session_id } },
});
export function newestFirst(rows: WorkSession[]): WorkSession[] {
  return [...rows].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
}

// ---- the current turn, as the platform recorded it (never a timer, never a resend) -------------------
type TurnState = WorkSessionProgress['state'];
export const endedStates = new Set<TurnState>(['failed', 'cancelled', 'unknown']);
export const progressLabels: Record<TurnState, string> = {
  waiting_start: 'Сообщение сохранено и ждёт запуска агента',
  queued: 'Сообщение принято: ход ждёт своей очереди',
  running: 'Агент выполняет рабочий ход',
  reply_ready: 'Ответ готов',
  done: 'Рабочий ход завершён',
  failed: 'Рабочий ход завершился ошибкой',
  cancelled: 'Рабочий ход отменён',
  unknown: 'Исход рабочего хода не подтверждён',
};
// What the operator can do next. A reload or reconnect never sends anything again.
export const nextStep: Record<TurnState, string> = {
  waiting_start: 'Нажмите START, когда будете готовы: до этого ничего не исполняется.',
  queued: 'Ничего делать не нужно: ход начнётся, когда подойдёт очередь.',
  running: 'Можно подождать или нажать STOP: поздний результат после STOP не применяется.',
  reply_ready: 'Ответ появится в истории, как только вкладка его получит.',
  done: 'Сохранённые файлы — в библиотеке.',
  failed: 'Ответа нет. Повторно ничего само не отправится: напишите сообщение ещё раз или уточните задачу.',
  cancelled: 'Ход остановлен (STOP или отзыв допуска), ответа нет. Чтобы продолжить, запустите агента и напишите сообщение снова.',
  unknown: 'Платформа не повторяет такой ход вслепую. Проверьте файлы в библиотеке и при необходимости напишите сообщение снова.',
};
// Elapsed time between two recorded instants (the turn's own time and the server's check).
export function elapsed(since: string, checkedAt: string): string | null {
  const seconds = Math.floor((Date.parse(checkedAt) - Date.parse(since)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 0) return null;
  if (seconds < 60) return `${seconds} с`;
  const minutes = Math.floor(seconds / 60);
  return minutes < 60 ? `${minutes} мин ${seconds % 60} с` : `${Math.floor(minutes / 60)} ч ${minutes % 60} мин`;
}
export function callsText(progress: WorkSessionProgress): string | null {
  const calls = progress.model_calls;
  if (!calls) return null;
  const parts = [`завершено: ${calls.completed}`];
  if (calls.running) parts.push(`выполняется: ${calls.running}`);
  if (calls.failed) parts.push(`с ошибкой: ${calls.failed}`);
  return 'Вызовы модели — ' + parts.join(', ');
}
