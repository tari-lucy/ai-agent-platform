import { Alert, Badge, Button, Checkbox, Group, NumberInput, Paper, SegmentedControl, Stack, Text, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { ConsequencePlan, Permission, RunSession, Scope, Ticket } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import {
  conversation, groups, kindLabels, nextConversationCursor, MAX_REPLY, partyLabels, priorityLabels, readCommand, replyProblem,
  stateLabels, statusText, terminal, turnLabel, unread, answerCommand, finishCommand, refuseCommand, noteProblem, actionProblem,
  activeGrants, decisionProblem, executeDecision, grantPreview, levelLabels, proposedBinding, proposedDecision, revokePreview, type Decision, type Level,
} from './model.ts';
import { areaLabels } from '../library/model.ts';

const when = (value: string) => formatTime(value, 'short');
// Bounded: at most this many pages of each list are read for one view.
const MAX_PAGES = 20;

function useAll<T extends { items: unknown[]; complete: boolean; next_cursor: string | null }>(
  scope: Scope, resource: string, read: (cursor: string | undefined, signal: AbortSignal) => Promise<T>,
) {
  const query = useInfiniteQuery({
    queryKey: [...projectionKey(resource, scope), 'tickets'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => read(pageParam, signal),
    getNextPageParam: (last, pages) => (last.complete || !last.next_cursor || pages.length >= MAX_PAGES ? undefined : last.next_cursor),
  });
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  useEffect(() => { if (hasNextPage && !isFetchingNextPage) void fetchNextPage(); }, [hasNextPage, isFetchingNextPage, fetchNextPage]);
  return query;
}

type Result = (text: string) => void;

// One request id per draft: a retry after a lost response replays it instead of posting twice.
// A version conflict keeps the draft and asks to refresh the card.
function useSend(api: PanelApi, scope: Scope) {
  const cache = useQueryClient();
  const [problem, setProblem] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const pending = useRef<string | null>(null);
  // Re-read before acting again: the action stays busy until fresh records arrive.
  const refresh = () => {
    setBusy(true); setStale(false); setProblem(null);
    void Promise.all(['tickets', 'ticket-messages', 'permissions', 'capability-bindings', 'capability-manifests']
      .map((resource) => cache.refetchQueries({ queryKey: projectionKey(resource, scope) }))).finally(() => setBusy(false));
  };
  async function send(make: (requestId: string) => Parameters<PanelApi['command']>[0]): Promise<boolean> {
    setProblem(null); setStale(false); setBusy(true);
    pending.current ??= crypto.randomUUID();
    try {
      await api.command(make(pending.current));
      pending.current = null;
      return true;
    } catch (error) {
      const code = error instanceof ApiFailure ? error.code : undefined;
      if (code === 'VERSION_CONFLICT') { pending.current = null; setStale(true); }
      setProblem(actionProblem(code, failureMessage(error) + ' Повторная отправка использует тот же запрос и не создаст дубль.'));
      return false;
    } finally {
      setBusy(false);
      for (const resource of ['tickets', 'ticket-messages', 'permissions']) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
    }
  }
  return { problem, stale, busy, send, refresh, reset: () => { pending.current = null; } };
}

function Problem({ state }: { state: ReturnType<typeof useSend> }) {
  if (!state.problem) return null;
  return <Alert color="orange" role="alert">{state.problem}
    {state.stale && <Button size="compact-xs" variant="light" ml="xs" onClick={state.refresh}>Обновить карточку</Button>}</Alert>;
}

function Answer({ api, scope, ticket, run, onResult, waiting }: { api: PanelApi; scope: Scope; ticket: Ticket; run?: RunSession; onResult: Result; waiting: boolean }) {
  const [body, setBody] = useState('');
  const [issue, setIssue] = useState<string | null>(null);
  const state = useSend(api, scope);
  async function submit() {
    const problem = replyProblem(body);
    setIssue(problem);
    if (problem) return;
    if (await state.send((id) => answerCommand(scope, ticket, body, id))) {
      setBody('');
      onResult(run?.state === 'ACTIVE' ? 'Ответ отправлен. Агент продолжит по своему порядку.' :
        'Ответ отправлен. Агент остановлен: он прочитает ответ только после явного START.');
    }
  }
  return <Stack gap={6} aria-label="Ответ на обращение">
    <Textarea label={terminal(ticket) ? 'Ваш ответ (вернёт обращение в работу)' : 'Ваш ответ'} autosize minRows={2} maxLength={MAX_REPLY}
      value={body} onChange={(e) => { setBody(e.currentTarget.value); state.reset(); }} />
    {ticket.kind === 'capability_request' && <Text size="xs" c="dimmed">Ответ текстом не выдаёт доступ, даже если в нём написано «разрешаю». Для доступа — кнопка «Разрешить».</Text>}
    <Group justify="space-between">
      <Text size="xs" c="dimmed">{run?.state === 'ACTIVE' ? 'Агент работает.' : 'Агент остановлен: ответ сохранится и будет прочитан после START.'}</Text>
      <Button size="compact-sm" loading={state.busy} disabled={waiting} onClick={() => void submit()}>Отправить ответ</Button>
    </Group>
    {issue && <Alert color="orange" role="alert">{issue}</Alert>}
    <Problem state={state} />
  </Stack>;
}

// A separate, explained closing step; it records the end of the conversation and grants nothing.
function Closing({ api, scope, ticket, onResult, refuse, waiting }: { api: PanelApi; scope: Scope; ticket: Ticket; onResult: Result; refuse: boolean; waiting: boolean }) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState('');
  const state = useSend(api, scope);
  const label = refuse ? 'Отказать' : 'Завершить обращение';
  async function submit() {
    if (noteProblem(note)) return;
    const ok = await state.send((id) => refuse ? refuseCommand(scope, ticket, note, id) : finishCommand(scope, ticket, note, id));
    if (ok) { setOpen(false); setNote(''); onResult(refuse ? 'Запрос закрыт без выдачи доступа.' : 'Обращение завершено. Права при этом не выдаются.'); }
  }
  if (!open) return <Button size="compact-sm" variant="subtle" color={refuse ? 'red' : 'gray'} disabled={waiting} onClick={() => setOpen(true)}>{label}…</Button>;
  return <Paper withBorder p="sm" aria-label={refuse ? 'Отказ в доступе' : 'Завершение обращения'}>
    <Text size="sm">{refuse ? 'Запрос будет закрыт без выдачи доступа. Агент увидит отказ и не будет ждать этой возможности.'
      : 'Обращение уйдёт в завершённые: агент увидит, что вопрос закрыт. Никаких прав это не выдаёт.'}</Text>
    <Textarea mt={6} label="Комментарий для агента (необязательно)" autosize minRows={1} maxLength={MAX_REPLY} value={note}
      onChange={(e) => { setNote(e.currentTarget.value); state.reset(); }} />
    <Group mt={6}><Button size="compact-sm" color={refuse ? 'red' : 'gray'} loading={state.busy} disabled={waiting} onClick={() => void submit()}>{refuse ? 'Отказать в доступе' : 'Завершить'}</Button>
      <Button size="compact-sm" variant="subtle" color="gray" onClick={() => setOpen(false)}>Отмена</Button></Group>
    <Problem state={state} />
  </Paper>;
}

function Details({ summary, children }: { summary: string; children: ReactNode }) {
  return <details className="ticket-details"><summary><Text span size="xs" c="dimmed">{summary}</Text></summary>
    <Stack gap={4} mt={4}>{children}</Stack></details>;
}

// Preview first: the owner states the consequences of this exact decision, then the operator confirms.
function usePlan(api: PanelApi, scope: Scope) {
  const cache = useQueryClient();
  const [plan, setPlan] = useState<ConsequencePlan | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const executing = useRef<string | null>(null);
  const explain = (error: unknown) => {
    const code = error instanceof ApiFailure ? error.code : undefined;
    setStale(code === 'VERSION_CONFLICT');
    return actionProblem(code, failureMessage(error));
  };
  const reload = () => Promise.all(['permissions', 'tickets', 'ticket-messages', 'capability-bindings', 'capability-manifests']
    .map((resource) => cache.refetchQueries({ queryKey: projectionKey(resource, scope) })));
  // After a conflict the card is read again before a new preview can be made from it.
  const refresh = () => {
    setBusy(true); setStale(false); setProblem(null); setPlan(null);
    void reload().finally(() => setBusy(false));
  };
  async function preview(command: Parameters<PanelApi['command']>[0]) {
    setProblem(null); setStale(false); setBusy(true); executing.current = null;
    try {
      const receipt = await api.command(command);
      if (!receipt.resource_id) throw new ApiFailure('NETWORK');
      setPlan(await api.plan(scope, receipt.resource_id));
    } catch (error) { setPlan(null); setProblem(explain(error)); } finally { setBusy(false); }
  }
  async function confirm(capabilityId: string): Promise<boolean> {
    if (!plan) return false;
    setProblem(null); setBusy(true);
    executing.current ??= crypto.randomUUID();
    try {
      await api.command(executeDecision(scope, capabilityId, plan.id, executing.current));
      setPlan(null); executing.current = null;
      return true;
    } catch (error) {
      const message = explain(error);
      if (error instanceof ApiFailure && error.code === 'VERSION_CONFLICT') {
        // The plan was for a version that no longer exists: it cannot be confirmed any more.
        setPlan(null); executing.current = null; setProblem(message);
      } else setProblem(message + ' Повтор использует тот же запрос.');
      return false;
    }
    finally { setBusy(false); void reload(); }
  }
  return { plan, problem, stale, busy, preview, confirm, refresh, cancel: () => { setPlan(null); executing.current = null; } };
}

function PlanView({ state, capabilityId, confirmLabel, onDone, summary }: {
  state: ReturnType<typeof usePlan>; capabilityId: string; confirmLabel: string; onDone: () => void; summary?: ReactNode;
}) {
  return <>
    {state.plan && <Paper withBorder p="sm" aria-label="Последствия решения">
      {summary}
      <Text size="sm" fw={600}>Что произойдёт</Text>
      <Text size="sm" className="plain-text">{state.plan.consequences}</Text>
      <Group mt="xs"><Button size="compact-sm" color="orange" loading={state.busy}
        onClick={() => void state.confirm(capabilityId).then((ok) => { if (ok) onDone(); })}>{confirmLabel}</Button>
        <Button size="compact-sm" variant="subtle" color="gray" onClick={state.cancel}>Отмена</Button></Group></Paper>}
    {state.problem && <Alert color="orange" role="alert">{state.problem}
      {state.stale && <Button size="compact-xs" variant="light" ml="xs" onClick={state.refresh}>Обновить карточку</Button>}</Alert>}
  </>;
}

const areaList = (items: string[]) => items.map((item) => areaLabels[item as keyof typeof areaLabels] ?? item).join(', ');

function AccessRequest({ api, scope, ticket, onResult, waiting }: { api: PanelApi; scope: Scope; ticket: Ticket; onResult: Result; waiting: boolean }) {
  const request = ticket.request!;
  const manifests = useQuery({ queryKey: [...projectionKey('capability-manifests', scope), 'tickets'], queryFn: ({ signal }) => api.capabilityManifests(scope, signal) });
  const bindings = useQuery({ queryKey: [...projectionKey('capability-bindings', scope), 'tickets'], queryFn: ({ signal }) => api.capabilityBindings(scope, signal) });
  const permissions = useQuery({ queryKey: [...projectionKey('permissions', scope), 'tickets'], queryFn: ({ signal }) => api.permissions(scope, signal) });
  const manifest = manifests.data?.items.find((row) => row.capability_id === request.capability_id);
  const binding = proposedBinding(bindings.data?.items ?? [], request.capability_id);
  const [now] = useState(() => Date.now());
  const grants = activeGrants(permissions.data?.items ?? [], request.capability_id, now);
  const [edited, setEdited] = useState<Decision | null>(null);
  const grant = usePlan(api, scope);
  const revoke = usePlan(api, scope);
  if (manifests.isPending || bindings.isPending) return <Text size="sm">Получаем описание возможности…</Text>;
  if (manifests.isError || bindings.isError) return <Alert color="orange">Не удалось получить описание возможности. Обновите страницу; ответить текстом или отказать можно и сейчас.</Alert>;
  if (!manifest) return <Alert color="gray">Эта возможность не зарегистрирована на этом host, поэтому выдать доступ здесь нельзя. Ответьте агенту текстом или откажите.</Alert>;
  const decision = edited ?? proposedDecision(ticket, manifest, binding);
  const allowed = (manifest.areas ?? []) as string[];
  const until = new Date(now + decision.days * 86400000);
  const set = (change: Partial<Decision>) => { setEdited({ ...decision, ...change }); grant.cancel(); };
  const problem = binding ? decisionProblem(decision, [binding]) : null;
  return <Paper withBorder p="sm" aria-label="Запрос доступа">
    <Text size="sm" fw={600}>Нужна возможность: {manifest.summary}</Text>
    <Stack gap={2} mt={4}>
      <Text size="sm">Области: {decision.areas.length ? areaList(decision.areas) : 'без доступа к файлам'}</Text>
      <Text size="sm">Срок: {decision.days} дн. (до {when(until.toISOString())})</Text>
      <Text size="sm">Подключение: {!binding ? 'нет' : binding.state === 'ready' ? 'проверено' : 'ещё не готово — доступ заработает после проверки'}</Text>
    </Stack>
    {grants.map((row: Permission) => <Group key={row.id} justify="space-between" mt={6}>
      <Text size="sm" c="teal.8">Доступ уже выдан до {when(row.expires_at)}</Text>
      <Button size="compact-xs" variant="subtle" color="red" onClick={() => void revoke.preview(revokePreview(scope, row, crypto.randomUUID()))}>Отозвать…</Button></Group>)}
    <PlanView state={revoke} capabilityId={request.capability_id} confirmLabel="Подтвердить отзыв" onDone={() => onResult('Доступ отозван.')} />
    {!binding ? <Alert color="orange" mt="xs">Для этой возможности на host нет подключения, поэтому доступ выдать нельзя. Подготовьте подключение
      в настройке host и вернитесь к этому обращению, либо откажите.</Alert> :
      <Group mt="xs"><Button size="compact-sm" loading={grant.busy} disabled={!!problem || waiting}
        onClick={() => void grant.preview(grantPreview(scope, ticket, decision, crypto.randomUUID()))}>Разрешить…</Button></Group>}
    {problem && <Alert color="orange" role="alert" mt={4}>{problem}</Alert>}
    <PlanView state={grant} capabilityId={request.capability_id} confirmLabel="Подтвердить выдачу доступа"
      onDone={() => onResult(`Доступ выдан до ${when(until.toISOString())}. Агента это не запускает.`)}
      summary={<Stack gap={2} mb={6} aria-label="Что вы разрешаете">
        <Text size="sm" fw={600}>Что вы разрешаете</Text>
        <Text size="sm">Возможность: {manifest.summary}</Text>
        <Text size="sm">Области: {decision.areas.length ? areaList(decision.areas) : 'без доступа к файлам'}</Text>
        <Text size="sm">Срок: {decision.days} дн., до {when(until.toISOString())} · уровень: {levelLabels[decision.level]}</Text>
        <Text size="sm" className="plain-text">Ограничения: {decision.constraints.trim() || 'не указаны'}</Text>
      </Stack>} />
    <Details summary="Подробности решения">
      <Text size="xs" c="dimmed">Возможность: {request.capability_id} · эффект: {manifest.effect} · подключение: {binding ? `${binding.binding_id} (${binding.adapter_id}, ${binding.state})` : 'нет'} · обращение версии {ticket.version}</Text>
      <SegmentedControl size="xs" value={decision.level} onChange={(value) => set({ level: value as Level })}
        data={(Object.keys(levelLabels) as Level[]).map((value) => ({ value, label: levelLabels[value] }))} />
      <NumberInput label="Срок, дней" min={1} max={365} value={decision.days} onChange={(value) => set({ days: Number(value) })} />
      {!!allowed.length && <Group gap="xs">{allowed.map((item) => <Checkbox key={item} label={areaLabels[item as keyof typeof areaLabels] ?? item} checked={decision.areas.includes(item)}
        onChange={(e) => set({ areas: e.currentTarget.checked ? [...decision.areas, item] : decision.areas.filter((x) => x !== item) })} />)}</Group>}
      <Textarea label="Ограничения" autosize minRows={1} maxLength={4000} value={decision.constraints} onChange={(e) => set({ constraints: e.currentTarget.value })} />
    </Details>
  </Paper>;
}

// One ticket's conversation, read from its own id onward; bounded to MAX_PAGES pages.
function useConversation(api: PanelApi, scope: Scope, ticket: Ticket) {
  const query = useInfiniteQuery({
    queryKey: [...projectionKey('ticket-messages', scope), 'tickets', ticket.id], initialPageParam: ticket.id as string | undefined,
    queryFn: ({ pageParam, signal }) => api.ticketMessages(scope, signal, pageParam),
    getNextPageParam: (last, pages) => (pages.length >= MAX_PAGES ? undefined : nextConversationCursor(last, ticket.id)),
  });
  const { hasNextPage, isFetchingNextPage, fetchNextPage } = query;
  useEffect(() => { if (hasNextPage && !isFetchingNextPage) void fetchNextPage(); }, [hasNextPage, isFetchingNextPage, fetchNextPage]);
  return { messages: conversation(query.data?.pages.flatMap((page) => page.items) ?? [], ticket.id), failed: query.isError };
}

function Card({ api, scope, ticket, run }: { api: PanelApi; scope: Scope; ticket: Ticket; run?: RunSession }) {
  const cache = useQueryClient();
  const { messages, failed } = useConversation(api, scope, ticket);
  const [result, setResult] = useState<string | null>(null);
  // Marking as read changes the ticket version: actions wait until the card is read again,
  // so a quick click is not sent against the version just replaced.
  const [reading, setReading] = useState(false);
  const [readFailed, setReadFailed] = useState<string | null>(null);
  const marked = useRef<string | null>(null);
  const versionKey = ticket.id + ':' + ticket.version;
  useEffect(() => {
    // Opening an unread ticket records that this exact version was seen, once per version.
    if (!unread(ticket) || marked.current === versionKey) return;
    marked.current = versionKey;
    setReading(true);
    api.command(readCommand(scope, ticket, crypto.randomUUID()))
      .catch(() => setReadFailed(versionKey))
      // A fresh read (cancelling any read already in flight) before actions are released,
      // so they never use the version the read mark has just replaced.
      .then(() => cache.refetchQueries({ queryKey: projectionKey('tickets', scope) }))
      .finally(() => setReading(false));
  }, [api, cache, scope, ticket, versionKey]);
  // Also before the effect runs: an unread version is about to change by the read mark.
  const settling = reading || (unread(ticket) && readFailed !== versionKey);
  const access = ticket.kind === 'capability_request' && !!ticket.request;
  return <Paper withBorder p="md" radius="lg" aria-label="Карточка обращения">
    <Group gap="xs"><Badge variant="light">{kindLabels[ticket.kind]}</Badge>
      <Badge variant="light" color={ticket.next_turn === 'operator' ? 'orange' : terminal(ticket) ? 'gray' : 'blue'}>{statusText(ticket)}</Badge></Group>
    <Title order={2} size="h4" mt="xs" className="plain-text">{ticket.title}</Title>
    <Stack gap={6} mt="sm">
      <div><Text size="sm" fw={600}>Что случилось</Text><Text size="sm" className="plain-text">{ticket.summary}</Text></div>
      <div><Text size="sm" fw={600}>Чего агент ждёт от вас</Text><Text size="sm" className="plain-text">{ticket.decision_needed}</Text></div>
      {access && ticket.request!.constraints && <div><Text size="sm" fw={600}>Зачем и с какими ограничениями</Text>
        <Text size="sm" className="plain-text">{ticket.request!.constraints}</Text></div>}
      {ticket.resolution != null && <Text size="sm" className="plain-text">Итог: {ticket.resolution}</Text>}
      {result && <Alert color="teal" role="status">{result}</Alert>}
      {access && !terminal(ticket) && <AccessRequest api={api} scope={scope} ticket={ticket} onResult={setResult} waiting={settling} />}
      <Details summary="Подробности обращения">
        <Text size="xs">Что уже попробовано: {ticket.attempted}</Text>
        <Text size="xs" c="dimmed">{turnLabel(ticket)} · состояние: {stateLabels[ticket.state]} · оценка агента: {priorityLabels[ticket.agent_priority] ?? ticket.agent_priority}
          {' '}· версия {ticket.version} · {when(ticket.updated_at)}{(ticket.occurrences ?? 1) > 1 ? ` · повторялось: ${ticket.occurrences}` : ''}
          {ticket.technical_refusal ? ' · технический отказ' : ''}</Text>
        {!!ticket.task_ids?.length && <Text size="xs" c="dimmed">Связанные задачи: {ticket.task_ids.join(', ')}</Text>}
        <Text size="xs" c="dimmed">Идентификатор: {ticket.id}</Text>
      </Details>
    </Stack>
    <Title order={3} size="h5" mt="md">Переписка ({messages.length})</Title>
    {(failed || messages.length < ticket.message_count) && <Text size="xs" c="orange.8">Загружены не все сообщения.</Text>}
    <ol className="memory-list" aria-label="Сообщения обращения">{messages.map((message) => <li key={message.id}>
      <Paper withBorder p="xs" mb={4}><Text size="xs" c="dimmed">{partyLabels[message.author]} · {when(message.created_at)}</Text>
        <Text size="sm" className="plain-text">{message.body}</Text></Paper></li>)}</ol>
    <Answer key={ticket.id} api={api} scope={scope} ticket={ticket} run={run} onResult={setResult} waiting={settling} />
    {!terminal(ticket) && <Group mt="xs"><Closing api={api} scope={scope} ticket={ticket} onResult={setResult} refuse={access} waiting={settling} /></Group>}
  </Paper>;
}

function List({ title, rows, selected, onSelect, empty }: { title: string; rows: Ticket[]; selected: string | null; onSelect: (id: string) => void; empty: string }) {
  return <div><Title order={2} size="h5" mb={4}>{title} ({rows.length})</Title>
    {!rows.length ? <Text size="sm" c="dimmed">{empty}</Text> :
      <ul className="memory-list" aria-label={title}>{rows.map((row) => <li key={row.id}>
        <button type="button" className={`memory-item ${row.id === selected ? 'selected' : ''}`} onClick={() => onSelect(row.id)}>
          <Group gap={6} wrap="nowrap"><Text size="sm" fw={600} className="plain-text">{row.title}</Text>
            {unread(row) && <Badge size="xs" color="orange">новое</Badge>}</Group>
          <Text size="xs" c="dimmed">{kindLabels[row.kind]} · {stateLabels[row.state]} · {when(row.updated_at)}</Text>
        </button></li>)}</ul>}</div>;
}

export function TicketsScreen({ api, scope, run }: { api: PanelApi; scope: Scope; run?: RunSession }) {
  const tickets = useAll(scope, 'tickets', (cursor, signal) => api.tickets(scope, signal, cursor));
  const [selected, setSelected] = useState<string | null>(null);
  const rows = tickets.data?.pages.flatMap((page) => page.items) ?? [];
  const { mine, working, done } = groups(rows);
  const ticket = rows.find((row) => row.id === selected) ?? null;
  const failed = tickets.isError ? tickets : undefined;
  return <Stack gap="md">
    <div><Title order={1} size="h3">Обращения</Title>
      <Text size="sm" c="dimmed">Вопросы, проблемы, идеи и запросы доступа от агента. Ответить можно и когда агент остановлен;
        открытие карточки и ответ сами агента не запускают и модель не вызывают.</Text>
      <Text size="sm" mt={4} aria-label="Профиль обращений">Профиль: <strong>{scope.persona_id}</strong> ·
        {scope.memory_mode === 'test' ? ' тестовая память' : ' рабочая память'} ({scope.memory_space_id})</Text></div>
    {failed && <Alert color="orange" title="Часть данных недоступна">{failureMessage(failed.error)}</Alert>}
    {tickets.isPending ? <Text size="sm">Получаем обращения…</Text> : <Group align="flex-start" grow wrap="wrap">
      <Stack gap="md" style={{ flex: '1 1 18rem' }}>
        <List title="Нужен мой ответ" rows={mine} selected={selected} onSelect={setSelected} empty="Ничего не ждёт вашего ответа." />
        <List title="В работе" rows={working} selected={selected} onSelect={setSelected} empty="Нет обращений в работе." />
        <List title="Завершённые" rows={done} selected={selected} onSelect={setSelected} empty="Завершённых нет." />
      </Stack>
      <div style={{ flex: '1 1 28rem' }}>{ticket ?
        <Card key={ticket.id} api={api} scope={scope} ticket={ticket} run={run} /> :
        <Alert color="gray">Выберите обращение, чтобы увидеть переписку и ответить.</Alert>}</div>
    </Group>}
  </Stack>;
}
