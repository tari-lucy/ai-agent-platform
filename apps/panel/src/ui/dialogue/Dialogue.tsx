import { formatTime } from '../time.ts';
import { Alert, Badge, Button, Group, Paper, Select, Stack, Text, Textarea, Title } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { Briefing, Receipt, RunSession, RunSessionPage, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { acknowledgeReply } from '../../shared/owner-commands.ts';
import { previewKey, type PrivatePreview } from '../../shared/private-preview.ts';
import { projectionKey } from '../../shared/scope.ts';
import {
  afterSend, briefingStatuses, briefingTypes, buildTimeline, canSend, cancelBriefing, createBriefing,
  generating, goal, messageCommand, openOperations, statusLabels, validBriefing, type Composer, type Entry,
} from './model.ts';

const when = (value: string) => formatTime(value, 'medium');

function useProjection<T>(api: PanelApi, scope: Scope, resource: string, read: (scope: Scope, signal: AbortSignal) => Promise<T>) {
  return useQuery({ queryKey: projectionKey(resource, scope), queryFn: ({ signal }) => read(scope, signal) });
}

function EntryRow({ entry }: { entry: Entry }) {
  const color = entry.status === 'done' || entry.status === 'saved' ? 'teal' :
    entry.status === 'failed' || entry.status === 'unknown' ? 'orange' :
      entry.status === 'cancelled' || entry.status === 'removed' ? 'gray' : 'blue';
  return <li className={`entry ${entry.kind}`}>
    <Group justify="space-between" gap="xs">
      <Text size="sm" fw={600}>{entry.author}</Text>
      <Text size="xs" c="dimmed" title={entry.at}>{when(entry.at)}</Text>
    </Group>
    {entry.text === null ? <Text size="sm" c="dimmed" fs="italic">Текст недоступен.</Text> :
      <Text className="plain-text">{entry.text}</Text>}
    <Group gap="xs" mt={4}>
      <Badge variant="light" color={color}>{statusLabels[entry.status]}</Badge>
      <Text size="xs" c="dimmed">{entry.detail}</Text>
    </Group>
  </li>;
}

function Composer({ api, run, scope, budgetBlocked }: { api: PanelApi; run: RunSession | undefined; scope: Scope; budgetBlocked: boolean }) {
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
      if (next.phase === 'saved') setText('');
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
    {phase.phase === 'saved' && <Alert color="teal" mt="sm" role="status">Сообщение принято ядром. Обработка выполняется отдельно.</Alert>}
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

export function Dialogue({ api, scope }: { api: PanelApi; scope: Scope }) {
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = (runs.data as RunSessionPage | undefined)?.items[0];
  const sources = useProjection(api, scope, 'sources', (s, signal) => api.inbound(s, signal));
  const utterances = useProjection(api, scope, 'utterances', (s, signal) => api.utterances(s, signal));
  const deliveries = useProjection(api, scope, 'deliveries', (s, signal) => api.deliveries(s, signal));
  const operations = useProjection(api, scope, 'operations', (s, signal) => api.operations(s, signal));
  const budget = useQuery({ queryKey: projectionKey('private-api-status', scope),
    queryFn: ({ signal }) => api.privateApiStatus(scope, signal), refetchInterval: 15000 });
  const cache = useQueryClient();
  const preview = useQuery<PrivatePreview>({ queryKey: previewKey(scope), enabled: false });
  const [receiving, setReceiving] = useState<string | null>(null);
  const [receiptError, setReceiptError] = useState<string | null>(null);
  async function receiveReply(row: import('../../shared/contracts.ts').Utterance) {
    setReceiving(row.id); setReceiptError(null);
    try { await api.command(acknowledgeReply(row, 'panel-receipt-' + row.id + '-' + row.revision)); }
    catch (error) { setReceiptError(failureMessage(error)); }
    finally {
      setReceiving(null);
      for (const resource of ['deliveries', 'episodes', 'sources']) {
        void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
      }
    }
  }
  const privateRows = (utterances.data?.items ?? []).filter((row) => row.operation_id &&
    row.audience_id === 'operator-panel' && row.scope.run_id === run?.run_id &&
    !(deliveries.data?.items ?? []).some((d) => d.utterance_id === row.id && d.revision === row.revision && d.status === 'DELIVERED'));
  const failed = [sources, utterances, deliveries, operations].filter((query) => query.isError);
  const timeline = buildTimeline(sources.data?.items ?? [], utterances.data?.items ?? [], deliveries.data?.items ?? []);
  const operationRows = operations.data?.items ?? [];
  const summary = goal(run, operationRows);
  const incomplete = [sources, utterances, deliveries].some((query) => query.data && !query.data.complete);
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
      {preview.data?.status === 'preview' && preview.data.scope.run_id === run?.run_id && run?.state === 'ACTIVE' &&
        <Paper withBorder p="xs" mt="sm" aria-label="Предварительный ответ">
          <Text size="xs" c="dimmed">Предварительно{preview.data.incomplete ? ' · поток неполный' : ''}</Text>
          <Text className="plain-text">{preview.data.text}</Text>
        </Paper>}
    </Paper>
    {!!failed.length && <Alert color="orange" title="Часть диалога недоступна">
      {failureMessage(failed[0].error)} Лента может быть неполной. Попытки отправки не повторяются автоматически.</Alert>}
    {incomplete && <Alert color="orange">Показана первая страница записей; более старые не загружены.</Alert>}
    <Paper withBorder p="md" radius="lg">
      <Title order={2} size="h4" mb="sm">Лента</Title>
      {sources.isPending || utterances.isPending ? <Text size="sm">Получаем ленту…</Text> :
        !timeline.length ? <Text size="sm" c="dimmed">Пока нет сообщений.</Text> :
          <ol className="timeline" aria-label="Лента диалога" aria-live="polite">{timeline.map((entry) => <EntryRow key={entry.key} entry={entry} />)}</ol>}
    </Paper>
    {receiptError && <Alert color="orange">{receiptError}</Alert>}
    {run?.state === 'ACTIVE' && privateRows.map((row) =>
      <Button key={row.id} variant="light" disabled={receiving !== null} onClick={() => void receiveReply(row)}>
        Подтвердить получение приватного ответа
      </Button>)}
    {budget.data?.enabled && <Alert color={budget.data.attempts_remaining === 0 ? 'orange' : 'blue'} title="Лимит API">
      Осталось {budget.data.attempts_remaining} из {budget.data.attempt_limit} запросов{budget.data.period === 'utc_day' ? ' за сутки' : ' за весь пробный период'}.
      {budget.data.resets_at && <> Обновление: {when(budget.data.resets_at)}.</>}
      <Text size="xs" mt={4}>Начатые неудачные попытки тоже учитываются. Денежный расход показывается отдельно.</Text>
    </Alert>}
    {budget.isError && <Text size="xs" c="dimmed">Остаток лимита API недоступен. Сервер проверит допуск перед запросом.</Text>}
    <Composer api={api} run={run} scope={scope} budgetBlocked={budget.data?.enabled === true && budget.data.attempts_remaining === 0} />
    <BriefingPanel api={api} scope={scope} run={run} />
  </Stack>;
}
