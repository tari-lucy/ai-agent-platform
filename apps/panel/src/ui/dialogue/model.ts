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
  if (utterance.audience_id === 'operator-panel' && utterance.channel_ids.length === 1 && utterance.channel_ids[0] === 'operator-panel' && utterance.operation_id) {
    const delivered = rows.some((r) => r.utterance_id === utterance.utterance_id && r.revision === utterance.revision && r.channel_id === 'operator-panel' && r.status === 'DELIVERED');
    return delivered ? { status: 'done', detail: 'Приватная панель подтвердила получение.' } :
      { status: 'saved', detail: 'Приватный ответ сохранён. Получение ещё не подтверждено.' };
  }
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

// ---- message path: accepted → processing → ready, or an explained failure ---------------------
// Derived only from core data: the receipt, the operation projection and the stored reply. A
// missing projection row means "not yet known", never "failed" or "done".
export type Stage = 'accepted' | 'processing' | 'ready' | 'failed' | 'cancelled' | 'unknown';
export type Progress = { stage: Stage; label: string; detail: string };
export function messageProgress(receipt: Receipt, operations: OperationProjection[], utterances: Utterance[]): Progress {
  const id = receipt.operation_id ?? null;
  if (id !== null && utterances.some((row) => row.operation_id === id)) {
    return { stage: 'ready', label: 'Ответ готов', detail: 'Ядро сохранило ответ персонажа.' };
  }
  const row = id === null ? undefined : operations.find((item) => item.operation_id === id);
  if (!row) return { stage: 'accepted', label: 'Принято', detail: 'Ядро приняло сообщение. Статус обработки уточняется.' };
  switch (row.status) {
    case 'RUNNING': return { stage: 'processing', label: 'Обрабатывается', detail: 'Персонаж готовит ответ.' };
    case 'SUCCEEDED': return { stage: 'processing', label: 'Обработка завершена', detail: 'Ответ ещё не получен панелью.' };
    case 'FAILED': return { stage: 'failed', label: 'Ошибка обработки', detail: 'Ответа нет. Сообщение сохранено; панель не отправит его повторно сама.' };
    case 'CANCELLED': return { stage: 'cancelled', label: 'Обработка отменена', detail: 'Ответ не будет создан для этого сообщения.' };
    case 'OUTCOME_UNKNOWN': return { stage: 'unknown', label: 'Исход неизвестен', detail: 'Ядро не знает, получен ли ответ. Автоматического повтора нет.' };
    case 'EXPIRED': return { stage: 'unknown', label: 'Срок обработки истёк', detail: 'Ответ мог не быть создан. Автоматического повтора нет.' };
    default: return { stage: 'accepted', label: 'Принято', detail: 'Сообщение ждёт своей очереди.' };
  }
}

// ---- delivery confirmation: only a final reply really shown in a visible tab -------------------
export type AckFacts = {
  visibility: string; ratio: number; mode: Scope['memory_mode']; runActive: boolean; runOpen: boolean;
  sameRun: boolean; entryKey: string; row: Utterance | undefined; shownText: string | null; newestRevision: boolean;
  delivered: boolean; streaming: boolean; readsHealthy: boolean;
};
export const VISIBLE_RATIO = 0.6;
export function mayAcknowledge(f: AckFacts): boolean {
  return f.visibility === 'visible' && f.ratio >= VISIBLE_RATIO && f.mode === 'work' && f.runActive && f.runOpen &&
    f.sameRun && !!f.row && f.entryKey === 'out:' + f.row.utterance_id && f.shownText === f.row.content &&
    f.newestRevision && !f.delivered && !f.streaming && f.readsHealthy &&
    f.row.channel_ids.length === 1 && f.row.channel_ids[0] === 'operator-panel' &&
    ['draft', 'approved'].includes(f.row.moderation_status);
}
