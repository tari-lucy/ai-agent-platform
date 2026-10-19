import { Alert, Badge, Button, Group, Paper, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { Link } from 'react-router';
import type { MemoryCycle, RunSession, Scope, StateChange } from '../../shared/contracts.ts';
import { failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import { automationCondition, backlog, backlogNote, continuationNote, cycleFailure, cycleOutcomes, cycleVerdict, newestCycles, verdictLabels } from './automation.ts';

type Records = { claims: { id: string }[]; summaries: { id: string }[]; incomplete: boolean };
const stages = { queued: 'Ожидание', extract: 'Извлечение утверждений', consolidate: 'Сводка', reflect: 'Рефлексия', done: 'Обработка завершена' };
const statuses = { queued: 'В очереди', running: 'Выполняется', completed: 'Завершён', failed: 'Ошибка', unknown: 'Исход неизвестен' };

function Cycle({ row, rows, records, changes }: { row: MemoryCycle; rows: MemoryCycle[]; records: Records; changes: StateChange[] }) {
  const result = cycleOutcomes(row, { ...records, changes });
  const failed = row.status === 'failed' || row.status === 'unknown';
  const verdict = cycleVerdict(row, changes);
  const next = continuationNote(row, rows);
  return <Stack gap={4}>
    <Group gap="xs"><Badge color={failed ? 'orange' : row.status === 'completed' ? 'teal' : 'gray'} variant="light">{statuses[row.status]}</Badge>
      <Text size="sm">{formatTime(row.updated_at)} · {stages[row.stage]}</Text></Group>
    <Text size="sm">Эпизодов: {row.episode_ids.length}. Результатов в журнале: {result.total}.</Text>
    {result.total > 0 && <Text size="sm">В загруженных записях найдено: утверждений — {result.claims}, сводок — {result.summaries}, предложений развития — {result.changes}.</Text>}
    {result.unclassified > 0 && <Text size="sm" c="dimmed">Ещё {result.unclassified} результатов не сопоставлены с доступными записями. Они могут быть вне загруженной части или уже недоступны.</Text>}
    {verdict && <Text size="sm">{verdictLabels[verdict]}</Text>}
    {row.status === 'completed' && <Text size="sm" c="dimmed">Отсутствие новых утверждений само по себе не означает ошибку.</Text>}
    {failed && <Alert color="orange" title={row.status === 'unknown' ? 'Исход цикла не подтверждён' : 'Цикл не завершён'}>
      <Text size="sm">Этап: {stages[row.stage]}. Код: {row.error_code}. {cycleFailure(row.error_code)}</Text>
      {next && <Text size="sm" mt={4}>{next}</Text>}
      <Text size="sm" mt={4}>Кнопки «повторить» нет: продолжение решает ядро по сохранённому исходу. Обновление страницы только перечитывает состояние.</Text>
    </Alert>}
  </Stack>;
}

export function MemoryAutomation({ api, scope, run, runUnavailable, records }: {
  api: PanelApi; scope: Scope; run?: RunSession; runUnavailable: boolean; records: Records;
}) {
  const enabled = scope.memory_mode === 'work';
  const cycles = useQuery({ queryKey: projectionKey('memory-cycles', scope), queryFn: ({ signal }) => api.memoryCycles(scope, signal), enabled, refetchInterval: 15000 });
  const budget = useQuery({ queryKey: projectionKey('private-api-status', scope), queryFn: ({ signal }) => api.privateApiStatus(scope, signal), enabled, refetchInterval: 15000 });
  const changes = useQuery({ queryKey: projectionKey('changes', scope), queryFn: ({ signal }) => api.changes(scope, signal), enabled, refetchInterval: 15000 });
  const episodes = useQuery({ queryKey: [...projectionKey('episodes', scope), 'backlog'], queryFn: ({ signal }) => api.episodes(scope, signal), enabled, refetchInterval: 15000 });
  const condition = automationCondition(scope, run, budget.data, runUnavailable || budget.isError);
  const rows = newestCycles(cycles.data?.items ?? []);
  const refresh = () => { void cycles.refetch(); void budget.refetch(); void changes.refetch(); void episodes.refetch(); };
  const changeRows = changes.isError ? [] : changes.data?.items ?? [];
  const waiting = episodes.data && cycles.data ? backlog(episodes.data.items, cycles.data.items, episodes.dataUpdatedAt, episodes.data.complete && cycles.data.complete) : null;
  return <Paper component="section" withBorder p="md" radius="lg" aria-label="Обработка памяти и рефлексия">
    <Stack gap="sm">
      <Group justify="space-between"><Title order={2} size="h4">Обработка памяти и рефлексия</Title>
        {enabled && <Button variant="subtle" size="compact-xs" onClick={refresh}>Обновить состояние памяти</Button>}</Group>
      <Alert color={condition.color} title={condition.title}><Text size="sm">{condition.message}</Text></Alert>
      {enabled && <>
        <Text size="sm">После 00:00 UTC (03:00 по Москве) агент обрабатывает подтверждённые разговоры прошлых суток: выделяет утверждения, делает сводку и проводит рефлексию.
          Чтобы ответ вошёл в опыт, он должен быть показан и подтверждён в диалоге. Если агент был остановлен при смене суток, после START в автономном режиме он подхватит необработанный опыт прошлых дней. Пустая смена суток не создаёт опыт. Точное время запуска зависит от занятости и лимитов.</Text>
        {budget.isError ? <Alert color="orange">Не удалось обновить настройки: {failureMessage(budget.error)}</Alert> : budget.data && <Text size="sm" c="dimmed">
          Проверено: {formatTime(budget.data.checked_at)}.
          {budget.data.memory_processing && <> Вызовов памяти сегодня: {budget.data.memory_calls_used} из {budget.data.memory_daily_calls}; резерв входит в общий лимит API.
            {budget.data.resets_at && <> Следующая смена суток: {formatTime(budget.data.resets_at)} (ваше местное время).</>}</>}
        </Text>}
        <Paper withBorder p="sm" aria-label="Необработанный опыт"><Text fw={600} size="sm">Необработанный опыт</Text>
          <Text size="sm">{waiting ? backlogNote(waiting) : episodes.isError ? 'Список эпизодов недоступен: очередь не определена.' : 'Считаем очередь…'}</Text></Paper>
        {cycles.isError && <Alert color="orange">Журнал сейчас недоступен: {failureMessage(cycles.error)} Сохранённые ниже данные могут быть устаревшими.</Alert>}
        {cycles.isPending ? <Text size="sm" role="status">Получаем журнал обработки…</Text> : rows.length > 0 ? <>
          <Text fw={600} size="sm">Последний полученный цикл</Text>
          <Cycle row={rows[0]} rows={rows} records={records} changes={changeRows} />
          {rows.length > 1 && <details><summary>Предыдущие полученные циклы ({rows.length - 1})</summary>
            <Stack gap="md" mt="sm">{rows.slice(1).map((row) => <Cycle key={row.id} row={row} rows={rows} records={records} changes={changeRows} />)}</Stack>
          </details>}
        </> : !cycles.isError && <Text size="sm">Циклов пока нет. Свежий разговор может ждать следующей смены суток; отсутствие циклов не означает, что опыт потерян.</Text>}
        {cycles.data && !cycles.data.complete && <Text size="sm" c="orange.8">Получена только часть журнала: самый новый цикл во всём хранилище не определён.</Text>}
        {(records.incomplete || changes.isError || changes.isPending || changes.data && !changes.data.complete) && <Text size="xs" c="dimmed">Список результатов неполон. Нули выше относятся только к найденным в загруженных записях результатам.</Text>}
        <Group gap="md"><Link to="/development">Посмотреть развитие</Link><Link to="/workspace">Проверить операции</Link><Link to="/llm">Проверить вызовы API</Link></Group>
      </>}
    </Stack>
  </Paper>;
}
