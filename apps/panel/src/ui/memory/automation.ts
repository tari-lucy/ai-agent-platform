import type { MemoryCycle, PrivateApiStatus, RunSession, Scope } from '../../shared/contracts.ts';
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
