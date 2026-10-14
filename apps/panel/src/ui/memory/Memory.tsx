import { Alert, Badge, Button, Checkbox, Group, Paper, Select, Stack, Tabs, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { ConsequencePlan, Receipt, RunSession, RunSessionPage, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import {
  afterExecute, availabilityLabels, basisSummary, buildCards, categoryLabels, conflictsOf, correctCommand, dependencyCount,
  executeCommand, filterCards, kindLabels, noFilters, planProblem, previewCommand, progress, rightsText, retentionText,
  validCorrection, visibleText, type Availability, type Card, type Category, type Correction, type Filters, type Kind,
  type Operation, type PlanPhase,
} from './model.ts';

const clock = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' });
const options = <T extends string>(labels: Record<T, string>) =>
  Object.entries(labels).map(([value, label]) => ({ value, label: label as string }));

// Pages continue from the server cursor. Keys extend the shared projection key so events invalidate them.
function usePaged<T extends { items: { id: string }[]; complete: boolean; next_cursor: string | null }>(
  scope: Scope, resource: string, read: (cursor: string | undefined, signal: AbortSignal) => Promise<T>,
) {
  return useInfiniteQuery({
    queryKey: [...projectionKey(resource, scope), 'memory'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => read(pageParam, signal),
    getNextPageParam: (last) => (last.complete || !last.next_cursor ? undefined : last.next_cursor),
  });
}

function Correct({ api, run, card, onDone }: { api: PanelApi; run: RunSession; card: Card; onDone: () => void }) {
  const [form, setForm] = useState<Correction>({ content: card.text ?? '', confidence: '', reason: '' });
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  async function submit() {
    const invalid = validCorrection(card, form);
    if (invalid) { setProblem(invalid); return; }
    setBusy(true); setProblem(null); setSaved(false);
    try {
      await api.command(correctCommand(run.scope, run.session_id, card, form, crypto.randomUUID()));
      setSaved(true); onDone();
    } catch (error) {
      // A stale version asks for a fresh look; the edit is never replayed over a newer one.
      setProblem(error instanceof ApiFailure && error.code === 'VERSION_CONFLICT' ?
        'Запись уже изменена. Обновите её и пересмотрите правку; чужая правка не будет затёрта.' : failureMessage(error));
      onDone();
    } finally { setBusy(false); }
  }
  return <Stack gap="xs" mt="sm">
    <Textarea label="Исправленный текст" autosize minRows={2} maxRows={8} maxLength={4000} value={form.content}
      onChange={(event) => setForm({ ...form, content: event.currentTarget.value })} />
    {card.kind === 'claim' && <TextInput label="Новая уверенность (0–1, только ниже текущей)" inputMode="decimal" value={form.confidence}
      description={`Сейчас: ${card.confidence}`} onChange={(event) => setForm({ ...form, confidence: event.currentTarget.value })} />}
    <Textarea label="Основание исправления" autosize minRows={2} maxLength={1000} value={form.reason}
      onChange={(event) => setForm({ ...form, reason: event.currentTarget.value })} />
    <Group justify="flex-end"><Button onClick={() => void submit()} disabled={busy}>{busy ? 'Сохраняем…' : 'Сохранить исправление'}</Button></Group>
    {problem && <Alert color="orange" role="alert">{problem}</Alert>}
    {saved && <Alert color="teal" role="status">Исправление принято. Прежние версии остаются в истории.</Alert>}
  </Stack>;
}

function PlanPanel({ api, scope, run, card, operation }: { api: PanelApi; scope: Scope; run: RunSession; card: Card; operation: Operation }) {
  const cache = useQueryClient();
  const [state, setState] = useState<PlanPhase>({ phase: 'idle' });
  const [understood, setUnderstood] = useState(false);
  const label = operation === 'delete' ? 'Удаление' : 'Экспорт';
  const operationId = state.phase === 'running' ? (state.receipt.operation_id ?? state.receipt.resource_id ?? null) : null;
  const status = useQuery({
    queryKey: [...projectionKey('operations', scope), 'one', operationId], enabled: operationId !== null,
    queryFn: ({ signal }) => api.operation(scope, operationId!, signal),
  });
  const [opened] = useState(() => Date.now());
  const planProblemText = state.phase === 'ready' ? planProblem(state.plan, card, opened) : null;
  async function preview() {
    setState({ phase: 'previewing' }); setUnderstood(false);
    try {
      const receipt = await api.command(previewCommand(run.scope, run.session_id, card, operation, crypto.randomUUID())) as Receipt;
      if (!receipt.resource_id) throw new ApiFailure('INVALID_ARGUMENTS');
      setState({ phase: 'ready', plan: await api.plan(scope, receipt.resource_id) });
    } catch (error) { setState({ phase: 'refused', code: error instanceof ApiFailure ? error.code : 'NETWORK' }); }
  }
  async function execute(plan: ConsequencePlan, again?: ReturnType<typeof executeCommand>) {
    // The term is checked again at the moment of the click, not only when the plan was drawn.
    if (!again && planProblem(plan, card, Date.now())) { setState({ phase: 'refused', code: 'EXPIRED' }); return; }
    const command = again ?? executeCommand(run.scope, run.session_id, plan, operation, crypto.randomUUID());
    setState({ phase: 'executing', plan, command });
    try {
      setState(afterExecute(plan, command, { receipt: await api.command(command) as Receipt }));
    } catch (error) {
      setState(afterExecute(plan, command, { code: error instanceof ApiFailure ? error.code : 'NETWORK' }));
    } finally {
      for (const resource of ['sources', 'episodes', 'claims', 'summaries', 'operations']) {
        void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
      }
    }
  }
  const view = progress(status.data ?? null, label);
  return <Stack gap="xs" mt="sm">
    {state.phase === 'idle' || state.phase === 'refused' || state.phase === 'previewing' ?
      <Button variant="light" disabled={state.phase === 'previewing'} onClick={() => void preview()}>
        {state.phase === 'previewing' ? 'Готовим план…' : operation === 'delete' ? 'Подготовить удаление' : 'Подготовить экспорт'}</Button> : null}
    {state.phase === 'refused' && <Alert color="orange" role="alert">{failureMessage(new ApiFailure(state.code))} Ничего не изменено.</Alert>}
    {(state.phase === 'ready' || state.phase === 'executing' || state.phase === 'unknown') && <Paper withBorder p="sm" aria-label="План последствий">
      <Text fw={600}>План: {label}</Text>
      <Text className="plain-text" size="sm" mt={4}>{state.plan.consequences}</Text>
      <Text size="sm" mt={4}>Связанных записей: {dependencyCount(state.plan, card)} · действует до {clock.format(new Date(state.plan.expires_at))}</Text>
      <Text size="xs" c="dimmed">Внешняя публикация, если была, удаляется на площадке отдельно; копии обрабатываются по политике резервных копий.</Text>
      {planProblemText && <Alert color="orange" mt="xs" role="alert">{planProblemText}</Alert>}
      {state.phase === 'ready' && <>
        {operation === 'delete' && <Checkbox mt="xs" label="Я понимаю, что удалённое восстановить нельзя" checked={understood}
          onChange={(event) => setUnderstood(event.currentTarget.checked)} />}
        <Group mt="xs"><Button color={operation === 'delete' ? 'red' : 'blue'} disabled={!!planProblemText || (operation === 'delete' && !understood)}
          onClick={() => void execute(state.plan)}>{operation === 'delete' ? 'Удалить' : 'Экспортировать'}</Button>
          <Button variant="subtle" color="gray" onClick={() => setState({ phase: 'idle' })}>Отмена</Button></Group></>}
      {state.phase === 'executing' && <Text size="sm" role="status" mt="xs">Отправляем команду…</Text>}
      {state.phase === 'unknown' && <Alert color="orange" mt="xs" role="alert" title="Результат неизвестен">
        Команда могла быть выполнена. Проверка повторяет ту же команду: ядро вернёт сохранённую квитанцию, второго выполнения не будет.
        <Button mt="xs" size="compact-sm" variant="light" onClick={() => void execute(state.plan, state.command)}>Проверить исход</Button></Alert>}
    </Paper>}
    {state.phase === 'running' && <Paper withBorder p="sm" role="status" aria-label="Ход выполнения">
      <Text fw={600}>{label}: {view.text}</Text>
      {!!view.done.length && <Text size="sm">Выполнено: {view.done.join(', ')}</Text>}
      {!!view.pending.length && <Text size="sm">Ожидает: {view.pending.join(', ')}</Text>}
      <Button mt="xs" size="compact-sm" variant="subtle" onClick={() => void status.refetch()}>Обновить статус</Button>
    </Paper>}
  </Stack>;
}

function Detail({ api, scope, run, card, all, onSelect, onChanged }: {
  api: PanelApi; scope: Scope; run: RunSession | undefined; card: Card; all: Card[]; onSelect: (card: Card) => void; onChanged: () => void;
}) {
  const [mode, setMode] = useState<'correct' | 'delete' | 'export' | null>(null);
  const [now] = useState(() => Date.now()); // render stays pure; retention text is a snapshot of when the card opened
  const text = visibleText(card);
  const [full, setFull] = useState(false);
  const shown = text !== null && !full && text.length > 600 ? text.slice(0, 600) + '…' : text;
  const conflicts = conflictsOf(card, all);
  return <Paper withBorder p="md" radius="lg" aria-label="Карточка записи">
    <Group gap="xs" mb="xs">
      <Badge variant="light">{kindLabels[card.kind]}</Badge><Badge variant="outline">{categoryLabels[card.category]}</Badge>
      <Badge color={card.availability === 'available' ? 'teal' : 'gray'} variant="light">{availabilityLabels[card.availability]}</Badge>
    </Group>
    {shown === null ? <Text c="dimmed" fs="italic">Содержимое недоступно: {availabilityLabels[card.availability].toLowerCase()}. Полный текст не восстанавливается.</Text> :
      <Text className="plain-text">{shown}</Text>}
    {text !== null && text.length > 600 && <Button size="compact-xs" variant="subtle" onClick={() => setFull(!full)}>{full ? 'Свернуть' : 'Показать полностью'}</Button>}
    <Stack gap={2} mt="sm">
      {card.confidence !== null && <Text size="sm">Уверенность: {card.confidence}</Text>}
      {card.kind !== 'source' && <Text size="sm">Основание: {basisSummary(card.basis)}</Text>}
      {card.source && <><Text size="sm">Срок хранения: {retentionText(card.source, now)}</Text>
        <Text size="sm">{rightsText(card.source).join(' · ')}</Text>
        <Text size="xs" c="dimmed">Адаптер: {card.source.adapter_id} · внешний ID: {card.source.external_id ?? '—'}</Text></>}
      {card.version !== null && <Text size="xs" c="dimmed">Версия {card.version} · {clock.format(new Date(card.at))}</Text>}
    </Stack>
    {!!card.conflicts.length && <Alert color="orange" mt="sm" title="Есть противоречие">
      Обе позиции сохранены. {conflicts.length < card.conflicts.length ? 'Не все связанные утверждения загружены.' : ''}
      <Group gap="xs" mt={4}>{conflicts.map((other) => <Button key={other.key} size="compact-xs" variant="light" onClick={() => onSelect(other)}>{other.text?.slice(0, 40)}</Button>)}</Group>
    </Alert>}
    <Group mt="md" gap="xs">
      {(card.kind === 'claim' || card.kind === 'summary') && <Button size="compact-sm" variant={mode === 'correct' ? 'filled' : 'light'} onClick={() => setMode(mode === 'correct' ? null : 'correct')}>Исправить</Button>}
      {card.kind === 'source' && <>
        <Button size="compact-sm" variant={mode === 'export' ? 'filled' : 'light'} onClick={() => setMode(mode === 'export' ? null : 'export')}>Экспорт</Button>
        <Button size="compact-sm" color="red" variant={mode === 'delete' ? 'filled' : 'light'} onClick={() => setMode(mode === 'delete' ? null : 'delete')}>Удалить</Button></>}
    </Group>
    {mode && !run && <Alert color="orange" mt="sm">Нет подтверждённого запуска в этой области: команды недоступны.</Alert>}
    {mode === 'correct' && run && <Correct key={card.key + card.version} api={api} run={run} card={card} onDone={onChanged} />}
    {(mode === 'delete' || mode === 'export') && run && <PlanPanel key={card.key + mode} api={api} scope={scope} run={run} card={card} operation={mode} />}
  </Paper>;
}

export function Memory({ api, scope }: { api: PanelApi; scope: Scope }) {
  const cache = useQueryClient();
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = (runs.data as RunSessionPage | undefined)?.items[0];
  const sources = usePaged(scope, 'sources', (cursor, signal) => api.sources(scope, signal, cursor));
  const episodes = usePaged(scope, 'episodes', (cursor, signal) => api.episodes(scope, signal, cursor));
  const claims = usePaged(scope, 'claims', (cursor, signal) => api.claims(scope, signal, cursor));
  const summaries = usePaged(scope, 'summaries', (cursor, signal) => api.summaries(scope, signal, cursor));
  const [filters, setFilters] = useState<Filters>(noFilters);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const all = [sources, episodes, claims, summaries];
  const cards = buildCards({
    sources: sources.data?.pages.flatMap((page) => page.items) ?? [], episodes: episodes.data?.pages.flatMap((page) => page.items) ?? [],
    claims: claims.data?.pages.flatMap((page) => page.items) ?? [], summaries: summaries.data?.pages.flatMap((page) => page.items) ?? [],
  });
  const shown = filterCards(cards, filters);
  const selected = cards.find((card) => card.key === selectedKey) ?? null;
  const more = all.filter((query) => query.hasNextPage);
  const failed = all.filter((query) => query.isError);
  const set = (patch: Partial<Filters>) => setFilters({ ...filters, ...patch });
  return <Stack gap="lg">
    <div><Text className="eyebrow">03 / Память и люди</Text><Title order={1}>Память</Title></div>
    {scope.memory_mode === 'test' && <Alert color="violet" title="Тестовая память">Записи изолированы от рабочей биографии.</Alert>}
    <Tabs defaultValue="memory">
      <Tabs.List><Tabs.Tab value="memory">Записи</Tabs.Tab><Tabs.Tab value="people">Люди</Tabs.Tab></Tabs.List>
      <Tabs.Panel value="people" pt="md"><Alert color="gray">Раздел людей, встреч и тем готовится отдельным этапом (M4-03).</Alert></Tabs.Panel>
      <Tabs.Panel value="memory" pt="md"><Stack gap="md">
        <Paper withBorder p="md" radius="lg"><Group align="flex-end" gap="sm">
          <Select label="Тип" clearable data={options<Kind>(kindLabels)} value={filters.kind || null} onChange={(v) => set({ kind: (v ?? '') as Kind | '' })} />
          <Select label="Вид знания" clearable data={options<Category>(categoryLabels)} value={filters.category || null} onChange={(v) => set({ category: (v ?? '') as Category | '' })} />
          <Select label="Доступность" clearable data={options<Availability>(availabilityLabels)} value={filters.availability || null} onChange={(v) => set({ availability: (v ?? '') as Availability | '' })} />
          <TextInput label="Человек / автор" value={filters.person} onChange={(event) => set({ person: event.currentTarget.value })} />
          <TextInput label="С даты" type="date" value={filters.from} onChange={(event) => set({ from: event.currentTarget.value })} />
          <TextInput label="По дату" type="date" value={filters.to} onChange={(event) => set({ to: event.currentTarget.value })} />
          <TextInput label="Текст" value={filters.query} onChange={(event) => set({ query: event.currentTarget.value })} />
          <Button variant="subtle" color="gray" onClick={() => setFilters(noFilters)}>Сбросить</Button>
        </Group>
          <Text size="xs" c="dimmed" mt="xs">Поиск идёт по уже загруженным записям; это не поиск по всей памяти.</Text></Paper>
        {!!failed.length && <Alert color="orange" title="Часть памяти недоступна">{failureMessage(failed[0].error)} Список может быть неполным.</Alert>}
        {!!more.length && <Alert color="orange">Загружена не вся память.
          <Button ml="sm" size="compact-sm" variant="light" onClick={() => more.forEach((query) => void query.fetchNextPage())}>Загрузить ещё</Button></Alert>}
        <Group align="flex-start" grow wrap="wrap">
          <Paper withBorder p="md" radius="lg" style={{ flex: '1 1 22rem' }}>
            <Title order={2} size="h4" mb="sm">Записи ({shown.length} из {cards.length})</Title>
            {all.some((query) => query.isPending) ? <Text size="sm">Получаем память…</Text> : !shown.length ?
              <Text size="sm" c="dimmed">{cards.length ? 'Ничего не подходит под фильтры.' : 'Память пока пуста.'}</Text> :
              <ul className="memory-list" aria-label="Список записей">{shown.map((card) => <li key={card.key}>
                <button type="button" className={`memory-item ${card.key === selectedKey ? 'selected' : ''}`} onClick={() => setSelectedKey(card.key)}>
                  <Group gap="xs"><Badge size="xs" variant="light">{kindLabels[card.kind]}</Badge><Badge size="xs" variant="outline">{categoryLabels[card.category]}</Badge>
                    {card.availability !== 'available' && <Badge size="xs" color="gray">{availabilityLabels[card.availability]}</Badge>}
                    {!!card.conflicts.length && <Badge size="xs" color="orange">противоречие</Badge>}</Group>
                  <Text size="sm" className="plain-text" lineClamp={2}>{visibleText(card) ?? card.title}</Text>
                  <Text size="xs" c="dimmed">{clock.format(new Date(card.at))}</Text>
                </button></li>)}</ul>}
          </Paper>
          <div style={{ flex: '1 1 24rem' }}>{selected ?
            <Detail key={selected.key} api={api} scope={scope} run={run} card={selected} all={cards} onSelect={(card) => setSelectedKey(card.key)}
              onChanged={() => { for (const query of all) void query.refetch(); void cache.invalidateQueries({ queryKey: ['projection'] }); }} /> :
            <Alert color="gray">Выберите запись, чтобы увидеть источники, срок хранения и действия.</Alert>}</div>
        </Group>
      </Stack></Tabs.Panel>
    </Tabs>
  </Stack>;
}
