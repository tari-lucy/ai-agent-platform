import { Alert, Anchor, Badge, Button, Group, Paper, Stack, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import type { RunSession, Scope, WorkSession, WorkTask } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import { Conversation } from './Conversation.tsx';
import { Traffic } from './Traffic.tsx';
import { useHarnessEntry } from '../opencode.ts';
import { useWorkSessions } from './sessions.ts';
import { discussTitle, openCommand, sessionFor } from './conversation.ts';
import {
  abandonCommand, assignCommand, attemptLabels, attemptsOf, experienceOf, finished, goalProblem, groups, MAX_GOAL,
  originLabels, spend, stateLabels, stepLabels, turn,
} from './model.ts';

const when = (value: string) => formatTime(value, 'short');
const MAX_PAGES = 20; // bounded: at most this many pages of each list for one view

function useAll<T extends { items: unknown[]; complete: boolean; next_cursor: string | null }>(
  scope: Scope, resource: string, read: (cursor: string | undefined, signal: AbortSignal) => Promise<T>,
) {
  const query = useInfiniteQuery({
    queryKey: [...projectionKey(resource, scope), 'work'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => read(pageParam, signal),
    getNextPageParam: (last, pages) => (last.complete || !last.next_cursor || pages.length >= MAX_PAGES ? undefined : last.next_cursor),
  });
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  useEffect(() => { if (hasNextPage && !isFetchingNextPage) void fetchNextPage(); }, [hasNextPage, isFetchingNextPage, fetchNextPage]);
  const pages = query.data?.pages ?? [];
  return { query, rows: pages.flatMap((page) => page.items) as T['items'], complete: pages.length > 0 && pages[pages.length - 1].complete };
}

function Assign({ api, scope }: { api: PanelApi; scope: Scope }) {
  const cache = useQueryClient();
  const [goal, setGoal] = useState('');
  const [reason, setReason] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  // One request id per draft: a retry after a lost reply replays instead of assigning twice.
  const pending = useRef<string | null>(null);
  async function submit() {
    const issue = goalProblem(goal);
    if (issue) { setProblem(issue); return; }
    setProblem(null); setSaved(false); setBusy(true);
    pending.current ??= crypto.randomUUID();
    try {
      await api.command(assignCommand(scope, goal, reason, pending.current));
      pending.current = null; setGoal(''); setReason(''); setSaved(true);
    } catch (error) {
      setProblem(failureMessage(error) + ' Повторная отправка использует тот же запрос и не создаст дубль.');
    } finally {
      setBusy(false);
      void cache.invalidateQueries({ queryKey: projectionKey('work-tasks', scope) });
    }
  }
  return <Paper withBorder p="md" aria-label="Новое поручение">
    <Stack gap="xs">
      <Title order={2} size="h5">Поручить</Title>
      <Textarea label="Поручение" autosize minRows={2} maxLength={MAX_GOAL} value={goal}
        onChange={(event) => { setGoal(event.currentTarget.value); pending.current = null; }} />
      <TextInput label="Зачем (необязательно)" value={reason}
        onChange={(event) => { setReason(event.currentTarget.value); pending.current = null; }} />
      {problem && <Text size="sm" c="red.8" role="alert">{problem}</Text>}
      {saved && <Text size="sm" c="teal.8">Поручение сохранено. Агент возьмётся за него после запуска.</Text>}
      <Group><Button onClick={() => void submit()} loading={busy}>Поручить</Button></Group>
    </Stack>
  </Paper>;
}

function Abandon({ api, scope, task }: { api: PanelApi; scope: Scope; task: WorkTask }) {
  const cache = useQueryClient();
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef<string | null>(null);
  async function submit() {
    if (!reason.trim()) { setProblem('Укажите причину.'); return; }
    setProblem(null); setBusy(true);
    pending.current ??= crypto.randomUUID();
    try {
      await api.command(abandonCommand(scope, task, reason, pending.current));
      pending.current = null; setOpen(false);
    } catch (error) {
      if (error instanceof ApiFailure && error.code === 'VERSION_CONFLICT') {
        pending.current = null;
        setProblem('Задача уже изменилась. Посмотрите её новое состояние и решите снова.');
      } else setProblem(failureMessage(error) + ' Повтор использует тот же запрос.');
    } finally {
      setBusy(false);
      void cache.invalidateQueries({ queryKey: projectionKey('work-tasks', scope) });
    }
  }
  if (!open) return <Button variant="subtle" color="red" size="compact-sm" onClick={() => setOpen(true)}>Отменить задачу</Button>;
  return <Stack gap={4}>
    <TextInput label="Причина отмены" value={reason} onChange={(event) => { setReason(event.currentTarget.value); pending.current = null; }} />
    {problem && <Text size="sm" c="red.8" role="alert">{problem}</Text>}
    <Group gap="xs">
      <Button color="red" size="compact-sm" loading={busy} onClick={() => void submit()}>Подтвердить отмену</Button>
      <Button variant="default" size="compact-sm" onClick={() => setOpen(false)}>Не отменять</Button>
    </Group>
  </Stack>;
}

function Discuss({ api, scope, task, sessions, onOpen }: {
  api: PanelApi; scope: Scope; task: WorkTask; sessions: WorkSession[]; onOpen: (id: string) => void;
}) {
  const cache = useQueryClient();
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef<string | null>(null); // one request id per click until it succeeds
  const linked = sessionFor(task.id, sessions);
  async function discuss() {
    if (linked) { onOpen(linked.id); return; }
    setBusy(true); setProblem(null);
    pending.current ??= crypto.randomUUID();
    try {
      const receipt = await api.command(openCommand(scope, discussTitle(task.goal), pending.current, task.id));
      pending.current = null;
      if ('resource_id' in receipt && receipt.resource_id) onOpen(receipt.resource_id);
    } catch (error) {
      setProblem(failureMessage(error) + ' Повтор использует тот же запрос.');
    } finally {
      setBusy(false);
      void cache.invalidateQueries({ queryKey: projectionKey('work-sessions', scope) });
    }
  }
  return <Stack gap={2}>
    <Button variant="light" size="compact-sm" loading={busy} onClick={() => void discuss()}>
      {linked ? 'Открыть разговор' : 'Обсудить с агентом'}
    </Button>
    {problem && <Text size="sm" c="red.8" role="alert">{problem}</Text>}
  </Stack>;
}

type Lists = {
  tickets: Parameters<typeof turn>[1]; attempts: Parameters<typeof attemptsOf>[1]; episodes: Parameters<typeof experienceOf>[1]; running: boolean;
  sessions: WorkSession[]; openSession: (id: string) => void; native: boolean;
};
function Task({ api, scope, task, lists }: { api: PanelApi; scope: Scope; task: WorkTask; lists: Lists }) {
  const tries = attemptsOf(task, lists.attempts);
  const last = tries[tries.length - 1];
  const experience = experienceOf(task, lists.episodes);
  return <Paper withBorder p="sm" component="li" aria-label={task.goal}>
    <Group justify="space-between" wrap="wrap" gap="xs">
      <Text fw={600}>{task.goal}</Text>
      <Group gap={4}>
        <Badge variant="light">{originLabels[task.origin]}</Badge>
        <Badge variant="light" color={finished(task) ? 'gray' : task.state === 'waiting' ? 'orange' : 'blue'}>{stateLabels[task.state]}</Badge>
      </Group>
    </Group>
    {task.reason && <Text size="sm">Причина: {task.reason}</Text>}
    <Text size="sm" c="dimmed">Чей ход: {turn(task, lists.tickets, lists.running)} · попыток {tries.length}{last ? ` · последняя: ${attemptLabels[last.state]}` : ''} · {when(task.updated_at)}</Text>
    {(task.progress ?? []).length > 0 && <Stack gap={2} mt={4} component="ol" style={{ paddingLeft: '1.2rem', margin: 0 }}>
      {(task.progress ?? []).map((step) => <Text key={step.attempt_id} size="sm" component="li">
        {step.capability_id} — {stepLabels[step.status]}: {step.summary.length > 300 ? step.summary.slice(0, 299) + '…' : step.summary}
      </Text>)}
    </Stack>}
    {task.state === 'waiting' && <Text size="sm" mt={4}>Ждёт: <Anchor href="#/tickets">обращения ({(task.waiting_ticket_ids ?? []).length})</Anchor></Text>}
    {(task.result_files ?? []).length > 0 && <Text size="sm">Результаты: {(task.result_files ?? []).map((ref) => `${ref.artifact_id} v${ref.version}`).join(', ')} — <Anchor href="#/library">в библиотеке</Anchor></Text>}
    {experience.length > 0 && <Text size="sm">Стало опытом: {experience.length} {experience.length === 1 ? 'эпизод' : 'эпиз.'} — <Anchor href="#/memory">в памяти</Anchor></Text>}
    {task.outcome && <Text size="sm" mt={4}>{task.outcome}</Text>}
    {!finished(task) && <Group mt={4} align="flex-start">
      {!lists.native && <Discuss api={api} scope={scope} task={task} sessions={lists.sessions} onOpen={lists.openSession} />}
      <Abandon api={api} scope={scope} task={task} />
    </Group>}
  </Paper>;
}

function Section({ title, rows, empty, render }: { title: string; rows: WorkTask[]; empty: string; render: (task: WorkTask) => React.JSX.Element }) {
  return <Stack gap="xs">
    <Title order={2} size="h5">{title}</Title>
    {rows.length ? <Stack gap="xs" component="ul" aria-label={title} style={{ listStyle: 'none', padding: 0, margin: 0 }}>{rows.map(render)}</Stack> :
      <Text size="sm" c="dimmed">{empty}</Text>}
  </Stack>;
}

export function WorkScreen({ api, scope, run }: { api: PanelApi; scope: Scope; run?: RunSession }) {
  const tasks = useAll(scope, 'work-tasks', (cursor, signal) => api.workTasks(scope, signal, cursor));
  const attempts = useAll(scope, 'work-attempts', (cursor, signal) => api.workAttempts(scope, signal, cursor));
  const tickets = useAll(scope, 'tickets', (cursor, signal) => api.tickets(scope, signal, cursor));
  const episodes = useAll(scope, 'episodes', (cursor, signal) => api.episodes(scope, signal, cursor));
  const status = useQuery({ queryKey: projectionKey('private-api-status', scope), queryFn: ({ signal }) => api.privateApiStatus(scope, signal) });
  const off = tasks.query.error instanceof ApiFailure && tasks.query.error.code === 'ACCESS_DENIED';
  const running = run?.state === 'ACTIVE';
  const sessions = useWorkSessions(api, scope);
  // doc33: with the persona's own OpenCode, new work and conversation go there.
  const harness = useHarnessEntry();
  const native = !!harness?.enabled;
  const nativeUrl = harness?.url ?? null;
  const [chosen, setChosen] = useState<string | null>(null);
  const talk = useRef<HTMLDivElement>(null);
  const openSession = (id: string) => { setChosen(id); talk.current?.scrollIntoView({ block: 'start' }); };
  const lists: Lists = {
    tickets: tickets.rows, attempts: attempts.rows, episodes: episodes.rows, running,
    sessions: sessions.data?.items ?? [], openSession, native,
  };
  const { mine, working, done } = groups(tasks.rows, tickets.rows);
  const partial = [tasks, attempts, tickets, episodes].some((list) => list.query.isError || (list.query.data && !list.complete));
  const used = spend(status.data);
  const render = (task: WorkTask) => <Task key={task.id} api={api} scope={scope} task={task} lists={lists} />;
  return <Stack gap="md">
    <div><Title order={1} size="h3">Работа</Title>
      <Text size="sm" c="dimmed">Поручения и собственные цели агента: выбор, шаги, результат и чей ход. Поручить можно и когда агент остановлен — он не запустится сам.</Text>
      {used && <Text size="sm">{used}</Text>}
      {!running && <Text size="sm" c="orange.8">Агент не работает: шаги начнутся только после явного запуска.</Text>}
    </div>
    {off ? <Paper withBorder p="md"><Text size="sm">Контур деятельности не включён на этом хосте (офлайн-команда configure-activity).</Text></Paper> : <>
      <div ref={talk}><Conversation api={api} scope={scope} run={run} chosen={chosen} onSelect={setChosen} reading={native} /></div>
      {native ? <Alert color="blue" title="Поручения и разговор — в OpenCode">
        Новое поручение и разговор с агентом ведите в{' '}
        {nativeUrl ? <Anchor href={nativeUrl} target="_blank" rel="noopener">родном окне OpenCode</Anchor> : 'родном окне OpenCode'}.
        Здесь — ход работы, результаты и прежние разговоры для чтения.
      </Alert> : <Assign api={api} scope={scope} />}
      {partial && <Text size="sm" c="orange.8">Часть данных недоступна или не дочитана — список может быть неполным.</Text>}
      {tasks.query.isError && !off && <Alert color="orange" title="Задачи недоступны">{failureMessage(tasks.query.error)}</Alert>}
      {tasks.query.isPending ? <Text size="sm">Получаем задачи…</Text> : <>
        <Section title="Нужен ваш ответ" rows={mine} empty="Ни одна задача не ждёт вашего ответа." render={render} />
        <Section title="В работе" rows={working} empty="Нет задач в работе." render={render} />
        <Section title="Завершённые" rows={done} empty="Завершённых задач нет." render={render} />
      </>}
      <Traffic api={api} scope={scope} />
    </>}
  </Stack>;
}
