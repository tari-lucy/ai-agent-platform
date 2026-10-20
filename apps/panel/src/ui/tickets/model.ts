import type { EditCommand, Scope, Ticket, TicketMessage } from '../../shared/contracts.ts';

// Pure ticket logic: grouping, labels, turns and the two operator commands. No I/O.
// A ticket is a conversation; an answer in words never grants a right or starts the agent.
export const kindLabels: Record<Ticket['kind'], string> = {
  problem: 'Проблема', question: 'Вопрос', idea: 'Идея', capability_request: 'Запрос возможности',
};
export const stateLabels: Record<Ticket['state'], string> = {
  waiting_reply: 'Ждёт ответа', in_progress: 'В работе', resolved: 'Решён', closed_unfulfilled: 'Закрыт без выполнения',
};
export const partyLabels: Record<TicketMessage['author'], string> = { operator: 'Оператор', agent: 'Агент', service: 'Служба' };
export const priorityLabels = ['не срочно', 'обычный', 'важно', 'срочно'] as const;
export type Outcome = 'answer' | 'resolve' | 'close';
export const outcomeLabels: Record<Outcome, string> = { answer: 'Ответить', resolve: 'Отметить решённым', close: 'Закрыть без выполнения' };

export const terminal = (ticket: Ticket) => ticket.state === 'resolved' || ticket.state === 'closed_unfulfilled';
export const unread = (ticket: Ticket) => ticket.operator_read_version < ticket.version;
export function turnLabel(ticket: Ticket): string {
  if (ticket.next_turn === null) return 'Завершён';
  return ticket.next_turn === 'operator' ? 'Нужен ваш ответ' : ticket.next_turn === 'agent' ? 'Ход агента' : 'Ход службы';
}

// "Needs my answer" first, then work in progress, then finished; newest change first inside each group.
export function groups(rows: Ticket[]): { mine: Ticket[]; working: Ticket[]; done: Ticket[] } {
  const order = (list: Ticket[]) => [...list].sort((a, b) =>
    (b.agent_priority - a.agent_priority) || Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
  return {
    mine: order(rows.filter((row) => row.next_turn === 'operator')),
    working: order(rows.filter((row) => !terminal(row) && row.next_turn !== 'operator')),
    done: order(rows.filter(terminal)),
  };
}
export function conversation(rows: TicketMessage[], ticketId: string): TicketMessage[] {
  return rows.filter((row) => row.ticket_id === ticketId).sort((a, b) => a.sequence - b.sequence);
}

export const MAX_REPLY = 4000;
export function replyProblem(body: string): string | null {
  if (!body.trim()) return 'Напишите сообщение.';
  if (body.length > MAX_REPLY) return `Сообщение длиннее ${MAX_REPLY} символов.`;
  return null;
}
// Tickets belong to the stable partition, never to one run.
const area = (scope: Scope): Scope => ({
  workspace_id: scope.workspace_id, persona_id: scope.persona_id, memory_space_id: scope.memory_space_id, memory_mode: scope.memory_mode,
});
export const replyCommand = (scope: Scope, ticket: Ticket, body: string, outcome: Outcome, requestId: string): EditCommand => ({
  type: 'ticket.reply', request_id: requestId, scope: area(scope), target: ticket.id, expected_version: ticket.version,
  arguments: { schema_id: 'ticket.reply-args', data: { ticket_id: ticket.id, body: body.trim(), outcome } },
});
export const readCommand = (scope: Scope, ticket: Ticket, requestId: string): EditCommand => ({
  type: 'ticket.read', request_id: requestId, scope: area(scope), target: ticket.id, expected_version: ticket.version,
  arguments: { schema_id: 'ticket.read-args', data: { ticket_id: ticket.id } },
});
