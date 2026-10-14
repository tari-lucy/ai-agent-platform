import { formatTime } from '../time.ts';
import { Alert, Badge, Button, Group, Paper, Select, SimpleGrid, Stack, Table, Tabs, Text, TextInput, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { RunSession, RunSessionPage, Scope, Tariff } from '../../shared/contracts.ts';
import { failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { PlanFlow, type PlanSpec } from '../plan/PlanFlow.tsx';
import {
  backdated, connectionResolver, costPerDay, emptyDraft, gaps, groupTariffs, historyRows, noFilters, perDay, rateText, recalcExecute,
  recalcPreview, recalcProblem, retries, roleRows, select, shares, summarise, tariffCommand, validTariff, type Filters, type TariffDraft,
} from './model.ts';

const when = (value: string) => formatTime(value, 'medium');
const number = new Intl.NumberFormat('ru-RU');

// First page, then the server cursor on demand. Keys extend the shared projection key so events refresh them.
function usePaged<T extends { items: unknown[]; complete: boolean; next_cursor: string | null }>(
  scope: Scope, resource: string, read: (cursor: string | undefined, signal: AbortSignal) => Promise<T>,
) {
  return useInfiniteQuery({
    queryKey: [...projectionKey(resource, scope), 'dashboard'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => read(pageParam, signal),
    getNextPageParam: (last) => (last.complete || !last.next_cursor ? undefined : last.next_cursor),
  });
}

// An accessible bar chart: the data is also given as a table next to it, colour is never the only carrier.
function Bars({ title, rows, unit }: { title: string; rows: { label: string; value: number; note?: string }[]; unit: string }) {
  const max = Math.max(1, ...rows.map((row) => row.value));
  const height = 22; const width = 420;
  return <figure style={{ margin: 0 }}>
    <svg role="img" aria-label={title} viewBox={`0 0 ${width} ${rows.length * height + 4}`} width="100%" style={{ maxWidth: width }}>
      <title>{title}</title>
      {rows.map((row, index) => <g key={row.label}>
        <rect x={110} y={index * height + 2} width={Math.max(2, (row.value / max) * (width - 190))} height={height - 6} fill="var(--mantine-color-blue-5)" />
        <text x={0} y={index * height + 15} fontSize="11">{row.label}</text>
        <text x={116 + (row.value / max) * (width - 190)} y={index * height + 15} fontSize="11">{number.format(row.value)} {unit}</text>
      </g>)}
    </svg>
    <details><summary>Таблица данных</summary><Table><Table.Tbody>{rows.map((row) => <Table.Tr key={row.label}>
      <Table.Td>{row.label}</Table.Td><Table.Td>{number.format(row.value)} {unit}</Table.Td><Table.Td>{row.note ?? ''}</Table.Td></Table.Tr>)}</Table.Tbody></Table></details>
  </figure>;
}

function TariffEditor({ api, run, connections, onSaved }: { api: PanelApi; run: RunSession | undefined; connections: { value: string; label: string }[]; onSaved: () => void }) {
  const [draft, setDraft] = useState<TariffDraft>(emptyDraft);
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [now] = useState(() => Date.now());
  const set = (patch: Partial<TariffDraft>) => setDraft({ ...draft, ...patch });
  async function save() {
    const invalid = validTariff(draft, Date.now());
    if (invalid || !run) { setProblem(invalid ?? 'Нет подтверждённого запуска в этой области.'); return; }
    setBusy(true); setProblem(null); setSaved(false);
    try { await api.command(tariffCommand(run.scope, run.session_id, draft, crypto.randomUUID())); setSaved(true); setDraft(emptyDraft); onSaved(); }
    catch (error) { setProblem(failureMessage(error)); }
    finally { setBusy(false); }
  }
  return <Paper withBorder p="md" radius="lg" aria-label="Новая версия тарифа"><Title order={3} size="h5" mb="xs">Новая версия тарифа</Title>
    <SimpleGrid cols={{ base: 1, sm: 2 }} spacing="xs">
      <Select label="Подключение" data={connections} value={draft.connection || null} onChange={(v) => set({ connection: v ?? '' })} />
      <TextInput label="ID модели" value={draft.model} onChange={(e) => set({ model: e.currentTarget.value.trim() })} />
      <TextInput label="Единица (токенов)" inputMode="numeric" value={draft.unit} onChange={(e) => set({ unit: e.currentTarget.value.trim() })} />
      <TextInput label="Валюта" placeholder="USD" value={draft.currency} onChange={(e) => set({ currency: e.currentTarget.value.trim().toUpperCase() })} maxLength={3} />
      <TextInput label="Вход (за единицу)" inputMode="decimal" value={draft.input} onChange={(e) => set({ input: e.currentTarget.value.trim() })} />
      <TextInput label="Выход (за единицу)" inputMode="decimal" value={draft.output} onChange={(e) => set({ output: e.currentTarget.value.trim() })} />
      <TextInput label="Cached input (необязательно)" inputMode="decimal" value={draft.cached} onChange={(e) => set({ cached: e.currentTarget.value.trim() })} />
      <TextInput label="Reasoning output (необязательно)" inputMode="decimal" value={draft.reasoning} onChange={(e) => set({ reasoning: e.currentTarget.value.trim() })} />
    </SimpleGrid>
    <div style={{ marginTop: 8 }}><label htmlFor="tariff-effective"><Text size="sm" fw={500}>Действует с (ваше время)</Text></label>
      <input id="tariff-effective" type="datetime-local" value={draft.effective} onChange={(e) => set({ effective: e.currentTarget.value })} /></div>
    <Text size="xs" c="dimmed" mt="xs">Пустая ставка означает «не задана», а не ноль. Изменение цены — новая версия; прошлые расчёты не переписываются.</Text>
    {backdated(draft, now) && <Alert color="orange" mt="xs">Дата в прошлом: прежние расчёты не изменятся, пока вы не выполните отдельный пересчёт.</Alert>}
    <Group justify="flex-end" mt="xs"><Button onClick={() => void save()} disabled={busy}>Сохранить тариф</Button></Group>
    {problem && <Alert color="orange" mt="xs" role="alert">{problem}</Alert>}
    {saved && <Alert color="teal" mt="xs" role="status">Тариф принят. Он применяется к будущим вызовам.</Alert>}
  </Paper>;
}

function Recalc({ api, scope, run, tariff }: { api: PanelApi; scope: Scope; run: RunSession; tariff: Tariff }) {
  const spec: PlanSpec = {
    label: 'Пересчёт', previewLabel: 'Подготовить пересчёт по этому тарифу', executeLabel: 'Выполнить пересчёт', danger: false,
    note: 'Пересчёт — отдельная явная операция: он показывает область и происхождение оценки и создаёт новые версии расчётов, не стирая прежние.',
    preview: (requestId) => recalcPreview(run.scope, run.session_id, tariff, requestId),
    execute: (plan, requestId) => recalcExecute(run.scope, run.session_id, plan, requestId),
    problem: (plan, now) => recalcProblem(plan, tariff, now), related: (plan) => plan.dependencies.filter((dep) => dep.resource_id !== tariff.id).length,
    refresh: ['cost-calculations', 'tariffs'],
  };
  return <PlanFlow key={tariff.id} api={api} scope={scope} spec={spec} />;
}

export function LlmScreen({ api, scope }: { api: PanelApi; scope: Scope }) {
  const cache = useQueryClient();
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = (runs.data as RunSessionPage | undefined)?.items[0];
  const attemptsQuery = usePaged(scope, 'llm-attempts', (cursor, signal) => api.llmAttempts(scope, signal, cursor));
  const costsQuery = usePaged(scope, 'cost-calculations', (cursor, signal) => api.costCalculations(scope, signal, cursor));
  const tariffsQuery = useQuery({ queryKey: projectionKey('tariffs', scope), queryFn: ({ signal }) => api.tariffs(scope, signal) });
  const assignmentsQuery = useQuery({ queryKey: projectionKey('llm-assignments', scope), queryFn: ({ signal }) => api.llmAssignments(scope, signal) });
  const connectionsQuery = useQuery({ queryKey: projectionKey('connections', scope), queryFn: ({ signal }) => api.connections(scope, signal) });
  const [filters, setFilters] = useState<Filters>(noFilters);
  const [now] = useState(() => Date.now());
  const attempts = attemptsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const calculations = costsQuery.data?.pages.flatMap((page) => page.items) ?? [];
  const assignments = assignmentsQuery.data?.items ?? [];
  const connectionOf = connectionResolver(assignments);
  const chosen = select(attempts, filters, connectionOf);
  const summary = summarise(chosen, calculations, connectionOf);
  const loadedAll = !attemptsQuery.hasNextPage && !costsQuery.hasNextPage;
  const missing = gaps(summary, loadedAll);
  const names = new Map((connectionsQuery.data?.items ?? []).map((item) => [item.id, item.name] as [string, string]));
  const connectionName = (id: string) => names.get(id) ?? id;
  const set = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch });
  const roles = [...new Set(attempts.map((item) => item.role_id))].sort();
  const models = [...new Set(attempts.map((item) => item.reported_model_id ?? item.requested_model_id))].sort();
  const connectionIds = [...new Set(attempts.map(connectionOf))].sort();
  const failed = [attemptsQuery, costsQuery, tariffsQuery, assignmentsQuery].filter((query) => query.isError);
  const groups = groupTariffs(tariffsQuery.data?.items ?? [], now);
  const rows = historyRows(chosen, calculations, connectionOf);
  const days = perDay(chosen);
  const perCurrency = costPerDay(chosen, calculations);
  return <Stack gap="lg">
    <div><Text className="eyebrow">07 / LLM и расходы</Text><Title order={1}>LLM и расходы</Title></div>
    {scope.memory_mode === 'test' && <Alert color="violet" title="Тестовая память">Статистика этой области — тестовая; реальные API-вызовы лаборатории входят в общий расход.</Alert>}
    {!!failed.length && <Alert color="orange" title="Часть данных недоступна">{failureMessage(failed[0].error)} Итоги могут быть неполными.</Alert>}
    <Paper withBorder p="md" radius="lg" aria-label="Полнота статистики" role="status">
      <Group justify="space-between"><Text fw={600}>Полнота статистики</Text>
        <Badge color={missing.length ? 'orange' : 'teal'} variant="light">{missing.length ? 'Неполная' : 'Полная по загруженному'}</Badge></Group>
      {missing.map((line) => <Text key={line} size="sm" c="orange.8">{line}</Text>)}
      {(attemptsQuery.hasNextPage || costsQuery.hasNextPage) && <Button mt="xs" size="compact-sm" variant="light"
        onClick={() => { void attemptsQuery.fetchNextPage(); void costsQuery.fetchNextPage(); }}>Загрузить ещё</Button>}
    </Paper>
    <Paper withBorder p="md" radius="lg"><Group align="flex-end" gap="sm">
      <TextInput label="С даты" type="date" value={filters.since} onChange={(e) => set({ since: e.currentTarget.value })} />
      <TextInput label="По дату" type="date" value={filters.until} onChange={(e) => set({ until: e.currentTarget.value })} />
      <Select label="Роль" clearable data={roles} value={filters.role || null} onChange={(v) => set({ role: v ?? '' })} />
      <Select label="Модель" clearable data={models} value={filters.model || null} onChange={(v) => set({ model: v ?? '' })} />
      <Select label="Подключение" clearable data={connectionIds.map((id) => ({ value: id, label: connectionName(id) }))} value={filters.connection || null} onChange={(v) => set({ connection: v ?? '' })} />
      <Button variant="subtle" color="gray" onClick={() => setFilters(noFilters)}>Сбросить</Button></Group>
      <Text size="xs" c="dimmed" mt="xs">Фильтр по сессии появится, когда попытки будут связаны с сессией. Итоги считаются по загруженным попыткам.</Text></Paper>
    <Tabs defaultValue="overview"><Tabs.List>
      <Tabs.Tab value="overview">Обзор</Tabs.Tab><Tabs.Tab value="roles">Модели и роли</Tabs.Tab><Tabs.Tab value="prices">Цены</Tabs.Tab>
      <Tabs.Tab value="history">История</Tabs.Tab><Tabs.Tab value="analytics">Аналитика</Tabs.Tab></Tabs.List>
      <Tabs.Panel value="overview" pt="md"><Stack gap="md">
        <SimpleGrid cols={{ base: 2, sm: 4 }}>
          <Paper withBorder p="sm"><Text size="xs" c="dimmed">Запросы / попытки</Text><Text fw={700} size="xl">{summary.totals.operations} / {summary.totals.attempts}</Text></Paper>
          <Paper withBorder p="sm"><Text size="xs" c="dimmed">Повторы</Text><Text fw={700} size="xl">{retries(summary.totals)}</Text></Paper>
          <Paper withBorder p="sm"><Text size="xs" c="dimmed">Токены: вход / выход</Text><Text fw={700} size="xl">{number.format(summary.totals.input)} / {number.format(summary.totals.output)}</Text>
            <Text size="xs" c="dimmed">в т.ч. cached {number.format(summary.totals.cached)}, reasoning {number.format(summary.totals.reasoning)}</Text></Paper>
          <Paper withBorder p="sm"><Text size="xs" c="dimmed">Отказы / отмены / неизвестно</Text>
            <Text fw={700} size="xl">{summary.totals.byStatus.failed ?? 0} / {summary.totals.byStatus.cancelled ?? 0} / {summary.totals.byStatus.unknown ?? 0}</Text></Paper>
        </SimpleGrid>
        <Paper withBorder p="md" radius="lg" aria-label="Расходы по валютам"><Title order={3} size="h5">Расходы по валютам</Title>
          {!summary.cost.length ? <Text size="sm" c="dimmed">Известной стоимости нет. Это не нулевой расход: тарифа или usage может не быть.</Text> :
            summary.cost.map((row) => <Group key={row.currency} justify="space-between"><Text fw={600}>{row.amount} {row.currency}</Text>
              <Text size="xs" c="dimmed">полных {row.complete}, частичных {row.partial}</Text></Group>)}
          {summary.unknownCost > 0 && <Text size="sm" c="orange.8">Цена неизвестна у {summary.unknownCost} попыток — в суммы не входят.</Text>}
          <Text size="xs" c="dimmed" mt="xs">Валюты не складываются. Это расчётная оценка по тарифу, а не счёт агрегатора.</Text></Paper>
      </Stack></Tabs.Panel>
      <Tabs.Panel value="roles" pt="md"><Paper withBorder p="md" radius="lg"><Title order={3} size="h5" mb="xs">Роли и назначения</Title>
        {!roleRows(assignments, summary.byRole).length ? <Text size="sm" c="dimmed">Назначений нет. Без назначенной модели реальная генерация выключена.</Text> :
          <Table aria-label="Назначения ролей"><Table.Thead><Table.Tr><Table.Th>Роль</Table.Th><Table.Th>Модель</Table.Th><Table.Th>Подключение</Table.Th><Table.Th>Проверка</Table.Th><Table.Th>Резерв</Table.Th><Table.Th>Попыток</Table.Th></Table.Tr></Table.Thead>
            <Table.Tbody>{roleRows(assignments, summary.byRole).map((row) => <Table.Tr key={row.assignment.id}>
              <Table.Td>{row.role}</Table.Td><Table.Td>{row.assignment.model_id}</Table.Td><Table.Td>{connectionName(row.assignment.connection_id)}</Table.Td>
              <Table.Td>{{ unverified: 'не проверено', verified: 'проверено', failed: 'не прошла' }[row.assignment.capability_verification]}</Table.Td>
              <Table.Td>{row.fallback ? `${row.fallback.model_id} · ${connectionName(row.fallback.connection_id)}` : '—'}</Table.Td><Table.Td>{row.attempts}</Table.Td></Table.Tr>)}</Table.Tbody></Table>}
        <Text size="xs" c="dimmed" mt="xs">Назначения показаны только для чтения. Модератор должен быть на другой модели, чем персонаж. Изменение назначений — отдельная операция владельца.</Text></Paper></Tabs.Panel>
      <Tabs.Panel value="prices" pt="md"><Stack gap="md">
        <Paper withBorder p="md" radius="lg"><Title order={3} size="h5" mb="xs">Тарифы</Title>
          {!groups.length ? <Text size="sm" c="dimmed">Тарифов нет: стоимость неизвестна, а не нулевая.</Text> : groups.map((group) => <Paper key={group.key} withBorder p="sm" mb="xs">
            <Text fw={600}>{connectionName(group.connection)} · {group.model}</Text>
            {!group.current && <Text size="sm" c="orange.8">Действующей версии нет (все версии начинаются в будущем).</Text>}
            {group.versions.map((tariff) => <Group key={tariff.id + tariff.version} justify="space-between" mt={4}>
              <Text size="sm">v{tariff.version} · с {when(tariff.effective_at)} · вход {rateText(tariff.input_rate, tariff)} · выход {rateText(tariff.output_rate, tariff)}
                {tariff.cached_input_rate != null && ` · cached ${rateText(tariff.cached_input_rate, tariff)}`}{tariff.reasoning_output_rate != null && ` · reasoning ${rateText(tariff.reasoning_output_rate, tariff)}`}</Text>
              <Group gap="xs">{group.current?.id === tariff.id && group.current.version === tariff.version && <Badge color="teal" variant="light">действует</Badge>}</Group></Group>)}
            {run && group.current && <details><summary>Пересчёт по действующей версии</summary><Recalc api={api} scope={scope} run={run} tariff={group.current} /></details>}
          </Paper>)}</Paper>
        <TariffEditor api={api} run={run} connections={(connectionsQuery.data?.items ?? []).map((item) => ({ value: item.id, label: item.name }))}
          onSaved={() => void cache.invalidateQueries({ queryKey: projectionKey('tariffs', scope) })} />
      </Stack></Tabs.Panel>
      <Tabs.Panel value="history" pt="md"><Paper withBorder p="md" radius="lg" style={{ overflowX: 'auto' }}>
        {!rows.length ? <Text size="sm" c="dimmed">Попыток нет.</Text> : <Table aria-label="История попыток"><Table.Thead><Table.Tr>
          <Table.Th>Время</Table.Th><Table.Th>Роль</Table.Th><Table.Th>Модель</Table.Th><Table.Th>Статус</Table.Th><Table.Th>Usage</Table.Th><Table.Th>Токены вх/вых</Table.Th><Table.Th>Задержка</Table.Th><Table.Th>Тариф</Table.Th><Table.Th>Цена</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>{rows.map((row) => <Table.Tr key={row.id}><Table.Td>{when(row.at)}</Table.Td><Table.Td>{row.role}</Table.Td>
            <Table.Td>{row.requested}{row.routed && <Text size="xs" c="orange.8">ответила: {row.reported}</Text>}</Table.Td><Table.Td>{row.status}</Table.Td><Table.Td>{row.usage}</Table.Td>
            <Table.Td>{row.tokens}</Table.Td><Table.Td>{row.latencyMs === null ? '—' : `${number.format(row.latencyMs)} мс`}</Table.Td><Table.Td>{row.tariff}</Table.Td><Table.Td>{row.cost}</Table.Td></Table.Tr>)}</Table.Tbody></Table>}
        <Text size="xs" c="dimmed" mt="xs">Полные prompt, тексты приватных диалогов и секретные URL не хранятся в записях и не показываются.</Text></Paper></Tabs.Panel>
      <Tabs.Panel value="analytics" pt="md"><Stack gap="md">
        <Paper withBorder p="md" radius="lg"><Title order={3} size="h5" mb="xs">Токены по дням (известные)</Title>
          {!days.length ? <Text size="sm" c="dimmed">Данных нет.</Text> : <Bars title="Известные токены по дням" unit="ток." rows={days.map((d) => ({ label: d.day, value: d.tokens, note: d.unknown ? `неизвестно у ${d.unknown} попыток` : '' }))} />}</Paper>
        {Object.entries(perCurrency).map(([currency, series]) => <Paper key={currency} withBorder p="md" radius="lg"><Title order={3} size="h5" mb="xs">Расход по дням, {currency}</Title>
          <Bars title={`Расход по дням в ${currency}`} unit={currency} rows={series.map((d) => ({ label: d.day, value: Number(d.amount) }))} /></Paper>)}
        <Paper withBorder p="md" radius="lg"><Title order={3} size="h5" mb="xs">Распределение известных токенов</Title>
          {([['Роли', summary.byRole], ['Модели', summary.byModel], ['Подключения', summary.byConnection]] as const).map(([label, table]) => <div key={label}><Text fw={600} mt="xs">{label}</Text>
            {shares(table).map((row) => <Text key={row.key} size="sm">{label === 'Подключения' ? connectionName(row.key) : row.key}: {number.format(row.tokens)} ток. ({row.percent}%)</Text>)}</div>)}</Paper>
      </Stack></Tabs.Panel>
    </Tabs>
  </Stack>;
}
