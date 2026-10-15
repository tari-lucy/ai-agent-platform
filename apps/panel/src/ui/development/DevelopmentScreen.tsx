import { formatTime } from '../time.ts';
import { Alert, Badge, Button, Group, Paper, Select, Stack, Tabs, Text, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { RulesData, Opportunity, ProfilePage, Receipt, RunSession, RunSessionPage, Scope, StateChange, StoryArc } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { samePartition, projectionKey } from '../../shared/scope.ts';
import { PlanFlow, type PlanSpec } from '../plan/PlanFlow.tsx';
import {
  arcLabels, arcStateNote, attention, awaitingOperator, cancelOpportunityCommand, canCancelOpportunity, canCorrect, canPauseArc, canRollback,
  changeLabels, decisionCommand, evidenceLost, evidenceSummary, gmIntensityCommand, gmPauseCommand, intensities, interpretationCommand, opportunitiesOf,
  opportunityLabels, patchOf, pathItems, pauseArcCommand, rollbackExecute, rollbackPreview, rollbackProblem, validInterpretation, versions, type Decision, type Intensity,
} from './model.ts';

const when = (value: string) => formatTime(value, 'short');

function usePaged<T extends { items: unknown[]; complete: boolean; next_cursor: string | null }>(
  scope: Scope, resource: string, read: (cursor: string | undefined, signal: AbortSignal) => Promise<T>,
) {
  return useInfiniteQuery({
    queryKey: [...projectionKey(resource, scope), 'development'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => read(pageParam, signal),
    getNextPageParam: (last) => (last.complete || !last.next_cursor ? undefined : last.next_cursor),
  });
}

// One command at a time with the identical command re-sent when the outcome is unknown.
function useAction(api: PanelApi, scope: Scope, refresh: string[]) {
  const cache = useQueryClient();
  const [state, setState] = useState<{ phase: 'idle' | 'sending' | 'done' } | { phase: 'unknown'; command: Parameters<PanelApi['command']>[0] } | { phase: 'refused'; code: string }>({ phase: 'idle' });
  async function run(command: Parameters<PanelApi['command']>[0]) {
    setState({ phase: 'sending' });
    try { await api.command(command) as Receipt; setState({ phase: 'done' }); }
    catch (error) {
      const code = error instanceof ApiFailure ? error.code : 'NETWORK';
      setState(['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(code) ? { phase: 'unknown', command } : { phase: 'refused', code });
    } finally { for (const resource of refresh) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) }); }
  }
  const view = <>
    {state.phase === 'done' && <Alert color="teal" mt="xs" role="status">Команда принята ядром. Результат подтвердит владелец.</Alert>}
    {state.phase === 'refused' && <Alert color="orange" mt="xs" role="alert">{state.code === 'VERSION_CONFLICT' ?
      'Запись уже изменена. Обновите её и пересмотрите решение; чужое изменение не затирается.' : failureMessage(new ApiFailure(state.code))} Операция не подтверждена. Проверьте текущее состояние.</Alert>}
    {state.phase === 'unknown' && <Alert color="orange" mt="xs" role="alert" title="Результат неизвестен">
      Команда могла быть выполнена. Проверка повторяет ту же команду; второго действия не будет.
      <Button mt="xs" size="compact-sm" variant="light" onClick={() => void run(state.command)}>Проверить исход</Button></Alert>}
  </>;
  return { run, busy: state.phase === 'sending' || state.phase === 'unknown', view };
}

function ChangeCard({ api, scope, run, change }: { api: PanelApi; scope: Scope; run: RunSession | undefined; change: StateChange }) {
  const patch = patchOf(change);
  const action = useAction(api, scope, ['changes', 'character-states']);
  const [reason, setReason] = useState('');
  const [text, setText] = useState(change.interpretation);
  const [problem, setProblem] = useState<string | null>(null);
  const [mode, setMode] = useState<'interpret' | 'rollback' | null>(null);
  function decide(decision: Decision) {
    if (!run) return;
    void action.run(decisionCommand(run, change, decision, reason, crypto.randomUUID()));
  }
  function interpret() {
    const invalid = validInterpretation(change, text, reason);
    if (invalid || !run) { setProblem(invalid ?? 'Нет подтверждённого запуска.'); return; }
    setProblem(null); void action.run(interpretationCommand(run, change, text, reason, crypto.randomUUID()));
  }
  const spec: PlanSpec | null = run ? {
    ownerOperation: true, label: 'Откат', previewLabel: 'Подготовить откат', executeLabel: 'Откатить изменение', danger: true,
    confirmText: 'Я понимаю: создастся новая компенсирующая версия; опыт и уже случившееся не стираются',
    note: 'Откат пересматривает зависимые предложения и текущий контекст, но не отменяет событие мира и не забирает отправленную реплику.',
    preview: (requestId) => rollbackPreview(run, change, requestId), execute: (plan, requestId) => rollbackExecute(run, change, plan, requestId),
    problem: (plan, now) => rollbackProblem(plan, change, now), related: (plan) => plan.dependencies.filter((dep) => dep.resource_id !== change.id).length,
    refresh: ['changes', 'character-states', 'opportunities'],
  } : null;
  return <Paper withBorder p="md" radius="lg" aria-label="Карточка изменения">
    <Group gap="xs" mb="xs"><Badge variant="light" color={change.status === 'APPLIED' ? 'teal' : awaitingOperator(change) ? 'orange' : 'gray'}>{changeLabels[change.status]}</Badge>
      {patch?.rollback && <Badge variant="outline">Компенсирующая версия</Badge>}</Group>
    {patch ? <><Text fw={600}>{patch.property}</Text><Text size="sm">Было: <b>{patch.before}</b></Text><Text size="sm">{change.status === 'APPLIED' ? 'Стало' : 'Предложено'}: <b>{patch.after}</b></Text></> :
      <Text size="sm" c="orange.8">Формат правки не распознан: значения не показаны.</Text>}
    <Text size="sm" mt="xs">Интерпретация (это не факт): <span className="plain-text">{change.interpretation}</span></Text>
    <Text size="sm">Неопределённость: {change.uncertainty}</Text>
    <Text size="sm" c="dimmed">Ожидаемое проявление (ещё не подтверждено): <span className="plain-text">{change.expected_manifestation}</span></Text>
    <Text size="sm">Основания: {evidenceSummary(change.evidence)}</Text>
    {evidenceLost(change) && <Alert color="orange" mt="xs">Все основания недоступны: изменение нельзя считать подтверждённым опытом.</Alert>}
    {change.operator_reason && <Text size="sm">Основание оператора: {change.operator_reason}</Text>}
    <Text size="xs" c="dimmed">{versions(change)} · автор предложения {change.owner} · {when(change.updated_at)}{change.operator_decision_id ? ` · решение ${change.operator_decision_id}` : ''}</Text>
    <Text size="xs" c="dimmed">Наблюдённые позднее проявления: связанных записей нет.</Text>
    {awaitingOperator(change) && <Stack gap="xs" mt="sm"><Textarea label="Основание решения" autosize minRows={1} maxLength={1000} value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
      <Group><Button disabled={!run || action.busy || !reason.trim()} onClick={() => decide('accept')}>Принять</Button><Button color="red" variant="light" disabled={!run || action.busy || !reason.trim()} onClick={() => decide('reject')}>Отклонить</Button></Group>
      <Text size="xs" c="dimmed">Истечение ожидания не означает одобрения: изменение останется непринятым.</Text></Stack>}
    <Group mt="sm" gap="xs">
      {canCorrect(change) && <Button size="compact-sm" variant="light" onClick={() => setMode(mode === 'interpret' ? null : 'interpret')}>Исправить интерпретацию</Button>}
      {canRollback(change) && <Button size="compact-sm" color="red" variant="light" onClick={() => setMode(mode === 'rollback' ? null : 'rollback')}>Откатить</Button>}</Group>
    {mode === 'interpret' && <Stack gap="xs" mt="xs"><Textarea label="Исправленная интерпретация" autosize minRows={2} maxLength={1500} value={text} onChange={(event) => setText(event.currentTarget.value)} />
      <Textarea label="Основание исправления" autosize minRows={1} maxLength={1000} value={reason} onChange={(event) => setReason(event.currentTarget.value)} />
      <Group justify="flex-end"><Button disabled={action.busy} onClick={interpret}>Сохранить исправление</Button></Group>
      {problem && <Alert color="orange" role="alert">{problem}</Alert>}</Stack>}
    {mode === 'rollback' && spec && <PlanFlow key={change.id} api={api} scope={scope} spec={spec} />}
    {action.view}
  </Paper>;
}

function ArcCard({ api, scope, run, arc, opps }: { api: PanelApi; scope: Scope; run: RunSession | undefined; arc: StoryArc; opps: Opportunity[] }) {
  const action = useAction(api, scope, ['arcs', 'opportunities']);
  const groups = opportunitiesOf(arc, opps);
  return <Paper withBorder p="md" radius="lg" aria-label="Карточка линии">
    <Group gap="xs" mb="xs"><Badge variant="light" color={arc.status === 'open' ? 'teal' : 'gray'}>{arcLabels[arc.status]}</Badge>
      {groups.blocked.length > 0 && <Badge color="orange" variant="light">Есть блокировки</Badge>}</Group>
    <Text className="plain-text" fw={600}>{arc.direction}</Text>
    <Text size="sm" c="dimmed">Направление, а не предсказанный финал: у линии может не быть заранее заданного конца.</Text>
    <Text size="sm" mt="xs">Границы: <span className="plain-text">{arc.boundaries}</span></Text>
    <Text size="sm">Основания: {evidenceSummary(arc.grounds)}{arc.rules_version != null ? ` · правила v${arc.rules_version}` : ''}</Text>
    {arc.cooldown_until && <Text size="sm">Пауза между испытаниями до {when(arc.cooldown_until)}</Text>}
    <Alert color={groups.blocked.length ? 'orange' : 'gray'} mt="xs">{arcStateNote(arc, opps)}</Alert>
    <Title order={4} size="h5" mt="sm">Пережито</Title>
    {!groups.lived.length ? <Text size="sm" c="dimmed">Пока ничего не пережито.</Text> : groups.lived.map((opp) => <Paper key={opp.id} withBorder p="xs" mt={4}>
      <Text size="sm" className="plain-text">Выбор: {opp.choice ?? 'не записан'}</Text><Text size="sm" className="plain-text">Исход: {opp.actual_outcome ?? 'не записан'}</Text></Paper>)}
    <Title order={4} size="h5" mt="sm">Возможные продолжения (ещё не произошли)</Title>
    {!groups.possible.length ? <Text size="sm" c="dimmed">Предложенных испытаний нет.</Text> : groups.possible.map((opp) => <Paper key={opp.id} withBorder p="xs" mt={4}>
      <Group justify="space-between" wrap="nowrap"><div><Badge variant="outline" size="sm">{opportunityLabels[opp.status]}</Badge>
        <Text size="sm" className="plain-text">{opp.applicability}</Text><Text size="xs" c="dimmed">до {when(opp.expires_at)}</Text></div>
        {canCancelOpportunity(opp) && run && <Button size="compact-xs" variant="subtle" color="gray" disabled={action.busy}
          onClick={() => void action.run(cancelOpportunityCommand(run, opp, crypto.randomUUID()))}>Отменить</Button>}</Group></Paper>)}
    {groups.blocked.length > 0 && <><Title order={4} size="h5" mt="sm">Заблокировано границами</Title>
      {groups.blocked.map((opp) => <Text key={opp.id} size="sm">{opp.reason_code ?? 'причина не указана'}</Text>)}</>}
    {canPauseArc(arc) && run && <Group mt="sm"><Button size="compact-sm" color="orange" variant="light" disabled={action.busy}
      onClick={() => void action.run(pauseArcCommand(run, arc, crypto.randomUUID()))}>Приостановить линию</Button></Group>}
    {action.view}
  </Paper>;
}

export function DevelopmentScreen({ api, scope, run: activeRun }: { api: PanelApi; scope: Scope; run?: RunSession }) {
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = activeRun && samePartition(activeRun.scope, scope) ? activeRun : (runs.data as RunSessionPage | undefined)?.items[0];
  const settings = useQuery({ queryKey: projectionKey('gm-settings', scope), queryFn: ({ signal }) => api.gmSettings(scope, signal) });
  const gmSettings = settings.data?.items.find((r) => r.id === 'gm-runtime');
  const arcs = usePaged(scope, 'arcs', (cursor, signal) => api.arcs(scope, signal, cursor));
  const opps = usePaged(scope, 'opportunities', (cursor, signal) => api.opportunities(scope, signal, cursor));
  const changes = usePaged(scope, 'changes', (cursor, signal) => api.changes(scope, signal, cursor));
  const states = useQuery({ queryKey: projectionKey('character-states', scope), queryFn: ({ signal }) => api.characterStates(scope, signal) });
  const profiles = useQuery({ queryKey: projectionKey('personas', scope), queryFn: ({ signal }) => api.profiles(scope, signal) });
  const [selectedArc, setSelectedArc] = useState<string | null>(null);
  const [selectedChange, setSelectedChange] = useState<string | null>(null);
  const [intensity, setIntensity] = useState<Intensity>('normal');
  const gm = useAction(api, scope, ['sessions', 'gm-settings']);
  const arcRows = arcs.data?.pages.flatMap((p) => p.items) ?? [];
  const oppRows = opps.data?.pages.flatMap((p) => p.items) ?? [];
  const changeRows = [...(changes.data?.pages.flatMap((p) => p.items) ?? [])].sort((a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || a.id.localeCompare(b.id));
  const queue = attention(changeRows, oppRows);
  const path = pathItems(changeRows, oppRows);
  const arc = arcRows.find((item) => item.id === selectedArc) ?? null;
  const change = changeRows.find((item) => item.id === selectedChange) ?? null;
  const failed = [arcs, opps, changes, states, profiles].filter((query) => query.isError);
  const incomplete = [arcs, opps, changes].some((query) => query.hasNextPage);
  const profile = (profiles.data as ProfilePage | undefined)?.items[0];
  return <Stack gap="lg">
    <div><Text className="eyebrow">04 / Сюжет и развитие</Text><Title order={1}>Сюжет и развитие</Title></div>
    {scope.memory_mode === 'test' && <Alert color="violet" title="Тестовая память">Линии и изменения изолированы от рабочей биографии.</Alert>}
    {!!failed.length && <Alert color="orange" title="Часть данных недоступна">{failureMessage(failed[0].error)} Картина может быть неполной.</Alert>}
    {incomplete && <Alert color="orange">Загружена не вся история.
      <Button ml="sm" size="compact-sm" variant="light" onClick={() => [arcs, opps, changes].forEach((q) => q.hasNextPage && void q.fetchNextPage())}>Загрузить ещё</Button></Alert>}
    <Tabs defaultValue="overview"><Tabs.List><Tabs.Tab value="overview">Обзор</Tabs.Tab><Tabs.Tab value="arcs">Линии</Tabs.Tab>
      <Tabs.Tab value="changes">Изменения</Tabs.Tab><Tabs.Tab value="bounds">Границы</Tabs.Tab></Tabs.List>
      <Tabs.Panel value="overview" pt="md"><Stack gap="md">
        <Paper withBorder p="md" radius="lg" aria-label="Гейммастер"><Title order={3} size="h5" mb="xs">Гейммастер</Title>
          <Group gap="xs"><Badge color={run?.gm_paused ? 'gray' : 'teal'} variant="light">{!run ? 'Состояние неизвестно' : run.gm_paused ? 'На паузе' : 'Пауза снята по данным ядра'}</Badge></Group>
          <Group mt="xs" align="flex-end">
            <Button variant="light" disabled={!run || !gmSettings || gm.busy} onClick={() => run && void gm.run(gmPauseCommand(run, gmSettings!, !run.gm_paused, crypto.randomUUID()))}>{run?.gm_paused ? 'Снять паузу' : 'Приостановить гейммастера'}</Button>
            <Select label="Интенсивность" size="xs" w={170} allowDeselect={false} value={intensity} data={Object.entries(intensities).map(([value, label]) => ({ value, label }))} onChange={(v) => v && setIntensity(v as Intensity)} />
            <Button variant="light" disabled={!run || !gmSettings || gm.busy} onClick={() => run && void gm.run(gmIntensityCommand(run, gmSettings!, intensity, crypto.randomUUID()))}>Применить интенсивность</Button></Group>
          <Text size="xs" c="dimmed" mt="xs">Текущая интенсивность: {gmSettings?.intensity ?? 'недоступна'}. Гейммастер внутренний: у него нет собственного голоса, он не решает за людей и не пишет биографию.</Text>
          {gm.view}{settings.isError && <Alert color="orange">{failureMessage(settings.error)}</Alert>}</Paper>
        <Paper withBorder p="md" radius="lg" aria-label="Требует внимания"><Title order={3} size="h5" mb="xs">Требует внимания</Title>
          {!queue.length ? <Text size="sm" c="dimmed">Ничего не ждёт решения.</Text> : <ul>{queue.map((item) => <li key={item.key}><Text size="sm">{item.text}</Text></li>)}</ul>}</Paper>
        <Paper withBorder p="md" radius="lg" aria-label="Путь развития"><Title order={3} size="h5" mb="xs">Путь развития</Title>
          {!path.length ? <Text size="sm" c="dimmed">Пережитых событий и применённых изменений пока нет.</Text> : <Stack gap="xs" component="ol" className="plain-list">{path.map((item) => <li key={item.key}>
            <Paper withBorder p="xs"><Group gap="xs"><Badge size="sm" variant="light" color={item.kind === 'lived' ? 'blue' : item.kind === 'rollback' ? 'orange' : 'teal'}>
              {item.kind === 'lived' ? 'Пережито' : item.kind === 'rollback' ? 'Откат' : 'Изменение личности'}</Badge><Text size="xs" c="dimmed">{when(item.at)}</Text></Group>
              <Text size="sm" fw={600} className="plain-text">{item.title}</Text><Text size="sm" c="dimmed" className="plain-text">{item.detail}</Text></Paper></li>)}</Stack>}</Paper>
      </Stack></Tabs.Panel>
      <Tabs.Panel value="arcs" pt="md"><Group align="flex-start" grow wrap="wrap">
        <Paper withBorder p="md" radius="lg" style={{ flex: '1 1 20rem' }}><Title order={3} size="h5" mb="xs">Линии ({arcRows.length})</Title>
          {arcs.isPending ? <Text size="sm">Получаем линии…</Text> : !arcRows.length ? <Text size="sm" c="dimmed">Линий пока нет.</Text> : <ul className="memory-list" aria-label="Список линий">{arcRows.map((item) => <li key={item.id}>
            <button type="button" className={`memory-item ${item.id === selectedArc ? 'selected' : ''}`} onClick={() => setSelectedArc(item.id)}>
              <Badge size="xs" variant="light">{arcLabels[item.status]}</Badge><Text size="sm" className="plain-text" lineClamp={2}>{item.direction}</Text></button></li>)}</ul>}</Paper>
        <div style={{ flex: '1 1 24rem' }}>{arc ? <ArcCard key={arc.id + arc.version} api={api} scope={scope} run={run} arc={arc} opps={oppRows} /> : <Alert color="gray">Выберите линию, чтобы увидеть основания, пережитое и возможные продолжения.</Alert>}</div>
      </Group></Tabs.Panel>
      <Tabs.Panel value="changes" pt="md"><Group align="flex-start" grow wrap="wrap">
        <Paper withBorder p="md" radius="lg" style={{ flex: '1 1 20rem' }}><Title order={3} size="h5" mb="xs">Изменения ({changeRows.length})</Title>
          {changes.isPending ? <Text size="sm">Получаем изменения…</Text> : !changeRows.length ? <Text size="sm" c="dimmed">Изменений нет: персонаж остаётся прежним.</Text> : <ul className="memory-list" aria-label="Список изменений">{changeRows.map((item) => {
            const patch = patchOf(item);
            return <li key={item.id}><button type="button" className={`memory-item ${item.id === selectedChange ? 'selected' : ''}`} onClick={() => setSelectedChange(item.id)}>
              <Group gap="xs"><Badge size="xs" variant="light" color={awaitingOperator(item) ? 'orange' : 'gray'}>{changeLabels[item.status]}</Badge></Group>
              <Text size="sm">{patch ? `${patch.property}: ${patch.before} → ${patch.after}` : 'Формат правки не распознан'}</Text></button></li>;
          })}</ul>}</Paper>
        <div style={{ flex: '1 1 24rem' }}>{change ? <ChangeCard key={change.id + change.version} api={api} scope={scope} run={run} change={change} /> : <Alert color="gray">Выберите изменение, чтобы сравнить прежнее и новое значение, основания и интерпретацию.</Alert>}</div>
      </Group></Tabs.Panel>
      <Tabs.Panel value="bounds" pt="md"><Stack gap="md">
        <Paper withBorder p="md" radius="lg" aria-label="Допустимые изменения"><Title order={3} size="h5" mb="xs">Допустимые изменения личности</Title>
          {profile ? <><Text size="sm">Профиль {profile.persona_id} · версия {profile.version}</Text>
            <details><summary>Определения свойств</summary><Text size="xs" component="pre" className="plain-text">{JSON.stringify(profile.property_definitions, null, 2)}</Text></details>
            <details><summary>Пределы развития</summary><Text size="xs" component="pre" className="plain-text">{JSON.stringify(profile.development_bounds, null, 2)}</Text></details></> :
            <Text size="sm" c="dimmed">Принятый профиль недоступен.</Text>}
          <Text size="xs" c="dimmed" mt="xs">Идентичность и опорные ценности рассматривает оператор; убеждения, интересы, привычки и отношения развиваются по опыту в пределах профиля.</Text></Paper>
        <Paper withBorder p="md" radius="lg" aria-label="Текущее состояние"><Title order={3} size="h5" mb="xs">Текущее состояние</Title>
          {states.data?.items[0] ? <><Text size="sm">Версия состояния {states.data.items[0].state_version} · профиль v{states.data.items[0].profile_version}</Text>
            <Text size="xs" component="pre" className="plain-text">{JSON.stringify(states.data.items[0].values.data, null, 2)}</Text></> : <Text size="sm" c="dimmed">Состояние недоступно.</Text>}</Paper>
        <Paper withBorder p="md" radius="lg" aria-label="Границы линий"><Title order={3} size="h5" mb="xs">Сюжетные границы и блокировки</Title>
          {!arcRows.length && !oppRows.some((o) => o.status === 'BLOCKED') ? <Text size="sm" c="dimmed">Границ и блокировок нет в загруженных данных.</Text> : <Stack gap="xs">
            {arcRows.map((item) => <Text key={item.id} size="sm" className="plain-text">Линия «{item.direction.slice(0, 60)}»: {item.boundaries}</Text>)}
            {oppRows.filter((o) => o.status === 'BLOCKED').map((o) => <Alert key={o.id} color="orange">Заблокировано: {o.reason_code ?? 'причина не указана'}</Alert>)}</Stack>}
          {gmSettings && <Stack gap="xs" mt="xs" aria-label="Правила ядра">
            <Text size="sm" fw={600}>Правила · версия {gmSettings.rules_version}</Text>
            {((gmSettings.rules.data as unknown as RulesData).rules).map((rule) => <Text size="sm" key={rule.rule_id}>
              {{ hard: 'Жёсткое ограничение', soft: 'Мягкая граница', desired: 'Желательное направление' }[rule.kind]}: {rule.description}
            </Text>)}
            {!((gmSettings.rules.data as unknown as RulesData).rules).length && <Text size="sm">Список правил пуст.</Text>}
          </Stack>}</Paper>
      </Stack></Tabs.Panel>
    </Tabs>
  </Stack>;
}
