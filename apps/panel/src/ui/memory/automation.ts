import type { Episode, MemoryCycle, PrivateApiStatus, RunSession, Scope, StateChange } from '../../shared/contracts.ts';
import { samePartition } from '../../shared/scope.ts';

export type Condition = { code: string; title: string; message: string; color: string };
export function automationCondition(scope: Scope, run: RunSession | undefined, budget: PrivateApiStatus | undefined, unavailable: boolean): Condition {
  if (scope.memory_mode === 'test') return { code: 'test', color: 'violet', title: 'Это тестовая область', message: 'Суточная обработка рабочей биографии здесь не запускается.' };
  if (unavailable || !budget) return { code: 'unavailable', color: 'orange', title: 'Состояние обработки не подтверждено', message: 'Обновите данные. Последнее полученное состояние не доказывает, что обработка сейчас работает.' };
  if (!budget.enabled || !budget.memory_processing) return { code: 'disabled', color: 'gray', title: 'Автоматическая обработка не включена', message: 'Разговоры сами по себе не включают обработку. Подключение настраивается отдельно при остановленном локальном сервере.' };
  if (budget.memory_calls_used === undefined || budget.memory_daily_calls === undefined) return { code: 'unavailable', color: 'orange', title: 'Состояние резерва не подтверждено', message: 'Обновите данные лимита памяти.' };
  if (!run || !samePartition(scope, run.scope)) return { code: 'no-run', color: 'orange', title: 'Нет состояния запуска в этой области', message: 'Проверьте выбранную область и обновите состояние запуска.' };
  if (run.state !== 'ACTIVE') return { code: 'stopped', color: 'gray', title: 'Обработка ждёт запуска', message: 'Нужен работающий агент. Если он остановлен, нажмите START вверху; после перезапуска запуск остаётся ручным.' };
  if (run.activity !== 'OPEN' || run.autonomy !== 'autonomous') return { code: 'restricted', color: 'gray', title: 'Обработка ждёт автономного режима', message: 'Агент должен работать в открытом автономном режиме. Проверьте режим в «Рабочем пространстве».' };
  if (budget.attempts_remaining === 0 || budget.memory_calls_used >= budget.memory_daily_calls) return { code: 'budget', color: 'orange', title: 'Обработка ждёт обновления лимита', message: 'Общий лимит API или суточный резерв памяти исчерпан. Необработанный опыт остаётся до следующего доступного цикла.' };
  return { code: 'eligible', color: 'teal', title: 'Автоматическая обработка включена', message: 'При свободном агенте проверяется подтверждённый опыт прошлых суток. Разговор имеет приоритет. Это готовность к обработке, а не подтверждение её завершения.' };
}

export function newestCycles(rows: MemoryCycle[]): MemoryCycle[] {
  return [...rows].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
}
export function cycleOutcomes(cycle: MemoryCycle, records: { claims: { id: string }[]; summaries: { id: string }[]; changes: { id: string }[] }) {
  const ids = new Set(cycle.record_ids);
  const known = new Set<string>();
  const count = (rows: { id: string }[]) => {
    const matches = new Set(rows.filter((r) => ids.has(r.id)).map((r) => r.id));
    for (const id of matches) known.add(id);
    return matches.size;
  };
  const claims = count(records.claims), summaries = count(records.summaries), changes = count(records.changes);
  return { claims, summaries, changes, unclassified: ids.size - known.size, total: ids.size };
}
export function cycleFailure(code: string | null | undefined): string {
  if (code === 'ACCESS_DENIED' || code === 'EXPIRED') return 'Проверьте доступ к источникам и срок разрешений. Отозванные данные не будут переданы повторно.';
  if (code === 'RESOURCE_BUSY') return 'Обработке не хватило доступных ресурсов или допуска. Проверьте лимиты и состояние операций.';
  if (code === 'STALE_CONTEXT' || code === 'VERSION_CONFLICT') return 'Во время обработки изменились условия или версии данных. Старый результат не применяется.';
  return 'Проверьте операцию и вызовы модели в разделах «Рабочее пространство» и «LLM и расходы». По этому коду нельзя точно определить ответ провайдера.';
}

// ---- backlog and safe continuation (the server decides; this only explains what it will do) ---------------
const sameBatch = (a: MemoryCycle, b: MemoryCycle) => a.episode_ids.length === b.episode_ids.length && a.episode_ids.every((id) => b.episode_ids.includes(id));
export const MAX_CYCLE_ATTEMPTS = 3;
export type Continuation = 'none' | 'continued' | 'will-continue' | 'limit' | 'unknown';
export function continuation(row: MemoryCycle, rows: MemoryCycle[]): Continuation {
  if (row.status === 'unknown') return 'unknown';
  if (row.status !== 'failed') return 'none';
  const same = rows.filter((other) => sameBatch(other, row));
  if (same.some((other) => Date.parse(other.created_at) > Date.parse(row.created_at))) return 'continued';
  return same.length >= MAX_CYCLE_ATTEMPTS ? 'limit' : 'will-continue';
}
const resumeStage: Record<MemoryCycle['stage'], string> = { queued: 'начала', extract: 'начала', consolidate: 'сводки', reflect: 'рефлексии', done: 'начала' };
export function continuationNote(row: MemoryCycle, rows: MemoryCycle[]): string | null {
  const kind = continuation(row, rows);
  if (kind === 'unknown') return 'Исход неизвестен: этот цикл не повторяется автоматически, чтобы не оплатить и не применить результат дважды.';
  if (kind === 'continued') return 'Этот сбой уже продолжен отдельным циклом ниже или выше в журнале.';
  if (kind === 'limit') return `Достигнут предел попыток для этой порции (${MAX_CYCLE_ATTEMPTS}). Автоматического повтора больше не будет.`;
  if (kind === 'will-continue') return `Порция продолжится автоматически с этапа ${resumeStage[row.stage]}: уже сохранённые этапы повторно не оплачиваются. Условия — известен исход прошлых вызовов модели, источники не изменились и хватает лимита.`;
  return null;
}
export type Verdict = 'applied' | 'rejected' | 'kept' | null;
export function cycleVerdict(row: MemoryCycle, changes: StateChange[]): Verdict {
  if (row.status !== 'completed') return null;
  const own = changes.filter((change) => (row.record_ids ?? []).includes(change.id));
  if (own.some((change) => change.status === 'APPLIED')) return 'applied';
  if (own.some((change) => change.status === 'REJECTED')) return 'rejected';
  return 'kept';
}
export const verdictLabels: Record<Exclude<Verdict, null>, string> = {
  applied: 'Рефлексия: изменение характера применено.',
  rejected: 'Рефлексия предложила изменение, проверки его отклонили; характер прежний.',
  kept: 'Рефлексия оставила характер прежним (или её изменения не найдены в загруженных записях).',
};
export type Backlog = { waiting: number; today: number; continuing: number; unknown: number; complete: boolean };
// Episodes of earlier UTC days not yet in any started cycle wait for the next batch (up to 10 at a time).
export function backlog(episodes: Episode[], rows: MemoryCycle[], now: number, complete: boolean): Backlog {
  const midnight = new Date(now); midnight.setUTCHours(0, 0, 0, 0);
  const started = new Set(rows.filter((row) => row.status !== 'queued').flatMap((row) => row.episode_ids));
  const fresh = episodes.filter((row) => !started.has(row.id));
  return {
    waiting: fresh.filter((row) => Date.parse(row.created_at) < midnight.getTime()).length,
    today: fresh.filter((row) => Date.parse(row.created_at) >= midnight.getTime()).length,
    continuing: rows.filter((row) => continuation(row, rows) === 'will-continue').length,
    unknown: rows.filter((row) => row.status === 'unknown').length,
    complete,
  };
}
export const MAX_BATCH_EPISODES = 10;
export function backlogNote(view: Backlog): string {
  const atLeast = view.complete ? '' : 'не менее ';
  if (!view.waiting && !view.today && !view.continuing) return 'Необработанного опыта нет: обрабатывать нечего, это не ошибка.';
  const parts = [`Ждут обработки эпизодов прошлых суток: ${atLeast}${view.waiting} (за раз — до ${MAX_BATCH_EPISODES}).`];
  if (view.today) parts.push(`Сегодняшних эпизодов: ${atLeast}${view.today} — они обрабатываются после смены суток.`);
  if (view.continuing) parts.push(`Порций, ждущих продолжения после сбоя: ${view.continuing}.`);
  if (view.unknown) parts.push(`Циклов с неизвестным исходом: ${view.unknown} — они не повторяются автоматически.`);
  parts.push('Эпизод, слишком большой для одной порции или без доступных оснований, будет ждать; причину пишет журнал процесса.');
  return parts.join(' ');
}
