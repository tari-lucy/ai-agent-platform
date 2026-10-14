import { TransferSettings } from './TransferSettings.tsx';
import { DownloadReport } from './DownloadReport.tsx';
import { formatTime } from '../time.ts';
import { Alert, Badge, Button, Checkbox, Group, Paper, Stack, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { LaboratoryReport, RunSession, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { startLaboratory } from '../../shared/owner-commands.ts';
import { projectionKey } from '../../shared/scope.ts';
import {
  compare, createCommand, eventCommand, eventProblem, isLive, labOpen, latestPerScenario, parseSummary,
  sortReports, startProblem, statusLabels, stopCommand, titleOf, type Start,
} from './model.ts';

const when = (value: string) => formatTime(value, 'medium');
const emptyStart: Start = { scenario: '', seed: 'lab-seed', real: false, typed: '', acknowledged: false };

// One command at a time; a lost reply is checked by re-sending the identical command.
function useAction(api: PanelApi, scope: Scope, refresh: string[], prepare = false) {
  const cache = useQueryClient();
  const [state, setState] = useState<{ phase: 'idle' | 'sending' | 'done'; resource?: string | null } | { phase: 'unknown'; command: Parameters<PanelApi['command']>[0] } | { phase: 'refused'; code: string }>({ phase: 'idle' });
  async function run(command: Parameters<PanelApi['command']>[0]) {
    setState({ phase: 'sending' });
    try { const result = await api.command(command); setState({ phase: 'done', resource: result.resource_id }); }
    catch (error) {
      const code = error instanceof ApiFailure ? error.code : 'NETWORK';
      setState(['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(code) ? { phase: 'unknown', command } : { phase: 'refused', code });
    } finally { for (const resource of refresh) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) }); }
  }
  const view = <>
    {state.phase === 'done' && <Alert color="teal" mt="xs" role="status">{prepare ? `Тестовая область подготовлена: ${state.resource ?? "обновите список"}. Выберите её в верхнем списке.` : "Команда принята ядром. Результат появится в отчётах."}</Alert>}
    {state.phase === 'refused' && <Alert color="orange" mt="xs" role="alert">{state.code === 'VERSION_CONFLICT' ? 'Запись уже изменена. Обновите список и повторите.' : failureMessage(new ApiFailure(state.code))} Ничего не запущено.</Alert>}
    {state.phase === 'unknown' && <Alert color="orange" mt="xs" role="alert" title="Результат неизвестен">
      Команда могла быть выполнена. Проверка повторяет ту же команду; второго запуска не будет.
      <Button mt="xs" size="compact-sm" variant="light" onClick={() => void run(state.command)}>Проверить исход</Button></Alert>}
  </>;
  return { run, busy: state.phase === 'sending' || state.phase === 'unknown', view };
}

export function LaboratoryScreen({ api, scope, run, scopes = [] }: { api: PanelApi; scope: Scope; run?: RunSession; scopes?: Scope[] }) {
  const catalogue = useQuery({
    queryKey: projectionKey('laboratory-catalogue', scope),
    queryFn: ({ signal }) => api.laboratoryCatalogue(scope, signal), enabled: labOpen(scope),
  });
  const entries = catalogue.data?.scenarios.map((s) => ({ id: s.scenario_id, title: s.title, tags: s.tags })) ?? [];
  const reportsQuery = useInfiniteQuery({
    queryKey: [...projectionKey('laboratory-reports', scope), 'screen'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => api.laboratoryReports(scope, signal, pageParam),
    getNextPageParam: (last) => (last.complete || !last.next_cursor ? undefined : last.next_cursor), enabled: labOpen(scope),
  });
  const reports = sortReports(reportsQuery.data?.pages.flatMap((p) => p.items) ?? []);
  const latest = latestPerScenario(reports);
  const start = useAction(api, scope, ['laboratory-reports']);
  const event = useAction(api, scope, ['laboratory-reports']);
  const stop = useAction(api, scope, ['laboratory-reports']);
  const create = useAction(api, scope, ['sessions'], true);
  const [form, setForm] = useState<Start>(emptyStart);
  const [problem, setProblem] = useState<string | null>(null);
  const [text, setText] = useState('');
  const [eventIssue, setEventIssue] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const set = (patch: Partial<Start>) => setForm({ ...form, ...patch });
  const live = reports.some(isLive);
  const open = labOpen(scope);
  function launch() {
    const issue = startProblem(scope, form, start.busy || live, entries);
    if (issue) { setProblem(issue); return; }
    const scenario = catalogue.data?.scenarios.find((entry) => entry.scenario_id === form.scenario);
    if (!run || run.state !== 'ACTIVE' || run.activity !== 'OPEN' || !scenario) {
      setProblem('Нужен активный запуск с открытой деятельностью и актуальный каталог.'); return;
    }
    if (form.real) { setProblem('В этом подключении доступны только mock-сценарии.'); return; }
    setProblem(null); void start.run(startLaboratory(run, scenario, scope, form.seed, crypto.randomUUID()));
  }
  function submitEvent() {
    const issue = eventProblem(scope, text);
    if (issue) { setEventIssue(issue); return; }
    setEventIssue(null); void event.run(eventCommand(scope, text, crypto.randomUUID()));
  }
  const toggle = (id: string) => setPicked((now) => (now.includes(id) ? now.filter((x) => x !== id) : [...now, id].slice(-2)));
  const [a, b] = picked.map((id) => reports.find((r) => r.id === id)).filter((r): r is LaboratoryReport => !!r);
  const replay = a && b ? compare(a, b) : null;
  return <Stack gap="lg">
    <div><Text className="eyebrow">05 / Лаборатория</Text><Title order={1}>Лаборатория</Title></div>
    <Alert color="blue" title="Что проверяет лаборатория" role="note">Зелёный mock-сценарий проверяет интерфейсы, границы и сбои, но не доказывает качество личности или LLM. Реальные модельные вызовы платные и входят в общий расход.</Alert>
    {open ? <Alert color="violet" title="Тестовая память">Эксперимент идёт в отдельной тестовой биографии. Ничего из неё не попадает в рабочую память автоматически.</Alert> :
      <Paper withBorder p="md" radius="lg"><Title order={2} size="h4" mb="xs">Тестовая область не выбрана</Title>
        <Text size="sm">Сейчас выбрана рабочая память. Лаборатория не запускает сценарии и не принимает события в рабочей биографии. Выберите тестовую область в верхнем списке или зарезервируйте свободную подготовленную область для этой личности (опыт не копируется).</Text>
        <Button mt="sm" variant="light" disabled={create.busy} onClick={() => void create.run(createCommand(scope, crypto.randomUUID()))}>Создать изолированную тестовую область</Button>{create.view}<Text size="xs" c="dimmed" mt="xs">После подготовки выберите тестовую память в верхнем списке. Если свободных областей нет, их нужно добавить при настройке приложения.</Text></Paper>}
    {open && <>
      {catalogue.isError && <Alert color="orange" title="Каталог недоступен">{failureMessage(catalogue.error)}</Alert>}
      {reportsQuery.isError && <Alert color="orange" title="Отчёты недоступны">{failureMessage(reportsQuery.error)} Список может быть неполным.</Alert>}
      {reportsQuery.hasNextPage && <Alert color="orange">Загружена не вся история запусков. <Button ml="sm" size="compact-sm" variant="light" onClick={() => void reportsQuery.fetchNextPage()}>Загрузить ещё</Button></Alert>}
      <Paper withBorder p="md" radius="lg" aria-label="Каталог сценариев"><Title order={2} size="h4" mb="xs">Каталог сценариев</Title>
        <ul className="plain-list">{entries.map((entry) => {
          const last = latest.get(entry.id);
          return <li key={entry.id}><Paper withBorder p="xs" mb={6}><Group justify="space-between" wrap="nowrap">
            <div><Text size="sm" fw={600}>{entry.title}</Text><Text size="xs" c="dimmed">{entry.tags.join(' · ')}</Text></div>
            <Group gap="xs"><Badge variant="light" color={last?.status === 'passed' ? 'teal' : last?.status === 'failed' || last?.status === 'unknown' ? 'orange' : 'gray'}>{last ? statusLabels[last.status] : 'Не запускался'}</Badge>
              <Button size="compact-xs" variant={form.scenario === entry.id ? 'filled' : 'light'} onClick={() => set({ scenario: entry.id })}>Выбрать</Button></Group></Group></Paper></li>;
        })}</ul></Paper>
      <Paper withBorder p="md" radius="lg" aria-label="Запуск сценария"><Title order={2} size="h4" mb="xs">Запуск</Title>
        <Text size="sm">Сценарий: <b>{form.scenario ? (entries.find((entry) => entry.id === form.scenario)?.title ?? form.scenario) : 'не выбран'}</b></Text>
        <TextInput mt="xs" label="Seed (фиксируется в отчёте)" value={form.seed} onChange={(e) => set({ seed: e.currentTarget.value.trim() })} maxLength={64} />
        <Text size="xs" c="dimmed" mt="xs">Доступен детерминированный mock: без сети, без расхода. Реальные API здесь не подключены.</Text>
        <Group mt="sm"><Button disabled={start.busy || live} onClick={launch}>Запустить</Button></Group>
        {live && <Text size="xs" c="orange.8" role="status">Идёт запуск: новый можно начать после его завершения или остановки.</Text>}
        {problem && <Alert color="orange" mt="xs" role="alert">{problem}</Alert>}{start.view}</Paper>
      <Paper withBorder p="md" radius="lg" aria-label="Отчёты"><Title order={2} size="h4" mb="xs">Отчёты</Title>
        {reportsQuery.isPending ? <Text size="sm">Получаем отчёты…</Text> : !reports.length ? <Text size="sm" c="dimmed">Отчётов пока нет.</Text> :
          <ul className="plain-list">{reports.map((report) => {
            const info = parseSummary(report.summary);
            return <li key={report.id}><Paper withBorder p="xs" mb={6}><Group justify="space-between" wrap="nowrap" align="flex-start">
              <div><Text size="sm" fw={600}>{titleOf(report.scenario_id)} · v{report.scenario_version}</Text>
                <Text size="xs" c="dimmed">{when(report.updated_at)}</Text>
                {info ? <Text size="sm">Режим: mock · проверок {info.checks}, не прошло {info.failed} · seed {info.seed} · отпечаток {info.digest}</Text> : <Text size="sm" c="orange.8">Сводка не распознана; режим не определён.</Text>}</div>
              <Stack gap={4} align="flex-end"><Badge variant="light" color={report.status === 'passed' ? 'teal' : report.status === 'failed' || report.status === 'unknown' ? 'orange' : 'gray'}>{statusLabels[report.status]}</Badge>
                <DownloadReport key={report.id + ':' + report.version + ':' + (report.report_artifact?.checksum ?? '')}
                  api={api} scope={scope} report={report} />
                <Checkbox size="xs" label="Сравнить" checked={picked.includes(report.id)} onChange={() => toggle(report.id)} />
                {isLive(report) && <Button size="compact-xs" color="red" variant="light" disabled={stop.busy} onClick={() => void stop.run(stopCommand(scope, report, crypto.randomUUID()))}>Остановить</Button>}</Stack></Group>
              {report.status === 'unknown' && <Text size="xs" c="orange.8">Исход неизвестен: результат не засчитан как пройденный.</Text>}</Paper></li>;
          })}</ul>}
        {replay && <Alert mt="xs" color={replay.verdict === 'same' ? 'teal' : 'orange'} role="status" aria-label="Сравнение отчётов">{replay.text}</Alert>}{stop.view}</Paper>
      <Paper withBorder p="md" radius="lg" aria-label="Подача события"><Title order={2} size="h4" mb="xs">Подать событие</Title>
        <Textarea label="Текст события" autosize minRows={2} maxLength={2000} value={text} onChange={(e) => setText(e.currentTarget.value)} />
        <Group mt="xs" justify="flex-end"><Button variant="light" disabled={event.busy} onClick={submitEvent}>Подать</Button></Group>
        {eventIssue && <Alert color="orange" mt="xs" role="alert">{eventIssue}</Alert>}{event.view}
        <Text size="xs" c="dimmed">Шаги сценария и виртуальные часы ядро панели не отдаёт: ход виден только в итоговом отчёте.</Text></Paper>
      <TransferSettings api={api} source={scope} scopes={scopes} run={run} />
    </>}
  </Stack>;
}
