import type {
  Claim, ConsequencePlan, EditCommand, Episode, ExecutePlanCommand, OperationProjection, PlanCommand, Receipt,
  Scope, Source, SourceRef, Summary,
} from '../../shared/contracts.ts';

// Pure memory-screen logic: cards, filters, retention text, corrections and plan flow. No I/O.

export type Kind = 'source' | 'episode' | 'claim' | 'summary';
export const kindLabels: Record<Kind, string> = { source: 'Источник', episode: 'Эпизод', claim: 'Утверждение', summary: 'Сводка' };
export type Category = 'observation' | 'message' | 'hypothesis' | 'interpretation' | 'canon' | 'other';
export const categoryLabels: Record<Category, string> = {
  observation: 'Наблюдение', message: 'Чужое сообщение', hypothesis: 'Гипотеза', interpretation: 'Интерпретация',
  canon: 'Канон', other: 'Другое',
};
export type Availability = 'available' | 'expired' | 'deleted' | 'unavailable';
export const availabilityLabels: Record<Availability, string> = {
  available: 'Доступно', expired: 'Срок хранения истёк', deleted: 'Удалено', unavailable: 'Недоступно',
};

export type Card = {
  key: string; kind: Kind; id: string; version: number | null; at: string; title: string;
  text: string | null; category: Category; confidence: number | null; conflicts: string[];
  basis: SourceRef[]; people: string[]; availability: Availability;
  source: Source | null;
};

const provenanceCategory = (provenance: string): Category =>
  provenance === 'observation' || provenance === 'simulation' ? 'observation' :
    provenance === 'third_party_message' || provenance === 'operator_input' ? 'message' :
      provenance === 'model_proposal' ? 'hypothesis' : provenance === 'interpretation' ? 'interpretation' : 'other';
const knowledgeCategory = (knowledge: Claim['knowledge_type']): Category =>
  knowledge === 'observed' ? 'observation' : knowledge === 'reported' ? 'message' : knowledge;

// A derived record is as available as its basis. Missing basis is stated, not papered over.
export function basisAvailability(refs: SourceRef[]): Availability {
  if (refs.some((ref) => ref.availability === 'available')) return 'available';
  if (refs.some((ref) => ref.availability === 'expired')) return 'expired';
  if (refs.some((ref) => ref.availability === 'deleted')) return 'deleted';
  return 'unavailable';
}
export function basisSummary(refs: SourceRef[]): string {
  const count = (state: Availability) => refs.filter((ref) => ref.availability === state).length;
  const parts = (['available', 'expired', 'deleted', 'unavailable'] as const)
    .filter((state) => count(state) > 0).map((state) => `${count(state)} · ${availabilityLabels[state].toLowerCase()}`);
  return parts.length ? parts.join('; ') : 'Основание не указано';
}

export function sourceCard(source: Source): Card {
  const readable = source.availability === 'available' && source.read_allowed && source.content != null;
  return {
    key: 'source:' + source.id, kind: 'source', id: source.id, version: source.version, at: source.occurred_at,
    title: source.source_type, text: readable ? source.content! : null, category: provenanceCategory(source.provenance),
    confidence: source.identity_confidence ?? null, conflicts: [], basis: [], people: source.author_id ? [source.author_id] : [],
    availability: source.availability, source,
  };
}
export function episodeCard(episode: Episode): Card {
  return {
    key: 'episode:' + episode.id, kind: 'episode', id: episode.id, version: null, at: episode.created_at,
    title: episode.choice, text: `${episode.choice} → ${episode.actual_outcome}`, category: provenanceCategory(episode.provenance),
    confidence: null, conflicts: [], basis: episode.sources, people: episode.participant_ids,
    availability: basisAvailability(episode.sources), source: null,
  };
}
export function claimCard(claim: Claim): Card {
  return {
    key: 'claim:' + claim.id, kind: 'claim', id: claim.id, version: claim.version, at: claim.updated_at,
    title: claim.subject_id, text: claim.content, category: knowledgeCategory(claim.knowledge_type), confidence: claim.confidence,
    conflicts: claim.conflict_ids ?? [], basis: claim.sources, people: [claim.subject_id],
    availability: basisAvailability(claim.sources), source: null,
  };
}
export function summaryCard(summary: Summary): Card {
  return {
    key: 'summary:' + summary.id, kind: 'summary', id: summary.id, version: summary.version, at: summary.period_end,
    title: 'Сводка периода', text: summary.content, category: 'interpretation', confidence: null, conflicts: [],
    basis: summary.sources, people: [], availability: basisAvailability(summary.sources), source: null,
  };
}
export const buildCards = (input: { sources: Source[]; episodes: Episode[]; claims: Claim[]; summaries: Summary[] }): Card[] =>
  [...input.sources.map(sourceCard), ...input.episodes.map(episodeCard), ...input.claims.map(claimCard),
    ...input.summaries.map(summaryCard)].sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.key.localeCompare(b.key));

// Text of an unavailable record is never shown, even if a stale copy arrived with it.
export const visibleText = (card: Card): string | null =>
  card.kind === 'source' ? card.text : card.availability === 'available' || card.availability === 'expired' ? card.text : null;

export type Filters = {
  kind: Kind | ''; category: Category | ''; availability: Availability | ''; person: string; from: string; to: string; query: string;
};
export const noFilters: Filters = { kind: '', category: '', availability: '', person: '', from: '', to: '', query: '' };
// Searches only what is loaded. It is a filter, not the server recall, and says so in the UI.
export function filterCards(cards: Card[], filters: Filters): Card[] {
  const from = filters.from ? Date.parse(filters.from) : -Infinity;
  const to = filters.to ? Date.parse(filters.to) + 86400000 : Infinity;
  const query = filters.query.trim().toLowerCase();
  const person = filters.person.trim().toLowerCase();
  return cards.filter((card) => (!filters.kind || card.kind === filters.kind) &&
    (!filters.category || card.category === filters.category) &&
    (!filters.availability || card.availability === filters.availability) &&
    (!person || card.people.some((name) => name.toLowerCase().includes(person))) &&
    Date.parse(card.at) >= from && Date.parse(card.at) < to &&
    (!query || (visibleText(card) ?? '').toLowerCase().includes(query) || card.title.toLowerCase().includes(query)));
}

const DAY = 86400000;
export function retentionText(source: Source | null, now: number): string {
  if (!source) return 'Срок определяется источниками основания.';
  const { expires_at, protected: guarded } = source.retention;
  if (guarded) return 'Защищено: хранится до явного решения.';
  if (expires_at === null) return 'Без срока.';
  const left = Date.parse(expires_at) - now;
  if (source.availability === 'expired' || left <= 0) return 'Срок хранения истёк; полный текст удалён.';
  return `Хранится до ${new Date(expires_at).toLocaleDateString('ru-RU')} (осталось ${Math.ceil(left / DAY)} дн.)`;
}
export const rightsText = (source: Source) => [
  `Чтение: ${source.read_allowed ? 'да' : 'нет'}`,
  `Передача в API: ${source.api_transfer_allowed ? 'да' : 'нет'}`,
  `Публичное раскрытие: ${source.public_disclosure_allowed ? 'да' : 'нет'}`,
];
export function conflictsOf(card: Card, all: Card[]): Card[] {
  return card.conflicts.map((id) => all.find((other) => other.kind === 'claim' && other.id === id)).filter((x): x is Card => !!x);
}

// ---- corrections ----------------------------------------------------------------------------
// ASSUMPTION (owner decision pending): wire schema ids of memory payloads are not defined yet.
export const MEMORY_SCHEMA = {
  correct: 'memory.correct', delete: 'memory.delete', export: 'memory.export',
} as const;
export type Correction = { content: string; confidence: string; reason: string };
export function validCorrection(card: Card, input: Correction): string | null {
  if (card.kind !== 'claim' && card.kind !== 'summary') return 'Исправлять можно утверждения и сводки.';
  if (card.version === null) return 'Запись неизменяема.';
  if (!input.reason.trim()) return 'Укажите основание исправления.';
  if (input.reason.length > 1000) return 'Основание длиннее 1000 символов.';
  const changesText = input.content.trim() !== '' && input.content !== (card.text ?? '');
  const lowers = input.confidence !== '';
  if (!changesText && !lowers) return 'Измените текст или снизьте уверенность.';
  if (input.content.length > 4000) return 'Текст длиннее 4000 символов.';
  if (lowers) {
    const value = Number(input.confidence);
    if (!Number.isFinite(value) || value < 0 || value > 1) return 'Уверенность — число от 0 до 1.';
    // Raising certainty needs new evidence, not an edit.
    if (card.kind === 'claim' && card.confidence !== null && value >= card.confidence) return 'Уверенность можно только снизить.';
    if (card.kind !== 'claim') return 'Уверенность есть только у утверждений.';
  }
  return null;
}
export function correctCommand(scope: Scope, target: string, card: Card, input: Correction, requestId: string): EditCommand {
  const data: Record<string, string | number> = { kind: card.kind, record_id: card.id, reason: input.reason.trim() };
  if (input.content.trim() !== '' && input.content !== (card.text ?? '')) data.content = input.content;
  if (input.confidence !== '') data.confidence = Number(input.confidence);
  return { type: 'memory.correct', request_id: requestId, scope, target, expected_version: card.version!,
    arguments: { schema_id: MEMORY_SCHEMA.correct, data } };
}

// ---- consequence plans: preview → explicit confirmation → execution -----------------------------
export type Operation = 'delete' | 'export';
export function previewCommand(scope: Scope, target: string, card: Card, operation: Operation, requestId: string): PlanCommand {
  return { type: operation === 'delete' ? 'memory.delete.preview' : 'memory.export.preview', request_id: requestId, scope, target,
    expected_version: card.version ?? 1, arguments: { schema_id: MEMORY_SCHEMA[operation], data: { source_ids: [card.id] } } };
}
export function executeCommand(scope: Scope, target: string, plan: ConsequencePlan, operation: Operation, requestId: string): ExecutePlanCommand {
  return { type: operation === 'delete' ? 'memory.delete.execute' : 'memory.export.execute', request_id: requestId, scope, target,
    expected_version: plan.version, arguments: { plan_id: plan.id, plan_version: plan.version } };
}
// The plan must name the very record the operator chose; otherwise the screen refuses to offer execution.
export function planProblem(plan: ConsequencePlan, card: Card, now: number): string | null {
  if (!plan.dependencies.some((dep) => dep.resource_id === card.id)) return 'План не содержит выбранную запись. Подготовьте его заново.';
  if (Date.parse(plan.expires_at) <= now) return 'Срок плана истёк. Подготовьте его заново.';
  return null;
}
export function dependencyCount(plan: ConsequencePlan, card: Card): number {
  return plan.dependencies.filter((dep) => dep.resource_id !== card.id).length;
}
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
