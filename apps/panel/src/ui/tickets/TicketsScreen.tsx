import { Alert, Badge, Button, Checkbox, Group, NumberInput, Paper, SegmentedControl, Select, Stack, Text, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useRef, useState } from 'react';
import type { ConsequencePlan, Permission, RunSession, Scope, Ticket, TicketMessage } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import {
  conversation, groups, kindLabels, MAX_REPLY, outcomeLabels, partyLabels, priorityLabels, readCommand, replyCommand, replyProblem,
  stateLabels, terminal, turnLabel, unread, type Outcome,
  activeGrants, decisionProblem, executeDecision, grantPreview, levelLabels, revokePreview, type Decision, type Level,
} from './model.ts';

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

function Reply({ api, scope, ticket, run }: { api: PanelApi; scope: Scope; ticket: Ticket; run?: RunSession }) {
  const cache = useQueryClient();
  const [body, setBody] = useState('');
  const [outcome, setOutcome] = useState<Outcome>('answer');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  // One request id per draft: a retry after a lost response replays it instead of posting twice.
  const pending = useRef<string | null>(null);
  async function submit() {
    const issue = replyProblem(body);
    if (issue) { setProblem(issue); return; }
    setProblem(null); setSaved(false); setBusy(true);
    pending.current ??= crypto.randomUUID();
    try {
      await api.command(replyCommand(scope, ticket, body, outcome, pending.current));
      pending.current = null; setBody(''); setOutcome('answer'); setSaved(true);
    } catch (error) {
      if (error instanceof ApiFailure && error.code === 'VERSION_CONFLICT') {
        pending.current = null;
        setProblem('Обращение уже изменилось. Прочитайте новые сообщения; ваш текст сохранён в поле.');
      } else {
        setProblem(failureMessage(error) + ' Повторная отправка использует тот же запрос и не создаст дубль.');
      }
    } finally {
      setBusy(false);
      void cache.invalidateQueries({ queryKey: projectionKey('tickets', scope) });
      void cache.invalidateQueries({ queryKey: projectionKey('ticket-messages', scope) });
    }
  }
  return <Stack gap={6} aria-label="Ответ на обращение">
    <Textarea label={terminal(ticket) ? 'Сообщение (вернёт обращение в работу)' : 'Сообщение'} autosize minRows={2} maxLength={MAX_REPLY}
      value={body} onChange={(e) => { setBody(e.currentTarget.value); pending.current = null; }} />
    <SegmentedControl size="xs" value={outcome} onChange={(value) => { setOutcome(value as Outcome); pending.current = null; }}
      data={(Object.keys(outcomeLabels) as Outcome[]).map((value) => ({ value, label: outcomeLabels[value] }))} />
    <Group justify="space-between">
      <Text size="xs" c="dimmed">{run?.state === 'ACTIVE' ? 'Агент продолжит зависимую задачу по своему порядку.' :
        'Агент остановлен: ответ сохранится и будет обработан только после явного START.'}</Text>
      <Button size="compact-sm" variant="light" loading={busy} onClick={() => void submit()}>Отправить</Button>
    </Group>
    {problem && <Alert color="orange" role="alert">{problem}</Alert>}
    {saved && <Alert color="teal" role="status">Сообщение сохранено.</Alert>}
  </Stack>;
}

// Preview first: the owner states the consequences of this exact decision, then the operator confirms.
function usePlan(api: PanelApi, scope: Scope) {
  const cache = useQueryClient();
  const [plan, setPlan] = useState<ConsequencePlan | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const executing = useRef<string | null>(null);
  const explain = (error: unknown) => error instanceof ApiFailure && error.code === 'ACCESS_DENIED' ?
    'Решение выше разрешённого потолка host, вне областей возможности или без доступа.' :
    error instanceof ApiFailure && error.code === 'VERSION_CONFLICT' ? 'Обращение или право изменилось. Обновите и проверьте заново.' :
    failureMessage(error);
  async function preview(command: Parameters<PanelApi['command']>[0]) {
    setProblem(null); setDone(null); setBusy(true); executing.current = null;
    try {
      const receipt = await api.command(command);
      if (!receipt.resource_id) throw new ApiFailure('NETWORK');
      setPlan(await api.plan(scope, receipt.resource_id));
    } catch (error) { setPlan(null); setProblem(explain(error)); } finally { setBusy(false); }
  }
  async function confirm(capabilityId: string, message: string) {
    if (!plan) return;
    setProblem(null); setBusy(true);
    executing.current ??= crypto.randomUUID();
    try {
      await api.command(executeDecision(scope, capabilityId, plan.id, executing.current));
      setPlan(null); executing.current = null; setDone(message);
    } catch (error) { setProblem(explain(error) + ' Повтор использует тот же запрос.'); }
    finally {
      setBusy(false);
      for (const resource of ['permissions', 'tickets', 'ticket-messages']) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
    }
  }
  return { plan, problem, done, busy, preview, confirm, cancel: () => { setPlan(null); executing.current = null; } };
}

function PlanView({ state, capabilityId, message }: { state: ReturnType<typeof usePlan>; capabilityId: string; message: string }) {
  return <>
    {state.plan && <Paper withBorder p="sm" aria-label="Последствия решения"><Text size="sm" className="plain-text">{state.plan.consequences}</Text>
      <Group mt="xs"><Button size="compact-sm" color="orange" loading={state.busy} onClick={() => void state.confirm(capabilityId, message)}>Подтвердить</Button>
        <Button size="compact-sm" variant="subtle" color="gray" onClick={state.cancel}>Отмена</Button></Group></Paper>}
    {state.problem && <Alert color="orange" role="alert">{state.problem}</Alert>}
    {state.done && <Alert color="teal" role="status">{state.done}</Alert>}
  </>;
}

function PermissionDecision({ api, scope, ticket }: { api: PanelApi; scope: Scope; ticket: Ticket }) {
  const request = ticket.request!;
  const manifests = useQuery({ queryKey: [...projectionKey('capability-manifests', scope), 'tickets'], queryFn: ({ signal }) => api.capabilityManifests(scope, signal) });
  const bindings = useQuery({ queryKey: [...projectionKey('capability-bindings', scope), 'tickets'], queryFn: ({ signal }) => api.capabilityBindings(scope, signal) });
  const permissions = useQuery({ queryKey: [...projectionKey('permissions', scope), 'tickets'], queryFn: ({ signal }) => api.permissions(scope, signal) });
  const manifest = manifests.data?.items.find((row) => row.capability_id === request.capability_id);
  const options = (bindings.data?.items ?? []).filter((row) => row.capability_id === request.capability_id && row.state !== 'revoked');
  const [now] = useState(() => Date.now());
  const grants = activeGrants(permissions.data?.items ?? [], request.capability_id, now);
  const allowedAreas = (manifest?.areas ?? []) as string[];
  const [decision, setDecision] = useState<Decision>({ bindingId: '', level: 'prepare_only', days: 7,
    areas: (request.areas ?? []).filter((item) => allowedAreas.includes(item)), constraints: request.constraints });
  const [problem, setProblem] = useState<string | null>(null);
  const grant = usePlan(api, scope);
  const revoke = usePlan(api, scope);
  if (manifests.isPending || bindings.isPending) return <Text size="sm">Получаем описание возможности…</Text>;
  if (!manifest) return <Text size="sm" c="dimmed">Возможность «{request.capability_id}» не зарегистрирована в рабочем месте или решения по правам выключены на этом host. Ответьте в переписке.</Text>;
  function submit() {
    const issue = decisionProblem(decision, options);
    setProblem(issue);
    if (!issue) void grant.preview(grantPreview(scope, ticket, decision, crypto.randomUUID()));
  }
  return <Paper withBorder p="sm" aria-label="Решение по праву">
    <Text size="sm" fw={600}>Решение по праву: {request.capability_id}</Text>
    <Text size="xs" c="dimmed">{manifest.summary} · эффект: {manifest.effect}</Text>
    {grants.map((row: Permission) => <Group key={row.id} justify="space-between" mt={4}>
      <Text size="sm">Выдано: {levelLabels[row.level as Level] ?? row.level} до {when(row.expires_at)}</Text>
      <Button size="compact-xs" variant="subtle" color="red" onClick={() => void revoke.preview(revokePreview(scope, row, crypto.randomUUID()))}>Отозвать…</Button></Group>)}
    <PlanView state={revoke} capabilityId={request.capability_id} message="Право отозвано." />
    <Stack gap={6} mt="xs">
      <Select label="Подключение" value={decision.bindingId || null} placeholder={options.length ? 'Выберите' : 'Нет подключений'}
        data={options.map((row) => ({ value: row.binding_id, label: `${row.adapter_id} · ${row.state === 'ready' ? 'проверено' : 'не готово'}` }))}
        onChange={(value) => setDecision({ ...decision, bindingId: value ?? '' })} />
      <SegmentedControl size="xs" value={decision.level} onChange={(value) => setDecision({ ...decision, level: value as Level })}
        data={(Object.keys(levelLabels) as Level[]).map((value) => ({ value, label: levelLabels[value] }))} />
      <NumberInput label="Срок, дней" min={1} max={365} value={decision.days} onChange={(value) => setDecision({ ...decision, days: Number(value) })} />
      {!!allowedAreas.length && <Group gap="xs">{allowedAreas.map((item) => <Checkbox key={item} label={item} checked={decision.areas.includes(item)}
        onChange={(e) => setDecision({ ...decision, areas: e.currentTarget.checked ? [...decision.areas, item] : decision.areas.filter((x) => x !== item) })} />)}</Group>}
      <Textarea label="Ограничения" autosize minRows={1} maxLength={4000} value={decision.constraints} onChange={(e) => setDecision({ ...decision, constraints: e.currentTarget.value })} />
      <Group justify="flex-end"><Button size="compact-sm" variant="light" loading={grant.busy} onClick={submit}>Предпросмотр решения</Button></Group>
      {problem && <Alert color="orange" role="alert">{problem}</Alert>}
      <PlanView state={grant} capabilityId={request.capability_id} message="Право выдано. Подключение и запуск — отдельно." />
    </Stack>
  </Paper>;
}

function Card({ api, scope, ticket, messages, run }: { api: PanelApi; scope: Scope; ticket: Ticket; messages: TicketMessage[]; run?: RunSession }) {
  const cache = useQueryClient();
  const marked = useRef<string | null>(null);
  useEffect(() => {
    // Opening an unread ticket records that this exact version was seen, once per version.
    const key = ticket.id + ':' + ticket.version;
    if (!unread(ticket) || marked.current === key) return;
    marked.current = key;
    api.command(readCommand(scope, ticket, crypto.randomUUID()))
      .catch(() => {})
      .finally(() => { void cache.invalidateQueries({ queryKey: projectionKey('tickets', scope) }); });
  }, [api, cache, scope, ticket]);
  const request = ticket.request;
  return <Paper withBorder p="md" radius="lg" aria-label="Карточка обращения">
    <Group gap="xs"><Badge variant="light">{kindLabels[ticket.kind]}</Badge><Badge variant="light" color={ticket.next_turn === 'operator' ? 'orange' : 'gray'}>{stateLabels[ticket.state]}</Badge>
      {ticket.technical_refusal && <Badge color="red" variant="light">технический отказ</Badge>}
      {(ticket.occurrences ?? 1) > 1 && <Badge color="gray" variant="outline">повторялось: {ticket.occurrences}</Badge>}</Group>
    <Title order={2} size="h4" mt="xs" className="plain-text">{ticket.title}</Title>
    <Text size="xs" c="dimmed">{turnLabel(ticket)} · оценка агента: {priorityLabels[ticket.agent_priority] ?? ticket.agent_priority} · версия {ticket.version} · {when(ticket.updated_at)}</Text>
    <Stack gap={6} mt="sm">
      <div><Text size="sm" fw={600}>Зачем</Text><Text size="sm" className="plain-text">{ticket.summary}</Text></div>
      <div><Text size="sm" fw={600}>Что уже попробовано</Text><Text size="sm" className="plain-text">{ticket.attempted}</Text></div>
      <div><Text size="sm" fw={600}>Какое решение нужно</Text><Text size="sm" className="plain-text">{ticket.decision_needed}</Text></div>
      {!!ticket.task_ids?.length && <Text size="sm">Связанные задачи: {ticket.task_ids.join(', ')}</Text>}
      {request && <Paper withBorder p="sm" bg="gray.0" aria-label="Запрошенная возможность">
        <Text size="sm" fw={600}>Запрошена возможность: {request.capability_id}</Text>
        <Text size="sm" className="plain-text">{request.constraints}</Text>
        <Text size="xs" c="dimmed" mt={4}>Ответ текстом не выдаёт права и не подключает инструмент. Изменение прав — отдельное решение.</Text>
      </Paper>}
      {ticket.resolution != null && <Text size="sm" className="plain-text">Итог: {ticket.resolution}</Text>}
      {request && !terminal(ticket) && <PermissionDecision api={api} scope={scope} ticket={ticket} />}
    </Stack>
    <Title order={3} size="h5" mt="md">Переписка ({messages.length})</Title>
    {messages.length < ticket.message_count && <Text size="xs" c="orange.8">Загружены не все сообщения.</Text>}
    <ol className="memory-list" aria-label="Сообщения обращения">{messages.map((message) => <li key={message.id}>
      <Paper withBorder p="xs" mb={4}><Text size="xs" c="dimmed">{partyLabels[message.author]} · {message.author_id} · {when(message.created_at)}</Text>
        <Text size="sm" className="plain-text">{message.body}</Text></Paper></li>)}</ol>
    <Reply key={ticket.id} api={api} scope={scope} ticket={ticket} run={run} />
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
  const messages = useAll(scope, 'ticket-messages', (cursor, signal) => api.ticketMessages(scope, signal, cursor));
  const [selected, setSelected] = useState<string | null>(null);
  const rows = tickets.data?.pages.flatMap((page) => page.items) ?? [];
  const all = messages.data?.pages.flatMap((page) => page.items) ?? [];
  const { mine, working, done } = groups(rows);
  const ticket = rows.find((row) => row.id === selected) ?? null;
  const failed = [tickets, messages].find((query) => query.isError);
  return <Stack gap="md">
    <div><Title order={1} size="h3">Обращения</Title>
      <Text size="sm" c="dimmed">Вопросы, проблемы, идеи и запросы возможностей от агента. Ответить можно и когда агент остановлен.</Text></div>
    {failed && <Alert color="orange" title="Часть данных недоступна">{failureMessage(failed.error)}</Alert>}
    {tickets.isPending ? <Text size="sm">Получаем обращения…</Text> : <Group align="flex-start" grow wrap="wrap">
      <Stack gap="md" style={{ flex: '1 1 18rem' }}>
        <List title="Нужен мой ответ" rows={mine} selected={selected} onSelect={setSelected} empty="Ничего не ждёт вашего ответа." />
        <List title="В работе" rows={working} selected={selected} onSelect={setSelected} empty="Нет обращений в работе." />
        <List title="Завершённые" rows={done} selected={selected} onSelect={setSelected} empty="Завершённых нет." />
      </Stack>
      <div style={{ flex: '1 1 28rem' }}>{ticket ?
        <Card key={ticket.id} api={api} scope={scope} ticket={ticket} messages={conversation(all, ticket.id)} run={run} /> :
        <Alert color="gray">Выберите обращение, чтобы увидеть переписку и ответить.</Alert>}</div>
    </Group>}
  </Stack>;
}
