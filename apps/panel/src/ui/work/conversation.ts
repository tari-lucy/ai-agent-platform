import type { CreateCommand, EditCommand, Scope, WorkSession, WorkSessionTranscript } from '../../shared/contracts.ts';

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
  speaker === 'operator' && sequence > (transcript.processed_sequence ?? 0);
// Whose move. A stored message is not an answer: no reply is promised before the session runs it.
export function turn(transcript: WorkSessionTranscript, running: boolean): string {
  if (transcript.state === 'closed') return 'Разговор закрыт';
  if (transcript.state === 'invalidated') return 'Контекст сброшен: начните новый разговор';
  if (transcript.state === 'waiting') return 'Ждёт ответа на обращение';
    if (transcript.pending_reply) return transcript.pending_reply.withheld ? 'Ответ скрыт: основания недоступны' :
      transcript.pending_reply.acknowledgeable ? 'Ответ готов, ждёт получения' : 'Ответ подготовлен; подтверждение пока недоступно';
  if (transcript.entries.some((entry) => pending(transcript, entry.sequence, entry.speaker))) {
    return running ? 'Сообщение сохранено и ожидает исполнения' : 'Сообщение сохранено; исполнение начнётся только после запуска';
  }
  const last = transcript.entries[transcript.entries.length - 1];
  if (!last || last.speaker === 'character') return 'Ваш ход';
  return 'Сообщение учтено агентом';
}
export const writable = (transcript: WorkSessionTranscript) => transcript.state === 'open' || transcript.state === 'waiting';
export const openCommand = (scope: Scope, title: string, requestId: string): CreateCommand => ({
  type: 'work.session.open', request_id: requestId, scope: area(scope), target: 'work',
  arguments: { schema_id: 'work.session.open-args', data: { title: title.trim() } },
});
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
