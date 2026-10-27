import { formatTime, untilText } from '../time.ts';
import { Alert, Badge, Button, Checkbox, Group, Paper, Select, Stack, Text, TextInput, Title } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import type { Capability, ConsequencePlan, OperationProjection, Permission, Receipt, RunSession, RunSessionPage, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { PlanFlow, type PlanSpec } from '../plan/PlanFlow.tsx';
import {
  accessBlocked, accessExecute, accessPreview, DAY,
  afterControl, backupIdPattern, cancelCommand, canCancel, controlAvailability, controlCommand, controlLabels, degradation, effectLabels,
  healthLabels, indicators, levelLabels, levels, needsReconciliation, operationLabels, orderOperations, overdue, permissionCommand,
  permissionState, permissionWarning, recoveryExecute, recoveryPreview, recoveryProblem, restoreBlocked, runNotice, verificationLabels,
  type Control, type ControlKind,
} from './model.ts';

const when = (value: string) => formatTime(value, 'short');
const tone = { ok: 'teal', wait: 'blue', warn: 'orange', off: 'gray' } as const;

function useRead<T>(scope: Scope, resource: string, read: (signal: AbortSignal) => Promise<T>) {
  return useQuery({ queryKey: projectionKey(resource, scope), queryFn: ({ signal }) => read(signal) });
}

function Controls({ api, scope, run }: { api: PanelApi; scope: Scope; run: RunSession | undefined }) {
  const cache = useQueryClient();
  const [state, setState] = useState<Control>({ phase: 'idle' });
  const [autonomy, setAutonomy] = useState<'observation' | 'autonomous'>('observation');
  async function submit(command: ReturnType<typeof controlCommand>) {
    setState({ phase: 'sending', command });
    try {
      setState(afterControl(command, { receipt: await api.command(command) as Receipt }));
    } catch (error) {
      setState(afterControl(command, { code: error instanceof ApiFailure ? error.code : 'NETWORK' }));
    } finally { void cache.invalidateQueries({ queryKey: projectionKey('sessions', scope) }); }
  }
  const busy = state.phase === 'sending' || state.phase === 'unknown';
  return <Stack gap="xs" mt="sm">
    <Group gap="xs" align="flex-end">
      {(['agent.start', 'agent.pause', 'agent.resume', 'session.end'] as ControlKind[]).map((kind) => {
        const open = controlAvailability(run, kind);
        return <Button key={kind} variant="light" size="compact-md" disabled={!open.enabled || busy} title={open.reason}
          onClick={() => run && void submit(controlCommand(run, kind, kind === 'agent.start' ? autonomy : null, crypto.randomUUID()))}>{controlLabels[kind]}</Button>;
      })}
      <Select label="Режим при запуске" size="xs" w={190} allowDeselect={false} value={autonomy} onChange={(v) => v && setAutonomy(v as typeof autonomy)}
        data={[{ value: 'observation', label: 'Наблюдение' }, { value: 'autonomous', label: 'Автономная работа' }]} />
    </Group>
    <Text size="xs" c="dimmed">Управление не заменяет STOP: он остаётся отдельной кнопкой вверху и не ждёт этих операций.</Text>
    {state.phase === 'accepted' && <Alert color="blue" role="status">Команда принята ядром. Состояние обновится после подтверждения владельца запуска.</Alert>}
    {state.phase === 'refused' && <Alert color="orange" role="alert">{failureMessage(new ApiFailure(state.code))} Ничего не изменено.</Alert>}
    {state.phase === 'unknown' && <Alert color="orange" role="alert" title="Результат неизвестен">
      Команда могла быть выполнена. Проверка повторяет ту же команду; второго запуска не будет.
      <Button mt="xs" size="compact-sm" variant="light" onClick={() => void submit(state.command)}>Проверить исход</Button></Alert>}
  </Stack>;
}

function Operations({ api, scope, run }: { api: PanelApi; scope: Scope; run: RunSession | undefined }) {
  const cache = useQueryClient();
  const rows = useRead(scope, 'operations', (signal) => api.operations(scope, signal));
  const [now] = useState(() => Date.now());
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function cancel(op: OperationProjection) {
    if (!run) return;
    setBusy(true); setNote(null);
    try {
      const receipt = await api.command(cancelCommand(run.scope, run.session_id, op, crypto.randomUUID())) as Receipt;
      // A receipt is a request accepted, not a finished cancellation: the owner's status decides.
      setNote(receipt.status === 'completed' ? 'Запрос на отмену принят. Фактический статус смотрите в списке.' : 'Запрос на отмену принят и ожидает владельца.');
    } catch (error) { setNote(failureMessage(error)); }
    finally { setBusy(false); void cache.invalidateQueries({ queryKey: projectionKey('operations', scope) }); }
  }
  return <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="sm">Задания и операции</Title>
    {rows.isPending ? <Text size="sm">Получаем задания…</Text> : rows.isError ? <Alert color="orange">{failureMessage(rows.error)}</Alert> :
      !rows.data.items.length ? <Text size="sm" c="dimmed">Заданий нет.</Text> :
        <Stack gap="xs" component="ul" className="plain-list" aria-label="Список заданий">{orderOperations(rows.data.items).map((op) => <li key={op.id}>
          <Paper withBorder p="xs"><Group justify="space-between" wrap="nowrap">
            <div><Text size="sm" fw={600}>{op.operation_id}</Text>
              <Text size="xs" c="dimmed">До {when(op.deadline)}{overdue(op, now) ? ' · просрочено' : ''}{op.waiting_reason ? ' · ' + op.waiting_reason : ''}</Text></div>
            <Group gap="xs"><Badge variant="light" color={op.status === 'OUTCOME_UNKNOWN' ? 'orange' : op.status === 'SUCCEEDED' ? 'teal' : 'gray'}>{operationLabels[op.status]}</Badge>
              {canCancel(op) && <Button size="compact-xs" variant="subtle" color="gray" disabled={busy || !run} onClick={() => void cancel(op)}>Отменить</Button>}</Group></Group>
            {needsReconciliation(op) && <Text size="xs" c="orange.8" mt={4}>Требуется сверка с владельцем: не повторяйте воздействие вслепую.</Text>}
          </Paper></li>)}</Stack>}
    {note && <Alert color="blue" mt="sm" role="status">{note}</Alert>}
    <Text size="xs" c="dimmed" mt="xs">Завершённое воздействие не становится «отменённым»: показывается фактический статус от владельца.</Text>
  </Paper>;
}

function Rights({ api, scope, run }: { api: PanelApi; scope: Scope; run: RunSession | undefined }) {
  const cache = useQueryClient();
  const permissions = useRead(scope, 'permissions', (signal) => api.permissions(scope, signal));
  const capabilities = useRead(scope, 'capabilities', (signal) => api.capabilities(scope, signal));
  const [now] = useState(() => Date.now());
  const [draft, setDraft] = useState<{ permission: Permission; level: Permission['level'] } | null>(null);
  const [ticked, setTicked] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const byId = new Map((capabilities.data?.items ?? []).map((item) => [item.id, item] as [string, Capability]));
  const warning = draft ? permissionWarning(draft.permission, draft.level, byId.get(draft.permission.capability_id)) : null;
  async function apply() {
    if (!draft || !run) return;
    setProblem(null); setSaved(false);
    try {
      await api.command(permissionCommand(run.scope, run.session_id, draft.permission, draft.level, crypto.randomUUID()));
      setSaved(true); setDraft(null); setTicked(false);
    } catch (error) {
      setProblem(error instanceof ApiFailure && error.code === 'VERSION_CONFLICT' ?
        'Право уже изменено. Обновите список и пересмотрите правку; чужое изменение не затирается.' : failureMessage(error));
    } finally { void cache.invalidateQueries({ queryKey: projectionKey('permissions', scope) }); }
  }
  return <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="sm">Права</Title>
    {permissions.isPending ? <Text size="sm">Получаем права…</Text> : permissions.isError ? <Alert color="orange">{failureMessage(permissions.error)}</Alert> :
      !permissions.data.items.length ? <Text size="sm" c="dimmed">Прав не выдано: действия закрыты.</Text> :
        <Stack gap="xs" component="ul" className="plain-list" aria-label="Список прав">{permissions.data.items.map((permission) => {
          const state = permissionState(permission, now);
          return <li key={permission.id}><Paper withBorder p="xs"><Group justify="space-between" wrap="nowrap">
            <div><Text size="sm" fw={600}>{permission.capability_id} · {permission.operation_type}</Text>
              <Text size="xs" c="dimmed">Субъект {permission.principal_id} · {untilText(permission.expires_at)}</Text></div>
            <Group gap="xs"><Badge variant="light" color={state === 'active' ? 'teal' : 'gray'}>{state === 'revoked' ? 'Отозвано' : state === 'expired' ? 'Срок истёк' : levelLabels[permission.level]}</Badge>
              {state === 'active' && <Button size="compact-xs" variant="subtle" onClick={() => { setDraft({ permission, level: permission.level }); setTicked(false); setSaved(false); }}>Изменить</Button>}</Group></Group></Paper></li>;
        })}</Stack>}
    {draft && <Paper withBorder p="sm" mt="sm" aria-label="Изменение права">
      <Select label="Новый уровень" allowDeselect={false} value={draft.level} onChange={(v) => v && setDraft({ ...draft, level: v as Permission['level'] })}
        data={levels.map((value) => ({ value, label: levelLabels[value] }))} />
      {warning && <Alert color="orange" mt="xs" role="alert">{warning}</Alert>}
      {warning && <Checkbox mt="xs" label="Подтверждаю расширение прав" checked={ticked} onChange={(event) => setTicked(event.currentTarget.checked)} />}
      <Group mt="xs"><Button disabled={draft.level === draft.permission.level || (!!warning && !ticked) || !run} onClick={() => void apply()}>Применить</Button>
        <Button variant="subtle" color="gray" onClick={() => setDraft(null)}>Отмена</Button></Group>
    </Paper>}
    {problem && <Alert color="orange" mt="sm" role="alert">{problem}</Alert>}
    {saved && <Alert color="teal" mt="sm" role="status">Изменение права принято.</Alert>}
    <Text size="xs" c="dimmed" mt="xs">Кнопка решения не является разрешением публичной публикации. Расширение прав всегда требует подтверждения.</Text>
  </Paper>;
}

function Services({ api, scope }: { api: PanelApi; scope: Scope }) {
  const capabilities = useRead(scope, 'capabilities', (signal) => api.capabilities(scope, signal));
  const connections = useRead(scope, 'connections', (signal) => api.connections(scope, signal));
  const reasons = degradation(capabilities.data?.items ?? [], connections.data?.items ?? []);
  return <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="sm">Подключения и возможности</Title>
    {(capabilities.isError || connections.isError) && <Alert color="orange" mb="sm">{failureMessage(capabilities.error ?? connections.error)} Состояние служб может быть неполным.</Alert>}
    {!!reasons.length && <Alert color="orange" mb="sm" title="Причины деградации"><ul>{reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></Alert>}
    <Title order={3} size="h5">Подключения</Title>
    {!connections.data?.items.length ? <Text size="sm" c="dimmed">Подключений нет или они недоступны.</Text> :
      connections.data.items.map((item) => <Group key={item.id} justify="space-between"><Text size="sm">{item.name} · {item.adapter_id}</Text>
        <Badge variant="light" color={item.configured && item.verification === 'verified' ? 'teal' : 'orange'}>{item.configured ? verificationLabels[item.verification] : 'Не настроено'}</Badge></Group>)}
    <Text size="xs" c="dimmed">Ключи и адреса хранятся только в локальной конфигурации; здесь видны лишь их состояния.</Text>
    <Title order={3} size="h5" mt="sm">Возможности</Title>
    {!capabilities.data?.items.length ? <Text size="sm" c="dimmed">Возможностей не зарегистрировано.</Text> :
      capabilities.data.items.map((item) => <Group key={item.id} justify="space-between"><Text size="sm">{item.id} · {effectLabels[item.effect]}</Text>
        <Badge variant="light" color={item.health === 'ready' ? 'teal' : item.health === 'degraded' ? 'orange' : 'red'}>{healthLabels[item.health]}</Badge></Group>)}
  </Paper>;
}

// OC-05: one initial access for the configured set; a capability added later is not covered.
// The owner answers with its own receipt (no background operation), so the card confirms in place.
function InitialAccess({ api, scope }: { api: PanelApi; scope: Scope }) {
  const cache = useQueryClient();
  const status = useRead(scope, 'workplace-access', (signal) => api.workplaceAccess(scope, signal));
  const ceiling = Math.floor((status.data?.max_duration_seconds ?? 0) / DAY);
  const [days, setDays] = useState<string | null>(null);
  const [constraints, setConstraints] = useState('');
  const [plan, setPlan] = useState<ConsequencePlan | null>(null);
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const executing = useRef<string | null>(null);
  const standing = status.data?.standing ?? false;
  const picked = days ?? (standing ? 'standing' : String(ceiling));
  const chosen = picked === 'standing' ? null : Number(picked);
  const blocked = accessBlocked(status.data, chosen);
  const reload = () => Promise.all(['permissions', 'tickets', 'workplace-access']
    .map((resource) => cache.invalidateQueries({ queryKey: projectionKey(resource, scope) })));
  async function preview() {
    setBusy(true); setProblem(null); setDone(false); setUnderstood(false); executing.current = null;
    try {
      const receipt = await api.command(accessPreview(scope, chosen, constraints.trim(), crypto.randomUUID()));
      if (!receipt.resource_id) throw new ApiFailure('NETWORK');
      setPlan(await api.plan(scope, receipt.resource_id));
    } catch (error) { setPlan(null); setProblem(failureMessage(error)); } finally { setBusy(false); }
  }
  async function confirm(current: ConsequencePlan) {
    if (Date.parse(current.expires_at) <= Date.now()) { setPlan(null); setProblem('Срок плана истёк. Подготовьте его заново.'); return; }
    setBusy(true); setProblem(null);
    executing.current ??= crypto.randomUUID(); // a retry repeats the same request: the owner replays its receipt
    try {
      await api.command(accessExecute(scope, current, executing.current));
      setPlan(null); setDone(true); executing.current = null;
    } catch (error) {
      if (error instanceof ApiFailure && error.code === 'VERSION_CONFLICT') {
        setPlan(null); executing.current = null; setProblem(failureMessage(error) + ' Набор изменился: подготовьте план заново.');
      } else setProblem(failureMessage(error) + ' Повтор использует тот же запрос.');
    } finally { setBusy(false); void reload(); }
  }
  return <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="sm">Начальный доступ</Title>
    {status.isError ? <Text size="sm" c="orange.8" role="status">Сведения о наборе недоступны: {failureMessage(status.error)}</Text> : !status.data ? <Text c="dimmed">Загружаем набор…</Text> :
      !status.data.configured ? <Text c="dimmed">Набор начального доступа на этом хосте не настроен.</Text> : <Stack gap="xs">
        <Text size="sm" c="dimmed">Одно решение на весь набор. Возможность, добавленная позже, этот допуск не получает. Отозвать можно каждое право отдельно.</Text>
        {status.data.members.map((member) => <Group key={member.capability_id} gap="xs" role="listitem" aria-label={member.capability_id}>
          <Badge variant="light" color={member.available ? 'teal' : 'gray'}>{member.available ? 'Разрешено' : 'Не выдано'}</Badge>
          <Text size="sm">{member.summary}</Text>
          {member.expires_at && <Text size="xs" c="dimmed">{untilText(member.expires_at)}</Text>}</Group>)}
        <Group gap="sm" align="end">
          <Select label="Срок" w={160} value={picked} allowDeselect={false} disabled={!!plan}
            onChange={(value) => { setDays(value); setDone(false); }}
            data={[...(standing ? [{ value: 'standing', label: 'До отзыва' }] : []), ...Array.from({ length: ceiling }, (_, index) => ({ value: String(index + 1), label: `${index + 1} дн.` }))]} />
          <TextInput label="Условия (необязательно)" maxLength={500} value={constraints} style={{ flex: 1 }} disabled={!!plan}
            onChange={(event) => setConstraints(event.currentTarget.value)} /></Group>
        {!plan && <Group><Button variant="light" loading={busy} disabled={!!blocked} onClick={() => void preview()}>Подготовить начальный доступ</Button></Group>}
        {!plan && blocked && <Text size="xs" c="orange.8" role="status">{blocked}</Text>}
        {plan && <Paper withBorder p="sm" aria-label="План последствий">
          <Text fw={600}>План: начальный доступ</Text>
          <Text className="plain-text" size="sm" mt={4}>{plan.consequences}</Text>
          <Text size="sm" mt={4}>Действует до {when(plan.expires_at)}</Text>
          <Checkbox mt="xs" label="Я понимаю: агент получит все перечисленные возможности на выбранный срок" checked={understood}
            onChange={(event) => setUnderstood(event.currentTarget.checked)} />
          <Group mt="xs"><Button loading={busy} disabled={!understood} onClick={() => void confirm(plan)}>Выдать доступ</Button>
            <Button variant="subtle" color="gray" onClick={() => { setPlan(null); executing.current = null; }}>Отмена</Button></Group></Paper>}
        {done && <Alert color="teal" role="status">Права выданы; прежние запросы этих возможностей закрыты.</Alert>}
        {problem && <Alert color="orange" role="alert">{problem}</Alert>}
      </Stack>}
  </Paper>;
}

function Recovery({ api, scope, run }: { api: PanelApi; scope: Scope; run: RunSession | undefined }) {
  const workspace = useRead(scope, 'workspaces', (signal) => api.workspaces(scope, signal));
  const [backupId, setBackupId] = useState('');
  const version = workspace.data?.items[0]?.version ?? 1;
  const target = run?.session_id ?? '';
  const backup: PlanSpec | null = run ? {
    label: 'Резервная копия', previewLabel: 'Подготовить резервную копию', executeLabel: 'Создать копию',
    note: 'Копия — локальный артефакт на этом компьютере, не публикация. Секреты из .env в неё не входят.',
    preview: (requestId) => recoveryPreview(run.scope, target, 'backup', '', version, requestId),
    execute: (plan, requestId) => recoveryExecute(run.scope, target, 'backup', plan, requestId),
    problem: (plan, now) => recoveryProblem(plan, 'backup', '', now), related: (plan) => plan.dependencies.length, refresh: ['workspaces'],
  } : null;
  const restore: PlanSpec | null = run ? {
    label: 'Восстановление', previewLabel: 'Подготовить восстановление', executeLabel: 'Восстановить', danger: true,
    blocked: restoreBlocked(run, backupId), typedConfirm: backupId, confirmText: 'Агент остановлен, я понимаю: текущее состояние будет заменено содержимым копии',
    note: 'Перед заменой делается страховочная копия. Позднейшие удаления применяются заново, события очереди замораживаются.',
    doneNote: 'После восстановления агент остаётся остановленным и требует явного запуска. Сначала проверьте состояние и незавершённые операции.',
    preview: (requestId) => recoveryPreview(run.scope, target, 'restore', backupId, version, requestId),
    execute: (plan, requestId) => recoveryExecute(run.scope, target, 'restore', plan, requestId),
    problem: (plan, now) => recoveryProblem(plan, 'restore', backupId, now), related: (plan) => plan.dependencies.length, refresh: ['workspaces', 'sessions', 'operations'],
  } : null;
  return <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="sm">Резервные копии и восстановление</Title>
    {!run ? <Alert color="orange">Нет подтверждённого запуска: операции с копиями недоступны.</Alert> : <Stack gap="md">
      <div><Text fw={600}>Создать копию</Text><PlanFlow key="backup" api={api} scope={scope} spec={backup!} /></div>
      <div><Text fw={600}>Восстановить из копии</Text>
        <TextInput label="Идентификатор копии" value={backupId} error={backupId && !backupIdPattern.test(backupId) ? 'Недопустимые символы' : undefined}
          onChange={(event) => setBackupId(event.currentTarget.value.trim())} maxLength={128} />
        <PlanFlow key={'restore:' + backupId} api={api} scope={scope} spec={restore!} /></div>
    </Stack>}
  </Paper>;
}

export function WorkspaceScreen({ api, scope }: { api: PanelApi; scope: Scope }) {
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = (runs.data as RunSessionPage | undefined)?.items[0];
  const notice = run ? runNotice(run) : null;
  return <Stack gap="lg">
    <div><Text className="eyebrow">06 / Рабочее пространство</Text><Title order={1}>Рабочее пространство</Title></div>
    {scope.memory_mode === 'test' && <Alert color="violet" title="Тестовая память">Эти данные изолированы от рабочей биографии. Просмотр не запускает агента.</Alert>}
    <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="sm">Запуск в этой области</Title>
      {run ? <Stack gap="xs">
        <Group gap="xs">{indicators(run).map((item) => <Badge key={item.label} variant="light" color={tone[item.tone]} size="lg">{item.label}: {item.value}</Badge>)}</Group>
        {notice && <Alert color={run.state === 'FAULTED' ? 'orange' : 'gray'} role="status">{notice}</Alert>}
        {!!run.unconfirmed_resource_ids?.length && <Alert color="orange" title="Остановка ещё не подтверждена">
          Неподтверждённые ресурсы: {run.unconfirmed_resource_ids.join(', ')}</Alert>}
        <details><summary>Технические сведения</summary><Text size="xs" component="pre">
          {JSON.stringify({ agent_id: run.agent_id, run_id: run.run_id, version: run.version, control_epoch: run.control_epoch, updated_at_utc: run.updated_at }, null, 2)}</Text></details>
      </Stack> : <Text c="dimmed">Сведения о запуске в этой области недоступны.</Text>}
      <Controls api={api} scope={scope} run={run} />
    </Paper>
    <Operations api={api} scope={scope} run={run} />
    <Services api={api} scope={scope} />
    <InitialAccess api={api} scope={scope} />
    <Rights api={api} scope={scope} run={run} />
    <Recovery api={api} scope={scope} run={run} />
  </Stack>;
}
