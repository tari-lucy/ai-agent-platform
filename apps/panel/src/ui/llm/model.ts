import type {
  ConsequencePlan, CostCalculation, CreateCommand, ExecutePlanCommand, LLMAssignment, LLMAttempt, PlanCommand, Scope, Tariff,
} from '../../shared/contracts.ts';
import { addMoney, isMoney } from './money.ts';

// Pure LLM-usage logic mirroring the owner's projections: unknown is never zero, currencies never mix,
// subsets (cached, reasoning) are shown beside their parents and never added into them. No I/O.

export type Filters = { since: string; until: string; role: string; model: string; connection: string };
export const noFilters: Filters = { since: '', until: '', role: '', model: '', connection: '' };
export const modelOf = (attempt: LLMAttempt) => attempt.reported_model_id ?? attempt.requested_model_id;
export type ConnectionOf = (attempt: LLMAttempt) => string;
// The assignment pinned on the attempt tells which connection was really used; unknown is stated.
export function connectionResolver(assignments: LLMAssignment[]): ConnectionOf {
  const byId = new Map(assignments.map((item) => [item.id, item.connection_id] as [string, string]));
  return (attempt) => byId.get(attempt.assignment_id) ?? 'unknown';
}

export function select(attempts: LLMAttempt[], filters: Filters, connectionOf: ConnectionOf): LLMAttempt[] {
  const since = filters.since ? Date.parse(filters.since) : -Infinity;
  const until = filters.until ? Date.parse(filters.until) + 86400000 : Infinity;
  return attempts.filter((attempt) => {
    const at = Date.parse(attempt.started_at);
    return at >= since && at < until && (!filters.role || attempt.role_id === filters.role) &&
      (!filters.model || modelOf(attempt) === filters.model) && (!filters.connection || connectionOf(attempt) === filters.connection);
  });
}

export type Totals = {
  attempts: number; operations: number; byStatus: Record<string, number>;
  input: number; output: number; cached: number; reasoning: number; unknownUsage: number; partialUsage: number;
};
export const emptyTotals = (): Totals => ({ attempts: 0, operations: 0, byStatus: {}, input: 0, output: 0, cached: 0, reasoning: 0, unknownUsage: 0, partialUsage: 0 });
export const retries = (totals: Totals) => totals.attempts - totals.operations;
export const usageComplete = (totals: Totals) => totals.unknownUsage === 0 && totals.partialUsage === 0;
function add(totals: Totals, attempt: LLMAttempt, seen: Set<string>) {
  totals.attempts += 1;
  if (!seen.has(attempt.operation_id)) { seen.add(attempt.operation_id); totals.operations += 1; }
  totals.byStatus[attempt.status] = (totals.byStatus[attempt.status] ?? 0) + 1;
  const usage = attempt.usage;
  if (usage.completeness === 'unknown' || usage.source === 'unknown') totals.unknownUsage += 1;
  else if (usage.completeness === 'partial') totals.partialUsage += 1;
  totals.input += usage.input_tokens ?? 0; totals.output += usage.output_tokens ?? 0;
  totals.cached += usage.cached_input_tokens ?? 0; totals.reasoning += usage.reasoning_output_tokens ?? 0;
}

export type CostRow = { currency: string; amount: string; complete: number; partial: number };
export type Summary = {
  totals: Totals; byRole: Record<string, Totals>; byModel: Record<string, Totals>; byConnection: Record<string, Totals>;
  cost: CostRow[]; unknownCost: number; complete: boolean;
};
export function summarise(attempts: LLMAttempt[], calculations: CostCalculation[], connectionOf: ConnectionOf): Summary {
  const costs = new Map(calculations.map((item) => [item.attempt_id, item] as [string, CostCalculation]));
  const out: Summary = { totals: emptyTotals(), byRole: {}, byModel: {}, byConnection: {}, cost: [], unknownCost: 0, complete: true };
  const seen = new Map<object, Set<string>>();
  const bucket = (table: Record<string, Totals>, key: string) => (table[key] ??= emptyTotals());
  for (const attempt of attempts) {
    for (const totals of [out.totals, bucket(out.byRole, attempt.role_id), bucket(out.byModel, modelOf(attempt)), bucket(out.byConnection, connectionOf(attempt))]) {
      add(totals, attempt, seen.get(totals) ?? seen.set(totals, new Set()).get(totals)!);
    }
    const cost = costs.get(attempt.attempt_id);
    // No calculation, an unknown one or one without a currency is "unknown", never a free request.
    if (!cost || cost.completeness === 'unknown' || cost.currency === null || cost.amount === null) { out.unknownCost += 1; continue; }
    let row = out.cost.find((item) => item.currency === cost.currency);
    if (!row) { row = { currency: cost.currency, amount: '0', complete: 0, partial: 0 }; out.cost.push(row); }
    row.amount = addMoney(row.amount, cost.amount);
    if (cost.completeness === 'complete') row.complete += 1; else row.partial += 1;
  }
  out.cost.sort((a, b) => a.currency.localeCompare(b.currency));
  out.complete = usageComplete(out.totals) && out.unknownCost === 0 && out.cost.every((row) => row.partial === 0);
  return out;
}
// What is missing, in words, so a known sum never looks like a full bill.
export function gaps(summary: Summary, loadedAll: boolean): string[] {
  const list: string[] = [];
  if (!loadedAll) list.push('Загружена не вся история: итоги неполные.');
  if (summary.totals.unknownUsage) list.push(`Usage неизвестен у ${summary.totals.unknownUsage} попыток: токены занижены.`);
  if (summary.totals.partialUsage) list.push(`Usage неполный у ${summary.totals.partialUsage} попыток.`);
  if (summary.unknownCost) list.push(`Цена неизвестна у ${summary.unknownCost} попыток: сумма — не полный счёт.`);
  for (const row of summary.cost) if (row.partial) list.push(`${row.currency}: ${row.partial} расчётов частичные.`);
  return list;
}

// ---- history --------------------------------------------------------------------------------------
export const statusLabels: Record<LLMAttempt['status'], string> = {
  running: 'Идёт', completed: 'Завершена', failed: 'Ошибка', cancelled: 'Отменена', incomplete: 'Неполная', unknown: 'Исход неизвестен',
};
export function usageLabel(attempt: LLMAttempt): string {
  const { source, completeness } = attempt.usage;
  if (source === 'unknown' || completeness === 'unknown') return 'Usage неизвестен';
  return `${source === 'provider' ? 'Провайдер' : 'Локальная оценка'}${completeness === 'partial' ? ', неполный' : ''}`;
}
export type HistoryRow = {
  id: string; at: string; role: string; requested: string; reported: string | null; routed: boolean; status: string;
  usage: string; tokens: string; latencyMs: number | null; tariff: string; cost: string; connection: string;
};
export function historyRows(attempts: LLMAttempt[], calculations: CostCalculation[], connectionOf: ConnectionOf): HistoryRow[] {
  const costs = new Map(calculations.map((item) => [item.attempt_id, item] as [string, CostCalculation]));
  return [...attempts].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at) || b.id.localeCompare(a.id)).map((attempt) => {
    const cost = costs.get(attempt.attempt_id);
    const { input_tokens: input, output_tokens: output } = attempt.usage;
    return {
      id: attempt.attempt_id, at: attempt.started_at, role: attempt.role_id, requested: attempt.requested_model_id,
      reported: attempt.reported_model_id ?? null,
      // The provider may route elsewhere than asked: the difference is shown, not hidden.
      routed: !!attempt.reported_model_id && attempt.reported_model_id !== attempt.requested_model_id,
      status: statusLabels[attempt.status], usage: usageLabel(attempt),
      tokens: input === null && output === null ? '—' : `${input ?? '?'} / ${output ?? '?'}`,
      latencyMs: attempt.completed_at ? Date.parse(attempt.completed_at) - Date.parse(attempt.started_at) : null,
      tariff: attempt.tariff_id ? `${attempt.tariff_id} v${attempt.tariff_version}` : 'тариф не закреплён',
      cost: !cost || cost.amount === null || cost.currency === null ? 'неизвестно' : `${cost.amount} ${cost.currency}${cost.completeness === 'partial' ? ' (частично)' : ''}`,
      connection: connectionOf(attempt),
    };
  });
}

// ---- analytics -------------------------------------------------------------------------------------
export type Day = { day: string; tokens: number; unknown: number };
// Known tokens per UTC day, with the unknown attempts counted beside them, never as zero tokens.
export function perDay(attempts: LLMAttempt[]): Day[] {
  const days = new Map<string, Day>();
  for (const attempt of attempts) {
    const key = attempt.started_at.slice(0, 10);
    const row = days.get(key) ?? { day: key, tokens: 0, unknown: 0 };
    row.tokens += (attempt.usage.input_tokens ?? 0) + (attempt.usage.output_tokens ?? 0);
    if (attempt.usage.source === 'unknown' || attempt.usage.completeness === 'unknown') row.unknown += 1;
    days.set(key, row);
  }
  return [...days.values()].sort((a, b) => a.day.localeCompare(b.day));
}
export type Share = { key: string; tokens: number; percent: number };
export function shares(table: Record<string, Totals>): Share[] {
  const rows = Object.entries(table).map(([key, totals]) => ({ key, tokens: totals.input + totals.output }));
  const sum = rows.reduce((total, row) => total + row.tokens, 0);
  return rows.map((row) => ({ ...row, percent: sum ? Math.round((row.tokens / sum) * 1000) / 10 : 0 })).sort((a, b) => b.tokens - a.tokens || a.key.localeCompare(b.key));
}
export function costPerDay(attempts: LLMAttempt[], calculations: CostCalculation[]): Record<string, { day: string; amount: string }[]> {
  const costs = new Map(calculations.map((item) => [item.attempt_id, item] as [string, CostCalculation]));
  const table: Record<string, Map<string, string>> = {};
  for (const attempt of attempts) {
    const cost = costs.get(attempt.attempt_id);
    if (!cost || cost.completeness === 'unknown' || cost.amount === null || cost.currency === null) continue;
    const days = (table[cost.currency] ??= new Map());
    const key = attempt.started_at.slice(0, 10);
    days.set(key, addMoney(days.get(key) ?? '0', cost.amount));
  }
  return Object.fromEntries(Object.entries(table).map(([currency, days]) => [currency, [...days.entries()].map(([day, amount]) => ({ day, amount })).sort((a, b) => a.day.localeCompare(b.day))]));
}

// ---- tariffs ----------------------------------------------------------------------------------------
export type TariffGroup = { key: string; connection: string; model: string; versions: Tariff[]; current: Tariff | null };
// A price change is a new version; the one in force is the latest already effective.
export function groupTariffs(tariffs: Tariff[], now: number): TariffGroup[] {
  const groups = new Map<string, TariffGroup>();
  for (const tariff of tariffs) {
    const key = tariff.connection_id + '\u0000' + tariff.model_id;
    const group = groups.get(key) ?? { key, connection: tariff.connection_id, model: tariff.model_id, versions: [], current: null };
    group.versions.push(tariff); groups.set(key, group);
  }
  for (const group of groups.values()) {
    group.versions.sort((a, b) => Date.parse(b.effective_at) - Date.parse(a.effective_at) || b.version - a.version);
    group.current = group.versions.find((item) => Date.parse(item.effective_at) <= now) ?? null;
  }
  return [...groups.values()].sort((a, b) => a.key.localeCompare(b.key));
}
export const rateText = (rate: string | null | undefined, tariff: Tariff) => (rate == null ? 'не задана' : `${rate} ${tariff.currency} / ${tariff.unit_tokens} ток.`);
export type TariffDraft = { connection: string; model: string; unit: string; currency: string; input: string; output: string; cached: string; reasoning: string; effective: string };
export const emptyDraft: TariffDraft = { connection: '', model: '', unit: '1000000', currency: '', input: '', output: '', cached: '', reasoning: '', effective: '' };
export function validTariff(draft: TariffDraft, now: number): string | null {
  if (!draft.connection) return 'Выберите подключение.';
  if (!/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(draft.model)) return 'Укажите точный ID модели.';
  const unit = Number(draft.unit);
  if (!Number.isSafeInteger(unit) || unit < 1 || unit > 1_000_000_000) return 'Единица — целое число токенов от 1 до 1 000 000 000.';
  if (!/^[A-Z]{3}$/.test(draft.currency)) return 'Валюта — три заглавные латинские буквы (например USD).';
  for (const [label, value] of [['входа', draft.input], ['выхода', draft.output], ['cached input', draft.cached], ['reasoning', draft.reasoning]] as const) {
    if (value !== '' && !isMoney(value)) return `Ставка ${label} — неотрицательное десятичное число.`;
  }
  // An empty rate means "not set", never zero; with no rate at all there is nothing to price.
  if (draft.input === '' && draft.output === '') return 'Задайте ставку входа или выхода: пустая цена — это «не задана», а не ноль.';
  if (!Number.isFinite(Date.parse(draft.effective))) return 'Укажите дату начала действия.';
  if (Date.parse(draft.effective) - now > 366 * 86400000) return 'Дата начала не может быть дальше года.';
  return null;
}
export const backdated = (draft: TariffDraft, now: number) => Number.isFinite(Date.parse(draft.effective)) && Date.parse(draft.effective) < now;
// Historical repricing has no connected owner route yet.
export const LLM_SCHEMA = { tariff: 'tariff.create-args', recalculate: 'llm.recalculate' } as const;
export function tariffCommand(scope: Scope, target: string, draft: TariffDraft, requestId: string, previousVersion = 0): CreateCommand {
  const data: Record<string, string | number> = { previous_version: previousVersion, connection_id: draft.connection, model_id: draft.model, unit_tokens: Number(draft.unit), currency: draft.currency, effective_at: new Date(draft.effective).toISOString() };
  if (draft.input !== '') data.input_rate = draft.input;
  if (draft.output !== '') data.output_rate = draft.output;
  if (draft.cached !== '') data.cached_input_rate = draft.cached;
  if (draft.reasoning !== '') data.reasoning_output_rate = draft.reasoning;
  return { type: 'tariff.create', request_id: requestId, scope, target, arguments: { schema_id: LLM_SCHEMA.tariff, data } };
}
export function recalcPreview(scope: Scope, target: string, tariff: Tariff, requestId: string): PlanCommand {
  return { type: 'llm.recalculate.preview', request_id: requestId, scope, target, expected_version: tariff.version,
    arguments: { schema_id: LLM_SCHEMA.recalculate, data: { tariff_id: tariff.id, tariff_version: tariff.version } } };
}
export function recalcExecute(scope: Scope, target: string, plan: ConsequencePlan, requestId: string): ExecutePlanCommand {
  return { type: 'llm.recalculate.execute', request_id: requestId, scope, target, expected_version: plan.version, arguments: { plan_id: plan.id, plan_version: plan.version } };
}
// The plan must name the tariff the operator chose, and must still be in force.
export function recalcProblem(plan: ConsequencePlan, tariff: Tariff, now: number): string | null {
  if (!plan.dependencies.some((dep) => dep.resource_id === tariff.id)) return 'План не содержит выбранный тариф. Подготовьте его заново.';
  if (Date.parse(plan.expires_at) <= now) return 'Срок плана истёк. Подготовьте его заново.';
  return null;
}
// ---- roles --------------------------------------------------------------------------------------------
export type RoleRow = { role: string; assignment: LLMAssignment; fallback: LLMAssignment | null; attempts: number };
export function roleRows(assignments: LLMAssignment[], byRole: Record<string, Totals>): RoleRow[] {
  const byId = new Map(assignments.map((item) => [item.id, item] as [string, LLMAssignment]));
  // An assignment named as someone's fallback is a reserve, not the role's own binding.
  const reserves = new Set(assignments.map((item) => item.fallback_assignment_id).filter((value): value is string => !!value));
  const primaries = assignments.filter((item) => !reserves.has(item.id));
  const latest = new Map<string, LLMAssignment>();
  for (const item of primaries.length ? primaries : assignments) { const known = latest.get(item.role_id); if (!known || item.version > known.version) latest.set(item.role_id, item); }
  return [...latest.values()].sort((a, b) => a.role_id.localeCompare(b.role_id)).map((assignment) => ({
    role: assignment.role_id, assignment, fallback: assignment.fallback_assignment_id ? byId.get(assignment.fallback_assignment_id) ?? null : null,
    attempts: byRole[assignment.role_id]?.attempts ?? 0,
  }));
}
