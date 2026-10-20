import test from 'node:test';
import assert from 'node:assert/strict';
import { ticketMessages, tickets, event } from '../src/shared/validation.ts';
import { affectedResources } from '../src/shared/events.ts';
import { conversation, groups, readCommand, replyCommand, replyProblem, turnLabel, unread } from '../src/ui/tickets/model.ts';
import type { Scope, Ticket, TicketMessage } from '../src/shared/contracts.ts';

const scope: Scope = { workspace_id: 'synthetic', persona_id: 'alpha', memory_space_id: 'alpha-work', memory_mode: 'work' };
const runScope: Scope = { ...scope, agent_id: 'primary', run_id: 'run-1', session_id: 'session-1' };
const T = (minute: number) => `2026-10-21T00:${String(minute).padStart(2, '0')}:00Z`;
const page = (items: unknown[]) => ({ items, limit: 50, next_cursor: null, complete: true });
const ticket = (id: string, over: Partial<Ticket> = {}): Ticket => ({
  id, ticket_id: id, schema_version: '1.0', scope, created_at: T(1), updated_at: T(1), version: 2, kind: 'question', title: 'Вопрос ' + id,
  summary: 'Нужно', attempted: 'Поиск', decision_needed: 'Решить', state: 'waiting_reply', next_turn: 'operator', task_ids: [], materials: [],
  sources: [], agent_priority: 1, technical_refusal: false, duplicate_key: 'a'.repeat(64), occurrences: 1, message_count: 1,
  operator_read_version: 0, request: null, resolution: null, ...over,
} as Ticket);
const message = (ticketId: string, sequence: number, author: TicketMessage['author'] = 'agent'): TicketMessage => ({
  id: `${ticketId}.m${String(sequence).padStart(5, '0')}`, schema_version: '1.0', scope, created_at: T(sequence), ticket_id: ticketId,
  sequence, author, author_id: author === 'operator' ? 'operator' : 'agent-01', body: 'Текст ' + sequence, ticket_version: sequence,
} as TicketMessage);

test('needs-my-answer first, then work in progress, then finished', () => {
  const rows = [
    ticket('t-done', { state: 'resolved', next_turn: null, resolution: 'Готово.' }),
    ticket('t-agent', { state: 'in_progress', next_turn: 'agent' }),
    ticket('t-low', { agent_priority: 0, updated_at: T(5) }),
    ticket('t-high', { agent_priority: 3 }),
  ];
  const { mine, working, done } = groups(rows);
  assert.deepEqual(mine.map((row) => row.id), ['t-high', 't-low']);
  assert.deepEqual(working.map((row) => row.id), ['t-agent']);
  assert.deepEqual(done.map((row) => row.id), ['t-done']);
  assert.equal(turnLabel(rows[1]), 'Ход агента');
  assert.equal(unread(ticket('x')), true);
  assert.equal(unread(ticket('x', { operator_read_version: 2 })), false);
});

test('commands carry the seen version, a partition-only scope and never an author field', () => {
  const row = ticket('t-1', { version: 4 });
  const reply = replyCommand(runScope, row, '  Ответ  ', 'answer', 'req-1');
  assert.equal(reply.expected_version, 4);
  assert.deepEqual(reply.scope, scope);
  assert.deepEqual(reply.arguments.data, { ticket_id: 't-1', body: 'Ответ', outcome: 'answer' });
  assert.equal(readCommand(scope, row, 'req-2').type, 'ticket.read');
  assert.equal(replyProblem('   '), 'Напишите сообщение.');
  assert.match(String(replyProblem('x'.repeat(4001))), /4000/);
  assert.equal(replyProblem('ok'), null);
});

test('projections reject inconsistent turns, foreign scopes and ungrounded capability shapes', () => {
  assert.equal(tickets(page([ticket('t-1')]), scope).items.length, 1);
  assert.throws(() => tickets(page([ticket('t-1', { next_turn: 'agent' })]), scope));
  assert.throws(() => tickets(page([ticket('t-1', { state: 'resolved' })]), scope));
  assert.throws(() => tickets(page([ticket('t-1', { scope: { ...scope, memory_space_id: 'other' } })]), scope));
  assert.throws(() => tickets(page([ticket('t-1', { kind: 'capability_request' })]), scope));
  assert.equal(tickets(page([ticket('t-1', { kind: 'capability_request', request: { capability_id: 'web.fetch', areas: [], duration_seconds: null, constraints: '' } })]), scope).items.length, 1);
  assert.throws(() => tickets(page([ticket('t-1', { operator_read_version: 3 })]), scope));
  const rows = ticketMessages(page([message('t-1', 2, 'operator'), message('t-1', 1), message('t-2', 1)]), scope).items;
  assert.deepEqual(conversation(rows, 't-1').map((row) => row.sequence), [1, 2]);
  assert.throws(() => ticketMessages(page([{ ...message('t-1', 1), author: 'admin' }]), scope));
});

test('ticket.changed refreshes tickets and their messages', () => {
  assert.deepEqual(affectedResources['ticket.changed'], ['tickets', 'ticket-messages']);
  const changed = event({ id: 'e1', event_id: 'e1', type: 'ticket.changed', scope, resource_id: 't-1', resource_version: 3,
    occurred_at: T(2), created_at: T(2), causal_id: null, operation_id: null });
  assert.equal(changed.type, 'ticket.changed');
});

test('a permission decision names exact versions and is previewed before execution', async () => {
  const { grantPreview, revokePreview, executeDecision, decisionProblem, activeGrants } = await import('../src/ui/tickets/model.ts');
  const row = ticket('t-cap', { kind: 'capability_request', version: 5, request: { capability_id: 'reference.read', areas: ['reference'], duration_seconds: null, constraints: 'Только чтение.' } });
  const binding = { binding_id: 'b-1', capability_id: 'reference.read' } as never;
  const decision = { bindingId: 'b-1', level: 'prepare_only' as const, days: 7, areas: ['reference'], constraints: ' Только чтение. ' };
  assert.equal(decisionProblem({ ...decision, bindingId: '' }, [binding]), 'Выберите подключение.');
  assert.equal(decisionProblem({ ...decision, days: 400 }, [binding]), 'Срок — от 1 до 365 дней.');
  assert.equal(decisionProblem(decision, [binding]), null);
  const preview = grantPreview(runScope, row, decision, 'p-1');
  assert.equal(preview.type, 'permission.decision.preview');
  assert.equal(preview.expected_version, 5);
  assert.deepEqual(preview.scope, scope);
  assert.deepEqual(preview.arguments.data, { action: 'grant', capability_id: 'reference.read', binding_id: 'b-1', ticket_id: 't-cap',
    ticket_version: 5, level: 'prepare_only', duration_seconds: 7 * 86400, areas: ['reference'], constraints: 'Только чтение.' });
  const execute = executeDecision(scope, 'reference.read', 'plan-1', 'x-1');
  assert.deepEqual(execute.arguments, { plan_id: 'plan-1', plan_version: 1 });
  const permission = { id: 'perm-1', version: 2, capability_id: 'reference.read', binding_id: 'b-1', revoked_at: null, expires_at: T(50) } as never;
  assert.equal(revokePreview(scope, permission, 'r-1').expected_version, 2);
  assert.equal(activeGrants([permission], 'reference.read', Date.parse(T(10))).length, 1);
  assert.equal(activeGrants([permission], 'reference.read', Date.parse(T(55))).length, 0);
});

test('a conversation is read from its ticket id and stops at the first foreign message', async () => {
  const { nextConversationCursor } = await import('../src/ui/tickets/model.ts');
  const own = [message('t-1', 1), message('t-1', 2)];
  assert.equal(nextConversationCursor({ items: own, complete: false, next_cursor: own[1].id }, 't-1'), own[1].id);
  assert.equal(nextConversationCursor({ items: [...own, message('t-2', 1)], complete: false, next_cursor: 't-2.m00001' }, 't-1'), undefined);
  assert.equal(nextConversationCursor({ items: own, complete: true, next_cursor: null }, 't-1'), undefined);
  assert.equal(nextConversationCursor({ items: [], complete: false, next_cursor: 'x' }, 't-1'), undefined);
});
