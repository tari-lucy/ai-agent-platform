import type { ConnectionState } from '../../shared/events.ts';
import { formatTime } from '../time.ts';
import { Alert, Anchor, Badge, Button, Group, Paper, Select, Stack, Text, Textarea, Title } from '@mantine/core';
import { keepPreviousData, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { Briefing, OperationProjection, Receipt, RunSession, RunSessionPage, Scope, Utterance } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { acknowledgeReply } from '../../shared/owner-commands.ts';
import { previewKey, type PrivatePreview } from '../../shared/private-preview.ts';
import { projectionKey } from '../../shared/scope.ts';
import {
  afterSend, briefingStatuses, briefingTypes, buildTimeline, canSend, cancelBriefing, createBriefing,
  generating, goal, latestRevisions, mayAcknowledge, messageCommand, messageProgress, openOperations, statusLabels, validBriefing,
  type Composer, type Entry,
} from './model.ts';
import { historyNote, PAGE_BUDGET, PAGE_BUDGET_MAX, walk, type Page } from './history.ts';
import { Timeline } from './Timeline.tsx';
import { nativeAgent, nativeHref, useHarnessEntry, useNativeEntry } from '../opencode.ts';

const when = (value: string) => formatTime(value, 'medium');

function useProjection<T>(api: PanelApi, scope: Scope, resource: string, read: (scope: Scope, signal: AbortSignal) => Promise<T>) {
  return useQuery({ queryKey: projectionKey(resource, scope), queryFn: ({ signal }) => read(scope, signal) });
}

function useWalk<T extends { id: string }>(
  scope: Scope, resource: string, budget: number, poll: boolean,
  read: (scope: Scope, signal: AbortSignal, cursor?: string) => Promise<Page<T>>,
) {
  return useQuery({
    queryKey: [...projectionKey(resource, scope), 'walk', budget],
    queryFn: ({ signal }) => walk<T>((cursor) => read(scope, signal, cursor), budget),
    placeholderData: keepPreviousData,
    refetchInterval: poll ? 3000 : false,
  });
}

// Back online or back in the tab: only re-read. A command is never replayed from here.
function useReconnect(scope: Scope) {
  const cache = useQueryClient();
  const [online, setOnline] = useState(typeof navigator === 'undefined' ? true : navigator.onLine);
  useEffect(() => {
    const reread = () => {
      for (const resource of ['sources', 'utterances', 'deliveries', 'operations', 'sessions', 'private-api-status']) {
        void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
      }
    };
    const up = () => { setOnline(true); reread(); };
    const down = () => setOnline(false);
    const visible = () => { if (document.visibilityState === 'visible') reread(); };
    window.addEventListener('online', up); window.addEventListener('offline', down);
    document.addEventListener('visibilitychange', visible);
    return () => {
      window.removeEventListener('online', up); window.removeEventListener('offline', down);
      document.removeEventListener('visibilitychange', visible);
    };
  }, [cache, scope]);
  return online;
}

function Composer({ api, run, scope, budgetBlocked, operationRows, utteranceRows, onSent }: {
  api: PanelApi; run: RunSession | undefined; scope: Scope; budgetBlocked: boolean;
  operationRows: OperationProjection[]; utteranceRows: Utterance[]; onSent: () => void;
}) {
  const cache = useQueryClient();
  const [text, setText] = useState('');
  const [phase, setPhase] = useState<Composer>({ phase: 'idle' });
  const verdict = budgetBlocked ? { ok: false, reason: 'Лимит API исчерпан. Дождитесь обновления лимита.' } : canSend(run, text, phase.phase);
  async function submit(command: ReturnType<typeof messageCommand>) {
    setPhase({ phase: 'sending', command });
    try {
      const receipt = await api.command(command) as Receipt;
      const next = afterSend(command, { receipt });
      setPhase(next);
      if (next.phase === 'saved') { setText(''); onSent(); }
    } catch (error) {
      setPhase(afterSend(command, { code: error instanceof ApiFailure ? error.code : 'NETWORK' }));
    } finally {
      for (const resource of ['sources', 'operations', 'utterances', 'deliveries', 'private-api-status']) {
        void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
      }
    }
  }
  function send() {
    if (!run || !verdict.ok) return;
    void submit(messageCommand(run, text, crypto.randomUUID()));
  }
  return <Paper withBorder p="md" radius="lg">
    <Textarea label="Сообщение персонажу" autosize minRows={2} maxRows={8} value={text} maxLength={4000}
      onChange={(event) => setText(event.currentTarget.value)} disabled={phase.phase === 'sending' || phase.phase === 'unknown'}
      description="Отправка не запускает агента и не обходит STOP." />
    <Group mt="sm" justify="space-between" align="flex-start">
      <Text size="xs" c="dimmed" role="status">{verdict.ok ? 'Готово к отправке.' : verdict.reason}</Text>
      <Button onClick={send} disabled={!verdict.ok}>{phase.phase === 'sending' ? 'Отправляем…' : 'Отправить'}</Button>
    </Group>
    {phase.phase === 'saved' && (() => {
      const progress = messageProgress(phase.receipt, operationRows, utteranceRows);
      const color = progress.stage === 'ready' ? 'teal' : progress.stage === 'failed' || progress.stage === 'unknown' ? 'orange' :
        progress.stage === 'cancelled' ? 'gray' : 'blue';
      return <Alert color={color} mt="sm" role="status" title={`Сообщение: ${progress.label}`}>
        <Text size="sm">{progress.detail}</Text>
        <Text size="xs" c="dimmed" mt={4}>Путь: принято → обрабатывается → ответ готов. Статус берётся у ядра, а не угадывается.</Text>
      </Alert>;
    })()}
    {phase.phase === 'rejected' && <Alert color="orange" mt="sm" role="alert">
      {failureMessage(new ApiFailure(phase.code))} Сообщение не отправлено; текст сохранён в поле.</Alert>}
    {phase.phase === 'unknown' && <Alert color="orange" mt="sm" role="alert" title="Результат отправки неизвестен">
      Сообщение могло быть сохранено. Новое не создаётся: проверка повторяет ту же команду, и ядро вернёт сохранённую квитанцию.
      <Button mt="xs" size="compact-sm" variant="light" onClick={() => void submit(phase.command)}>Проверить исход</Button>
    </Alert>}
  </Paper>;
}

function BriefingPanel({ api, scope, run }: { api: PanelApi; scope: Scope; run: RunSession | undefined }) {
  const cache = useQueryClient();
  const rows = useProjection(api, scope, 'briefings', (s, signal) => api.briefings(s, signal));
  const [kind, setKind] = useState<Briefing['type']>('context');
  const [content, setContent] = useState('');
  const [expires, setExpires] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run_(command: Parameters<PanelApi['command']>[0], clear: boolean) {
    setBusy(true); setProblem(null);
    try {
      await api.command(command);
      if (clear) { setContent(''); setExpires(''); }
    } catch (error) { setProblem(failureMessage(error)); }
    finally { setBusy(false); void cache.invalidateQueries({ queryKey: projectionKey('briefings', scope) }); }
  }
  function create() {
    const expiresAt = expires ? new Date(expires).toISOString() : '';
    const invalid = validBriefing(content, expires ? expiresAt : '', Date.now());
    if (invalid || !run) { setProblem(invalid ?? 'Нет подтверждённого запуска в этой области.'); return; }
    void run_(createBriefing(run.scope, run.session_id, kind, content, expiresAt, crypto.randomUUID()), true);
  }
  return <Paper withBorder p="md" radius="lg">
    <Title order={2} size="h4" mb="xs">Вводные</Title>
    <Text size="sm" c="dimmed" mb="sm">Вводная не меняет канон и права автоматически.</Text>
    {rows.isPending ? <Text size="sm">Получаем вводные…</Text> : rows.isError ?
      <Alert color="orange">{failureMessage(rows.error)}</Alert> :
      !rows.data.items.length ? <Text size="sm" c="dimmed">Вводных нет.</Text> :
        <Stack gap="xs">{rows.data.items.map((row) => <Paper key={row.id} withBorder p="xs">
          <Group justify="space-between"><Badge variant="light">{briefingTypes[row.type]}</Badge>
            <Badge color={row.status === 'active' ? 'teal' : 'gray'} variant="light">{briefingStatuses[row.status]}</Badge></Group>
          <Text className="plain-text" size="sm" mt={4}>{row.content}</Text>
          <Group justify="space-between" mt={4}>
            <Text size="xs" c="dimmed">Автор: {row.author_id} · до {when(row.expires_at)}</Text>
            {row.status === 'active' && run && <Button size="compact-xs" variant="subtle" color="gray" disabled={busy}
              onClick={() => void run_(cancelBriefing(run.scope, run.session_id, row, crypto.randomUUID()), false)}>Отменить</Button>}
          </Group>
        </Paper>)}
        {!rows.data.complete && <Text size="xs" c="orange.8">Показана первая страница вводных.</Text>}</Stack>}
    <Stack gap="xs" mt="md">
      <Select label="Тип вводной" value={kind} allowDeselect={false} data={Object.entries(briefingTypes).map(([value, label]) => ({ value, label }))}
        onChange={(value) => value && setKind(value as Briefing['type'])} />
      <Textarea label="Текст вводной" autosize minRows={2} maxRows={6} maxLength={4000} value={content}
        onChange={(event) => setContent(event.currentTarget.value)} />
      <div><label htmlFor="briefing-expiry"><Text size="sm" fw={500}>Срок действия (ваше время)</Text></label>
        <input id="briefing-expiry" type="datetime-local" value={expires} onChange={(event) => setExpires(event.currentTarget.value)} /></div>
      <Group justify="flex-end"><Button variant="light" onClick={create} disabled={busy}>Добавить вводную</Button></Group>
      {problem && <Alert color="orange" role="alert">{problem}</Alert>}
    </Stack>
  </Paper>;
}

function MemoryStatus({ api, scope, enabled }: { api: PanelApi; scope: Scope; enabled: boolean }) {
  const cycles = useQuery({ queryKey: projectionKey('memory-cycles', scope),
    queryFn: ({ signal }) => api.memoryCycles(scope, signal), enabled, refetchInterval: 15000 });
  if (!enabled) return null;
  const stages = { queued: 'В очереди', extract: 'Извлекает утверждения', consolidate: 'Пишет сводку', reflect: 'Осмысляет опыт', done: 'Завершено' };
  const rows = [...(cycles.data?.items ?? [])].sort((a,b) => b.updated_at.localeCompare(a.updated_at)).slice(0,5);
  return <Paper withBorder p="md" radius="lg" aria-label="Автономная память">
    <Title order={2} size="h4">Автономная память</Title>
    <Text size="sm" c="dimmed">После смены суток UTC агент обрабатывает подтверждённые разговоры. STOP и пауза откладывают обработку до продолжения работы. При нехватке лимита остаток переносится на следующие сутки.</Text>
    {cycles.isError ? <Alert color="orange">{failureMessage(cycles.error)}</Alert> :
      !rows.length ? <Text size="sm">Циклов пока нет. Разговоры текущих суток ожидают следующего обновления.</Text> :
        rows.map((row) => <Text key={row.id} size="sm" c={row.status === 'failed' || row.status === 'unknown' ? 'orange.8' : undefined}>
          {when(row.updated_at)} · {stages[row.stage]} · {row.episode_ids.length} эпизодов
          {(row.status === 'failed' || row.status === 'unknown') && <> · Требуется проверка ({row.error_code}). Автоматического повтора нет.</>}
        </Text>)}
    {cycles.data && !cycles.data.complete && <Text size="xs" c="orange.8">Показана часть журнала циклов.</Text>}
  </Paper>;
}


export function Dialogue({ api, scope, connection = 'connecting' }: { api: PanelApi; scope: Scope; connection?: ConnectionState }) {
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = (runs.data as RunSessionPage | undefined)?.items[0];
  const online = useReconnect(scope);
  const native = useNativeEntry();
  const agent = nativeAgent(native, scope);
  const harness = useHarnessEntry();
  const nativeUrl = harness?.enabled && harness.url ? harness.url : agent ? nativeHref(agent.directory) : null;
  const [budget, setBudget] = useState(PAGE_BUDGET);
  const [watching, setWatching] = useState(false);
  useEffect(() => {
    if (!watching) return;
    const timer = setTimeout(() => setWatching(false), 60000);
    return () => clearTimeout(timer);
  }, [watching]);
  const operations = useWalk(scope, 'operations', budget, false, (s, signal, cursor) => api.operations(s, signal, cursor));
  const operationRows = operations.data?.items ?? [];
  // Poll while the core still owns an open operation, or briefly after a send: read-only refreshes.
  const poll = openOperations(operationRows).length > 0 || watching;
  const sources = useWalk(scope, 'sources', budget, poll, (s, signal, cursor) => api.inbound(s, signal, cursor));
  const utterances = useWalk(scope, 'utterances', budget, poll, (s, signal, cursor) => api.utterances(s, signal, cursor));
  const deliveries = useWalk(scope, 'deliveries', budget, poll, (s, signal, cursor) => api.deliveries(s, signal, cursor));
  const budgetStatus = useQuery({ queryKey: projectionKey('private-api-status', scope),
    queryFn: ({ signal }) => api.privateApiStatus(scope, signal), refetchInterval: 15000 });
  const cache = useQueryClient();
  const preview = useQuery<PrivatePreview>({ queryKey: previewKey(scope), enabled: false });
  const automaticReceipts = useRef(new Set<string>());
  const [receiving, setReceiving] = useState<string | null>(null);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  async function receiveReply(row: import('../../shared/contracts.ts').Utterance) {
    setReceiving(row.id); setReceiptError(null);
    try { await api.command(acknowledgeReply(row, 'panel-receipt-' + row.id + '-' + row.revision)); }
    catch (error) { setReceiptError(failureMessage(error)); }
    finally {
      setReceiving(null);
      for (const resource of ['deliveries', 'episodes', 'sources', 'memory-cycles']) {
        void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
      }
    }
  }
  const utteranceRows = utterances.data?.items ?? [];
  const loadedDeliveries = deliveries.data?.items ?? [];
  // Bound extra reads. Each lookup proves the state of this exact utterance, independently
  // of whether historical deliveries have all been paged into the timeline.
  const receiptCandidates = latestRevisions(utteranceRows).filter((row) => row.operation_id &&
    row.audience_id === 'operator-panel' && row.scope.run_id === run?.run_id &&
    !loadedDeliveries.some((d) => d.utterance_id === row.id && d.revision === row.revision && d.status === 'DELIVERED'))
    .sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 20);
  const receiptReads = useQueries({ queries: receiptCandidates.map((row) => ({
    queryKey: [...projectionKey('deliveries', scope), 'utterance', row.id],
    queryFn: ({ signal }: { signal: AbortSignal }) => walk((cursor) => api.deliveries(scope, signal, cursor, row.id), PAGE_BUDGET),
    refetchInterval: poll ? 3000 : false,
  })) });
  const deliveryRows = [...new Map([...loadedDeliveries, ...receiptReads.flatMap((q) => q.data?.items ?? [])]
    .map((row) => [row.id, row])).values()];
  const receiptKnown = new Set(receiptCandidates.filter((_, i) => receiptReads[i].isSuccess &&
    !receiptReads[i].isFetching && receiptReads[i].data?.complete && !receiptReads[i].data?.error).map((row) => row.id));
  const privateRows = utteranceRows.filter((row) => row.operation_id &&
    row.audience_id === 'operator-panel' && row.scope.run_id === run?.run_id &&
    !deliveryRows.some((d) => d.utterance_id === row.id && d.revision === row.revision && d.status === 'DELIVERED'));
  const queries = [sources, utterances, deliveries, operations];
  const failed = queries.filter((query) => query.isError);
  const walks = [sources, utterances, deliveries].map((query) => query.data).filter((value) => value !== undefined);
  const timeline = buildTimeline(sources.data?.items ?? [], utteranceRows, deliveryRows);
  const summary = goal(run, operationRows);
  const note = historyNote(walks, budget);
  const healthy = !failed.length && walks.length === 3 && walks.every((w) => w.error === null);
  const latest = useRef({ run, privateRows, utteranceRows, deliveryRows, healthy, scope, receiptKnown });
  useEffect(() => { latest.current = { run, privateRows, utteranceRows, deliveryRows, healthy, scope, receiptKnown }; });
  // Only a final, exact, newest revision that is really on screen in a visible tab is acknowledged.
  const shown = useCallback((entry: Entry, ratio: number) => {
    const state = latest.current;
    const row = state.privateRows.find((r) => entry.key === 'out:' + r.utterance_id && entry.text === r.content);
    const newest = row !== undefined && latestRevisions(state.utteranceRows).some((r) => r.id === row.id && r.revision === row.revision);
    if (!row || !mayAcknowledge({
      visibility: document.visibilityState, ratio, mode: state.scope.memory_mode, runActive: state.run?.state === 'ACTIVE',
      runOpen: state.run?.activity === 'OPEN', sameRun: row.scope.run_id === state.run?.run_id, entryKey: entry.key, row,
      shownText: entry.text, newestRevision: newest, delivered: false, streaming: false, readsHealthy: state.healthy && state.receiptKnown.has(row.id),
    })) return;
    const key = row.id + ':' + row.revision;
    if (automaticReceipts.current.has(key)) return; // one automatic attempt; a failure leaves the manual button
    automaticReceipts.current.add(key);
    void receiveReply(row);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  const stream = preview.data?.status === 'preview' && preview.data.scope.run_id === run?.run_id && run?.state === 'ACTIVE' &&
    preview.data.text !== '' && !timeline.some((entry) => entry.kind === 'reply' && entry.text?.startsWith(preview.data!.text)) ?
    { text: preview.data.text, incomplete: preview.data.incomplete } : null;
  const stale = failed.some((query) => query.data !== undefined);
  return <Stack gap="lg">
    <div><Text className="eyebrow">01 / Диалог</Text><Title order={1}>Разговор</Title></div>
    {scope.memory_mode === 'test' &&
      <Alert color="violet" title="Тестовая память">Сообщения здесь не попадут в рабочую биографию.</Alert>}
    <Paper withBorder p="md" radius="lg" aria-label="Текущая цель">
      <Group justify="space-between"><Text fw={600}>{summary.headline}</Text>
        {generating(operationRows) && <Badge color="blue" variant="light">{statusLabels.generating}</Badge>}</Group>
      {summary.reasons.map((reason) => <Text key={reason} size="sm" c="dimmed">{reason}</Text>)}
      {!!summary.pending.length && <><Text size="sm" fw={500} mt="xs">Ожидающие дела ({openOperations(operationRows).length})</Text>
        <ul>{summary.pending.map((line) => <li key={line}><Text size="sm" className="plain-text">{line}</Text></li>)}</ul></>}
    </Paper>
    <Text size="sm" role="status" aria-label="Связь диалога" c={connection === 'connected' ? 'dimmed' : 'orange.8'}>
      {connection === 'connected' ? 'События диалога подключены.' : connection === 'connecting' ? 'Подключаем события диалога…' :
        connection === 'reconnecting' ? 'Связь событий прервана. Переподключаемся…' :
        connection === 'denied' ? 'Подписка на события отклонена. Проверьте вход и область.' : 'События недоступны. Повторите подключение вверху панели.'}
      {connection !== 'connected' && <> Последние данные могут устареть. Сообщения автоматически не повторяются.
        <Button ml="sm" size="compact-xs" variant="subtle" onClick={() => {
          for (const resource of ['sources', 'utterances', 'deliveries', 'operations', 'sessions']) {
            void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
          }
        }}>Перечитать диалог</Button>
      </>}
    </Text>
    {!online && <Alert color="orange" role="alert" title="Нет связи с сервером">
      Показаны последние полученные данные. Новые сообщения и ответы могут быть не видны; STOP отправляется отдельным запросом.
      Сообщения не повторяются автоматически.</Alert>}
    {!!failed.length && <Alert color="orange" role="alert" title={stale ? 'Данные могут быть устаревшими' : 'Часть диалога недоступна'}>
      {failureMessage(failed[0].error)} {stale ? 'Показаны ранее полученные записи; обновить их не удалось.' : 'Лента может быть неполной.'}{' '}
      Попытки отправки не повторяются автоматически.
      <Button ml="sm" size="compact-sm" variant="light" onClick={() => void cache.invalidateQueries({ queryKey: ['projection'] })}>Перечитать</Button>
    </Alert>}
    <Paper withBorder p="md" radius="lg">
      <Group justify="space-between" mb="sm"><Title order={2} size="h4">Лента</Title>
        {walks.length === 3 && <Text size="xs" c={note.tone === 'ok' ? 'dimmed' : 'orange.8'} role="status" data-history={note.tone}>{note.text}</Text>}</Group>
      {sources.isPending || utterances.isPending ? <Text size="sm">Получаем ленту…</Text> :
        !timeline.length && !stream ? <Text size="sm" c="dimmed">Пока нет сообщений.</Text> :
          <Timeline entries={timeline} stream={stream} onShown={run?.state === 'ACTIVE' ? shown : undefined} />}
      {walks.length === 3 && note.tone !== 'ok' && budget < PAGE_BUDGET_MAX &&
        <Button mt="sm" size="compact-sm" variant="light" onClick={() => setBudget((value) => Math.min(PAGE_BUDGET_MAX, value + PAGE_BUDGET))}>
          {note.tone === 'error' ? 'Повторить загрузку истории' : 'Загрузить более ранние записи'}</Button>}
    </Paper>
    {receiptError && <Alert color="orange">{receiptError}</Alert>}
    {run?.state === 'ACTIVE' && privateRows.map((row) =>
      <Button key={row.id} variant="light" disabled={receiving !== null} onClick={() => void receiveReply(row)}>
        Подтвердить получение приватного ответа
      </Button>)}
    {budgetStatus.data?.enabled && <Alert color={budgetStatus.data.attempts_remaining === 0 ? 'orange' : 'blue'} title="Лимит API">
      Осталось {budgetStatus.data.attempts_remaining} из {budgetStatus.data.attempt_limit} запросов{budgetStatus.data.period === 'utc_day' ? ' за сутки' : ' за весь пробный период'}.
      {budgetStatus.data.memory_processing && <Text size="sm">Для диалога доступно {budgetStatus.data.dialogue_remaining}. На фоновую память: {budgetStatus.data.memory_calls_used} из {budgetStatus.data.memory_daily_calls} запросов.</Text>}
      {budgetStatus.data.resets_at && <> Обновление: {when(budgetStatus.data.resets_at)}.</>}
      <Text size="xs" mt={4}>Начатые неудачные попытки тоже учитываются. Денежный расход показывается отдельно.</Text>
    </Alert>}
    {budgetStatus.isError && <Text size="xs" c="dimmed">Остаток лимита API недоступен. Сервер проверит допуск перед запросом.</Text>}
    {native ? <Alert color="blue" title="Разговор теперь в OpenCode">
      Здесь — только чтение прежнего диалога. Новое сообщение напишите в{' '}
      {nativeUrl ? <Anchor href={nativeUrl} target={harness?.enabled ? '_blank' : undefined}>родном окне OpenCode</Anchor> : 'родном окне OpenCode'}.
    </Alert> : <>
    <Composer api={api} run={run} scope={scope} operationRows={operationRows} utteranceRows={utteranceRows}
      onSent={() => setWatching(true)}
      budgetBlocked={budgetStatus.data?.enabled === true && (budgetStatus.data.dialogue_remaining ?? budgetStatus.data.attempts_remaining) === 0} />
    <MemoryStatus api={api} scope={scope} enabled={budgetStatus.data?.memory_processing === true} />
    <BriefingPanel api={api} scope={scope} run={run} /></>}
  </Stack>;
}
