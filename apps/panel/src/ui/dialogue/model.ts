import type {
  Briefing, CreateCommand, Delivery, EditCommand, MessageCommand, OperationProjection, Receipt,
  RunSession, Scope, Source, Utterance,
} from '../../shared/contracts.ts';

// Pure dialogue logic: timeline, statuses, composer state and command builders. No I/O.

export type Status = 'sending' | 'saved' | 'generating' | 'preview' | 'done' | 'cancelled' | 'unknown' | 'failed' | 'removed';
export const statusLabels: Record<Status, string> = {
  sending: 'Отправляется', saved: 'Сохранено', generating: 'Генерируется', preview: 'Предварительно',
  done: 'Завершено', cancelled: 'Отменено / не отправлено', unknown: 'Исход неизвестен',
  failed: 'Ошибка доставки', removed: 'Текст недоступен',
};
export type Entry = {
  key: string; kind: 'incoming' | 'reply'; at: string; author: string; text: string | null;
  status: Status; detail: string;
};

const inboundKinds = new Set(['operator_input', 'third_party_message']);

// Only the newest revision of an utterance is the reply; older revisions are history.
export function latestRevisions(items: Utterance[]): Utterance[] {
  const newest = new Map<string, Utterance>();
  for (const item of items) {
    const known = newest.get(item.utterance_id);
    if (!known || item.revision > known.revision) newest.set(item.utterance_id, item);
  }
  return [...newest.values()];
}

// Delivery state is per channel. One bad or unknown channel must not look like success.
export function deliveryStatus(utterance: Utterance, rows: Delivery[]): { status: Status; detail: string } {
  if (utterance.moderation_status === 'blocked') return { status: 'cancelled', detail: 'Модерация закрыла выпуск.' };
  if (utterance.moderation_status === 'draft') return { status: 'preview', detail: 'Черновик, ещё не проверен.' };
  if (utterance.moderation_status === 'checking') return { status: 'preview', detail: 'Проверяется модератором.' };
  const mine = latestAttempts(rows.filter((row) => row.utterance_id === utterance.utterance_id &&
    row.revision === utterance.revision));
  const states = mine.map((row) => row.status);
  if (!states.length) return { status: 'saved', detail: 'Одобрено, доставка ещё не начата.' };
  if (states.includes('UNKNOWN')) return { status: 'unknown', detail: 'Результат доставки неизвестен хотя бы в одном канале.' };
  if (states.includes('FAILED') || states.includes('PARTIAL')) return { status: 'failed', detail: 'Доставка прошла не полностью.' };
  if (states.includes('SENDING') || states.includes('QUEUED')) return { status: 'generating', detail: 'Доставляется.' };
  if (states.every((state) => state === 'CANCELLED')) return { status: 'cancelled', detail: 'Доставка отменена.' };
  return { status: 'done', detail: 'Доставлено.' };
}
// A newer attempt of one channel supersedes older ones; an unrelated channel keeps its own row.
function latestAttempts(rows: Delivery[]): Delivery[] {
  const newest = new Map<string, Delivery>();
  for (const row of rows) {
    const known = newest.get(row.channel_id);
    if (!known || Date.parse(row.created_at) >= Date.parse(known.created_at)) newest.set(row.channel_id, row);
  }
  return [...newest.values()];
}

export function buildTimeline(sources: Source[], utterances: Utterance[], rows: Delivery[]): Entry[] {
  const entries: Entry[] = [];
  for (const source of sources) {
    if (!inboundKinds.has(source.provenance)) continue;
    const available = source.availability === 'available' && source.content != null;
    entries.push({
      key: 'in:' + source.id, kind: 'incoming', at: source.occurred_at, author: source.author_id ?? 'неизвестный автор',
      text: available ? source.content! : null, status: available ? 'saved' : 'removed',
      detail: available ? 'Сообщение сохранено в памяти.' : 'Срок хранения истёк или текст удалён.',
    });
  }
  for (const item of latestRevisions(utterances)) {
    const { status, detail } = deliveryStatus(item, rows);
    entries.push({ key: 'out:' + item.utterance_id, kind: 'reply', at: item.created_at, author: 'Персонаж', text: item.content, status, detail });
  }
  return entries.sort((a, b) => Date.parse(a.at) - Date.parse(b.at) || a.key.localeCompare(b.key));
}

const openOperation = new Set(['PROPOSED', 'AWAITING_APPROVAL', 'READY', 'QUEUED', 'RUNNING', 'WAITING']);
export function openOperations(rows: OperationProjection[]): OperationProjection[] {
  return rows.filter((row) => openOperation.has(row.status));
}
// "Генерируется" is shown only from an owner-confirmed open operation, never from our own guess.
export function generating(rows: OperationProjection[]): boolean {
  return rows.some((row) => row.status === 'RUNNING' || row.status === 'QUEUED');
}

export type Goal = { headline: string; reasons: string[]; pending: string[] };
const stateText = { STOPPED: 'Агент остановлен', STARTING: 'Агент запускается', ACTIVE: 'Агент работает',
  STOPPING: 'Агент останавливается', FAULTED: 'Требуется проверка запуска' } as const;
export function goal(run: RunSession | undefined, operationRows: OperationProjection[]): Goal {
  const open = openOperations(operationRows);
  return {
    headline: run ? stateText[run.state] : 'Состояние запуска недоступно',
    // The core exposes no goal text yet: say so instead of inventing one.
    reasons: ['Цель и основание не переданы ядром.'],
    pending: open.map((row) => `${row.operation_id}: ${row.waiting_reason ?? row.status}`),
  };
}

// ---- composer: one command at a time; an unknown outcome keeps the same request id -----------
export type Composer =
  | { phase: 'idle' }
  | { phase: 'sending'; command: MessageCommand }
  | { phase: 'unknown'; command: MessageCommand }
  | { phase: 'saved'; receipt: Receipt }
  | { phase: 'rejected'; code: string };
export function messageCommand(run: RunSession, text: string, requestId: string): MessageCommand {
  return { type: 'dialogue.message', request_id: requestId, scope: run.scope, target: run.session_id,
    arguments: { text } };
}
export function canSend(run: RunSession | undefined, text: string, phase: Composer['phase']): { ok: boolean; reason: string } {
  if (!run) return { ok: false, reason: 'Нет подтверждённого запуска в этой области.' };
  if (run.state !== 'ACTIVE') return { ok: false, reason: 'Агент не работает: сообщение не будет обработано.' };
  if (phase === 'sending' || phase === 'unknown') return { ok: false, reason: 'Предыдущая отправка ещё не завершена.' };
  if (!text.trim()) return { ok: false, reason: 'Введите текст сообщения.' };
  if (text.length > 4000) return { ok: false, reason: 'Сообщение длиннее 4000 символов.' };
  return { ok: true, reason: '' };
}
export function afterSend(command: MessageCommand, outcome: { receipt: Receipt } | { code: string }): Composer {
  if ('receipt' in outcome) {
    return outcome.receipt.status === 'rejected' ? { phase: 'rejected', code: 'ACCESS_DENIED' } :
      { phase: 'saved', receipt: outcome.receipt };
  }
  // Timeout/network/server loss: the command may have been saved. Never forget it, never new id.
  return ['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(outcome.code) ?
    { phase: 'unknown', command } : { phase: 'rejected', code: outcome.code };
}

// ---- briefings --------------------------------------------------------------------------------
// ASSUMPTION (owner decision pending): the wire schema ids of briefing payloads are not defined by
// the backend yet. They are named here once so the contract owner can replace them in one place.
export const BRIEFING_SCHEMA = { create: 'briefing.create', cancel: 'briefing.cancel' } as const;
export const briefingTypes = { context: 'Контекст', canon_proposal: 'Предложение по канону', scenario: 'Сценарий' } as const;
export const briefingStatuses = { active: 'Действует', cancelled: 'Отменена', expired: 'Истекла' } as const;
export function createBriefing(scope: Scope, target: string, kind: Briefing['type'], content: string, expiresAt: string, requestId: string): CreateCommand {
  return { type: 'briefing.create', request_id: requestId, scope, target,
    arguments: { schema_id: BRIEFING_SCHEMA.create, data: { type: kind, content, expires_at: expiresAt } } };
}
export function cancelBriefing(scope: Scope, target: string, row: Briefing, requestId: string): EditCommand {
  return { type: 'briefing.cancel', request_id: requestId, scope, target, expected_version: row.version,
    arguments: { schema_id: BRIEFING_SCHEMA.cancel, data: { briefing_id: row.id } } };
}
export function validBriefing(content: string, expiresAt: string, now: number): string | null {
  if (!content.trim()) return 'Введите текст вводной.';
  if (content.length > 4000) return 'Вводная длиннее 4000 символов.';
  const when = Date.parse(expiresAt);
  if (!Number.isFinite(when)) return 'Укажите срок действия.';
  if (when <= now) return 'Срок должен быть в будущем.';
  if (when - now > 366 * 86400000) return 'Срок не может превышать года.';
  return null;
}
