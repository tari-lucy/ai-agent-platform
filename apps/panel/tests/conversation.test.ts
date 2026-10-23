import test from 'node:test';
import assert from 'node:assert/strict';
import { event, workSessionTranscript, workSessions } from '../src/shared/validation.ts';
import { affectedResources } from '../src/shared/events.ts';
import { closeCommand, discussTitle, messageCommand, messageProblem, openCommand, pending, sessionFor, stopNotice, turn, waitingLabels, writable } from '../src/ui/work/conversation.ts';
import type { Scope, WorkSession, WorkSessionTranscript } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-21T00:${String(minute).padStart(2, '0')}:00Z`;
const entry = (sequence: number, speaker: 'operator' | 'character', text: string | null = 'Текст ' + sequence) =>
  ({ sequence, speaker, text, withheld: text === null, at: T(sequence) });
const transcript = (over: Partial<WorkSessionTranscript> = {}): WorkSessionTranscript => ({
  scope, session_id: 's-1', session_version: 3, state: 'open', title: 'Конспект', entries: [entry(1, 'operator')],
  complete: true, checked_at: T(9), ...over,
} as WorkSessionTranscript);

test('the session event is accepted and refreshes the conversation reads', () => {
  const value = { id: 'e1', event_id: 'e1', type: 'work-session.changed', scope, occurred_at: T(1), resource_id: 's-1', resource_version: 2 };
  assert.equal(event(value).type, 'work-session.changed');
  assert.deepEqual(affectedResources['work-session.changed'], ['work-sessions', 'work-session-messages', 'work-session-transcript']);
});

test('transcripts are accepted only ordered, in the partition and with text or withheld', () => {
  assert.equal(workSessionTranscript(transcript(), scope, 's-1').entries.length, 1);
  assert.throws(() => workSessionTranscript(transcript(), scope, 's-2'));
  assert.throws(() => workSessionTranscript(transcript({ scope: { ...scope, memory_mode: 'test' } }), scope, 's-1'));
  assert.throws(() => workSessionTranscript(transcript({ entries: [entry(2, 'operator'), entry(1, 'character')] as never }), scope, 's-1'));
  assert.throws(() => workSessionTranscript(transcript({ entries: [{ ...entry(1, 'operator'), withheld: true }] as never }), scope, 's-1'));
  assert.equal(workSessionTranscript(transcript({ entries: [entry(1, 'operator', null)] as never }), scope, 's-1').entries[0].withheld, true);
  const page = { items: [{ id: 's-1', scope, version: 1, created_at: T(1), updated_at: T(1), title: 'Конспект', state: 'waiting', waiting_ticket_ids: [], last_sequence: 0 }], limit: 50, next_cursor: null, complete: true };
  assert.throws(() => workSessions(page, scope));  // waiting needs a ticket
});

test('whose turn: a stored message waits for execution; no reply is promised', () => {
  assert.equal(turn(transcript(), false), 'Сообщение сохранено; исполнение начнётся только после запуска');
  assert.equal(turn(transcript(), true), 'Сообщение сохранено и ожидает исполнения');
  assert.equal(turn(transcript({ processed_sequence: 1 }), true), 'Сообщение учтено агентом');
  assert.equal(turn(transcript({ processed_sequence: 1, entries: [entry(1, 'operator'), entry(2, 'character')] as never }), true), 'Ваш ход');
  assert.equal(turn(transcript({ entries: [] }), false), 'Ваш ход');
  assert.equal(turn(transcript({ state: 'closed' }), true), 'Разговор закрыт');
  assert.equal(pending(transcript({ processed_sequence: 1 }), 2, 'operator'), true);
  assert.equal(pending(transcript({ processed_sequence: 1 }), 1, 'operator'), false);
  assert.equal(pending(transcript(), 2, 'character'), false);
  assert.equal(writable(transcript({ state: 'closed' })), false);
  assert.equal(writable(transcript({ state: 'waiting' })), true);
});

test('a pending answer is separate from history and a withheld answer cannot be acknowledged', () => {
  const draft = { scope: runScope, utterance_id: 'reply-1', version: 1, revision: 1,
    text: 'Ответ', withheld: false, acknowledgeable: true };
  const ready = transcript({ pending_reply: draft });
  assert.equal(workSessionTranscript(ready, scope, 's-1').entries.length, 1);
  assert.equal(turn(ready, true), 'Ответ готов, ждёт получения');
  const withheld = transcript({ pending_reply: { ...draft, text: null, withheld: true, acknowledgeable: false } });
  assert.equal(workSessionTranscript(withheld, scope, 's-1').pending_reply?.text, null);
  assert.equal(turn(withheld, true), 'Ответ скрыт: основания недоступны');
  assert.throws(() => workSessionTranscript(transcript({ pending_reply: { ...draft, withheld: true } }), scope, 's-1'));
  assert.throws(() => workSessionTranscript(transcript({ pending_reply: { ...draft,
    scope: { ...runScope, memory_mode: 'test' } } }), scope, 's-1'));
});

test('commands use the stable area, the seen version and the exact text', () => {
  assert.equal(messageProblem(' \n'), 'Напишите сообщение.');
  const open = openCommand(runScope, ' Конспект ', 'req-1');
  assert.deepEqual([open.type, open.scope, open.target, open.arguments.data], ['work.session.open', scope, 'work', { title: 'Конспект' }]);
  const say = messageCommand(runScope, transcript(), ' Привет ', 'req-2');
  assert.deepEqual([say.target, say.expected_version, say.arguments.data], ['s-1', 3, { session_id: 's-1', text: ' Привет ' }]);
  const close = closeCommand(runScope, transcript(), 'req-3');
  assert.deepEqual([close.type, close.expected_version], ['work.session.close', 3]);
});

test('a task is discussed in its own open conversation, opened with the task link', () => {
  const session = (id: string, over: Partial<WorkSession> = {}) => ({
    id, scope, version: 1, created_at: T(1), updated_at: T(1), title: 'Р', state: 'open', task_ids: ['t-1'], last_sequence: 0, ...over,
  } as WorkSession);
  const rows = [session('old', { updated_at: T(1) }), session('new', { updated_at: T(5) }), session('closed', { state: 'closed', updated_at: T(9) }), session('other', { task_ids: ['t-2'] })];
  assert.equal(sessionFor('t-1', rows)?.id, 'new');  // newest still-open one; a closed one is not reused
  assert.equal(sessionFor('t-3', rows), undefined);
  const open = openCommand(runScope, discussTitle('Конспект'), 'req-9', 't-1');
  assert.deepEqual(open.arguments.data, { title: 'Обсуждение: Конспект', task_id: 't-1' });
  assert.equal(discussTitle('x'.repeat(300)).length, 200);
});

test('a waiting message shows why: no right, the access request, its refusal or no work contour', () => {
  assert.equal(turn(transcript({ waiting_reason: 'access_requested', access_ticket_id: 't-1' }), true),
    'Для рабочего разговора требуется разрешение: решите запрос в обращениях');
  assert.equal(turn(transcript({ waiting_reason: 'access_refused', access_ticket_id: 't-1' }), true),
    'Разрешение на рабочий контур отклонено; сообщения не исполняются');
  assert.equal(turn(transcript({ waiting_reason: 'unavailable', waiting_detail: 'DOCKER_NOT_FOUND' }), true),
    'Рабочий контур не подключён на этом хосте (DOCKER_NOT_FOUND)');
  assert.equal(turn(transcript({ processed_sequence: 1, waiting_reason: 'access_refused' }), true), 'Сообщение учтено агентом');
  assert.equal(turn(transcript({ waiting_reason: 'unavailable' }), false), 'Сообщение сохранено; исполнение начнётся только после запуска');
  assert.equal(workSessionTranscript(transcript({ waiting_reason: 'access_requested', access_ticket_id: 't-1' }), scope, 's-1').access_ticket_id, 't-1');
  assert.throws(() => workSessionTranscript(transcript({ waiting_reason: 'later' as never }), scope, 's-1'));
});

test('an unavailable message stops the conversation with the reason and the way on', () => {
  assert.equal(turn(transcript({ waiting_reason: 'message_unavailable' }), false), waitingLabels.message_unavailable);
  const stopped = transcript({ state: 'invalidated', invalidated_reason: 'message_unavailable', entries: [] });
  assert.equal(turn(stopped, true), 'Разговор остановлен: начните новый разговор');
  assert.match(stopNotice(stopped) ?? '', /не исполнялось.*начните новый разговор/s);
  assert.equal(stopNotice(transcript()), null);
  assert.match(stopNotice(transcript({ state: 'invalidated', entries: [] })) ?? '', /сброшен/);
  assert.equal(workSessionTranscript(stopped, scope, 's-1').invalidated_reason, 'message_unavailable');
  assert.throws(() => workSessionTranscript(transcript({ invalidated_reason: 'message_unavailable' }), scope, 's-1'));
  assert.throws(() => workSessionTranscript(transcript({ state: 'invalidated', invalidated_reason: 'other' as never }), scope, 's-1'));
});
