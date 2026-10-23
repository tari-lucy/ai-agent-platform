import type { CapabilityBinding, CapabilityManifest, EditCommand, ExecutePlanCommand, Permission, PermissionDecisionArguments, PlanCommand, Scope, Ticket, TicketMessage } from '../../shared/contracts.ts';

// Pure ticket logic: grouping, labels, turns and the two operator commands. No I/O.
// A ticket is a conversation; an answer in words never grants a right or starts the agent.
export const kindLabels: Record<Ticket['kind'], string> = {
  problem: 'Проблема', question: 'Вопрос', idea: 'Идея', capability_request: 'Запрос доступа',
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
// Message ids are `<ticket id>.mNNNNN` and the owner lists by id, so a read that starts after the
// ticket id returns exactly this ticket's messages first. Stop at the first foreign message.
export function nextConversationCursor(page: { items: TicketMessage[]; complete: boolean; next_cursor: string | null }, ticketId: string): string | undefined {
  if (page.complete || !page.next_cursor || !page.items.length) return undefined;
  return page.items.every((row) => row.ticket_id === ticketId) ? page.next_cursor : undefined;
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

// ---- structured permission decision (preview → execute, workspace owner) -------------------------------
// The form names an exact ticket version, capability, binding, level, duration and areas; the server
// checks the host ceiling. Words in the conversation are never parsed into a grant.
export type Level = 'prepare_only' | 'operator_decision' | 'automatic';
export const levelLabels: Record<Level, string> = {
  prepare_only: 'Только подготовка', operator_decision: 'С решением оператора', automatic: 'Автоматически',
};
export type Decision = { bindingId: string; level: Level; days: number; areas: string[]; constraints: string };
export function decisionProblem(decision: Decision, bindings: CapabilityBinding[]): string | null {
  if (!bindings.some((row) => row.binding_id === decision.bindingId)) return 'Выберите подключение.';
  if (!Number.isInteger(decision.days) || decision.days < 1 || decision.days > 365) return 'Срок — от 1 до 365 дней.';
  if (decision.constraints.length > 4000) return 'Ограничения длиннее 4000 символов.';
  return null;
}
export const grantPreview = (scope: Scope, ticket: Ticket, decision: Decision, requestId: string): PlanCommand => {
  const data: PermissionDecisionArguments = {
    action: 'grant', capability_id: ticket.request!.capability_id, binding_id: decision.bindingId, ticket_id: ticket.id,
    ticket_version: ticket.version, level: decision.level, duration_seconds: decision.days * 86400, areas: decision.areas as PermissionDecisionArguments['areas'],
    constraints: decision.constraints.trim(),
  };
  return { type: 'permission.decision.preview', request_id: requestId, scope: area(scope), target: data.capability_id,
    expected_version: ticket.version, arguments: { schema_id: 'permission.decision-args', data } };
};
export const revokePreview = (scope: Scope, permission: Permission, requestId: string): PlanCommand => ({
  type: 'permission.decision.preview', request_id: requestId, scope: area(scope), target: permission.capability_id,
  expected_version: permission.version, arguments: { schema_id: 'permission.decision-args', data: {
    action: 'revoke', capability_id: permission.capability_id, binding_id: permission.binding_id,
    permission_id: permission.id, permission_version: permission.version,
  } },
});
export const executeDecision = (scope: Scope, capabilityId: string, planId: string, requestId: string): ExecutePlanCommand => ({
  type: 'permission.decision.execute', request_id: requestId, scope: area(scope), target: capabilityId, expected_version: 1,
  arguments: { plan_id: planId, plan_version: 1 },
});
export const activeGrants = (rows: Permission[], capabilityId: string, now: number) =>
  rows.filter((row) => row.capability_id === capabilityId && row.revoked_at == null && Date.parse(row.expires_at) > now);

// ---- plain operator actions: answer, finish, allow, refuse ---------------------------------------------
// The operator never picks internal states or types identifiers: they come from current records.
export function statusText(ticket: Ticket): string {
  if (ticket.state === 'resolved') return 'Завершено';
  if (ticket.state === 'closed_unfulfilled') return ticket.kind === 'capability_request' ? 'Закрыто без выдачи доступа' : 'Закрыто без выполнения';
  return ticket.next_turn === 'operator' ? 'Ждёт вашего ответа' : 'Агент работает над этим';
}
export const FINISH_NOTE = 'Оператор завершил обращение.';
export const REFUSE_NOTE = 'Оператор отказал в доступе.';
export const answerCommand = (scope: Scope, ticket: Ticket, body: string, requestId: string) =>
  replyCommand(scope, ticket, body, 'answer', requestId);
// Finishing is a closing word, never a grant: the ticket owner records it as resolved.
export const finishCommand = (scope: Scope, ticket: Ticket, note: string, requestId: string) =>
  replyCommand(scope, ticket, note.trim() || FINISH_NOTE, 'resolve', requestId);
// Refusing closes the request without fulfilment; no Permission is written.
export const refuseCommand = (scope: Scope, ticket: Ticket, note: string, requestId: string) =>
  replyCommand(scope, ticket, note.trim() || REFUSE_NOTE, 'close', requestId);
export const noteProblem = (note: string) => note.length > MAX_REPLY ? `Комментарий длиннее ${MAX_REPLY} символов.` : null;

// The connection a grant is pinned to: a verified one first, then one still being prepared
// (the grant then waits for it); never a revoked one. Newest verification first.
export function proposedBinding(rows: CapabilityBinding[], capabilityId: string): CapabilityBinding | undefined {
  const usable = rows.filter((row) => row.capability_id === capabilityId && row.state !== 'revoked');
  const rank = (row: CapabilityBinding) => (row.state === 'ready' ? 0 : 1);
  return [...usable].sort((a, b) => rank(a) - rank(b) ||
    Date.parse(b.verified_at ?? b.updated_at) - Date.parse(a.verified_at ?? a.updated_at) || a.id.localeCompare(b.id))[0];
}
// What the agent asked for, within what the capability allows. Areas outside the manifest are
// dropped; no requested areas means the capability's own. A public effect is never automatic.
export function proposedDecision(ticket: Ticket, manifest: CapabilityManifest, binding: CapabilityBinding | undefined): Decision {
  const request = ticket.request!;
  const allowed = (manifest.areas ?? []) as string[];
  const asked = (request.areas ?? []).filter((item) => allowed.includes(item));
  const days = request.duration_seconds ? Math.min(365, Math.max(1, Math.ceil(request.duration_seconds / 86400))) : 7;
  return {
    bindingId: binding?.binding_id ?? '', level: manifest.effect === 'public_output' ? 'operator_decision' : 'automatic',
    days, areas: asked.length ? asked : [...allowed], constraints: request.constraints,
  };
}
// Why an action failed, and what to do next, in the operator's words.
export function actionProblem(code: string | undefined, fallback: string): string {
  if (code === 'VERSION_CONFLICT') return 'Обращение изменилось, пока вы его читали. Обновите карточку и проверьте ещё раз — ваш текст сохранён.';
  if (code === 'ACCESS_DENIED') return 'Такое решение выходит за потолок, разрешённый на этом host (уровень или срок), или вне областей возможности. '
    + 'Уменьшите срок или уровень в «Подробностях решения» и попробуйте снова.';
  if (code === 'CAPABILITY_UNAVAILABLE') return 'Решения по доступу на этом host выключены. Ответьте агенту текстом или откажите.';
  return fallback;
}
