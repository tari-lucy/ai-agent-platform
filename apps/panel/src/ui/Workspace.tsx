import { Alert, Anchor, Button, Group, Select, Text } from '@mantine/core';
import { useCallback, useEffect, useRef, useState } from 'react';
import { NavLink, Navigate, Route, Routes } from 'react-router';
import { useQueries, useQueryClient } from '@tanstack/react-query';
import type { OperatorSession, RunSession, RunSessionPage, Scope, StopReceipt } from '../shared/contracts.ts';
import { failureMessage, type PanelApi } from '../shared/api.ts';
import { affectedResources, connectEvents, eventsUrl, type ConnectionState } from '../shared/events.ts';
import { partition, projectionKey, scopeKey, scopeLabel, samePartition } from '../shared/scope.ts';
import { appendPreview, previewKey, type PrivatePreview } from '../shared/private-preview.ts';
import { mergeRuns, stopTarget, stopObservation } from '../shared/projections.ts';
import { Dialogue } from './dialogue/Dialogue.tsx';
import { Memory } from './memory/Memory.tsx';
import { WorkspaceScreen } from './workspace/WorkspaceScreen.tsx';
import { LlmScreen } from './llm/LlmScreen.tsx';
import { DevelopmentScreen } from './development/DevelopmentScreen.tsx';
import { LaboratoryScreen } from './laboratory/LaboratoryScreen.tsx';
import { PersonaScreen } from './persona/PersonaScreen.tsx';
import { TicketsScreen } from './tickets/TicketsScreen.tsx';
import { LibraryScreen } from './library/LibraryScreen.tsx';
import { WorkScreen } from './work/WorkScreen.tsx';
import { RunPowerControl } from './RunPowerControl.tsx';
import { PanelGuide } from './help/PanelGuide.tsx';
import { groups, screens } from './areas.ts';
import { HarnessEntry, NativeEntry, nativeAgent, nativeHref, useHarness, useNative } from './opencode.ts';


const runLabels = { STOPPED: 'Остановлен', STARTING: 'Запускается', ACTIVE: 'Работает', STOPPING: 'Останавливается', FAULTED: 'Требуется проверка' };
const connectionLabels: Record<ConnectionState, string> = {
  connecting: 'Подключение событий', connected: 'События подключены', reconnecting: 'Связь прервана · переподключение',
  offline: 'События недоступны', denied: 'Подписка отклонена',
};
function EventLink({ api, scope, onState }: { api: PanelApi; scope: Scope; onState: (key: string, state: ConnectionState) => void }) {
  const cache = useQueryClient();
  const [status, setStatus] = useState<ConnectionState>('connecting');
  const [revision, setRevision] = useState(0);
  const key = scopeKey(scope);
  useEffect(() => {
    // Construct the exact authorised partition from the key; no render-time socket.
    const [workspace_id, persona_id, memory_space_id, memory_mode] = JSON.parse(key) as [string, string, string, 'work' | 'test'];
    const selected: Scope = { workspace_id, persona_id, memory_space_id, memory_mode };
    return connectEvents({
      scope: selected, url: eventsUrl(window.location), state: (next) => { setStatus(next); onState(key, next); },
      refresh: () => { void cache.invalidateQueries({ queryKey: ['projection', key] }); },
      stream: (event) => {
        const run = cache.getQueryData<RunSessionPage>(projectionKey('sessions', selected))?.items[0];
        if (!run || run.scope.run_id !== event.scope.run_id || run.scope.session_id !== event.scope.session_id ||
            run.state !== 'ACTIVE' || run.activity !== 'OPEN') return;
        cache.setQueryData<PrivatePreview>(previewKey(selected), (previous) => appendPreview(previous, event));
      },
      changed: (event) => {
        if (event.type === 'run.changed') cache.removeQueries({ queryKey: previewKey(selected) });
        for (const resource of affectedResources[event.type]) {
          void cache.invalidateQueries({ queryKey: projectionKey(resource, selected) });
        }
      },
    });
  }, [cache, key, revision, onState]);
  useEffect(() => {
    if (status !== 'denied') return;
    // Distinguish expired cookie from a rejected subscription; never silently mint a session.
    void api.restore().catch(() => {});
  }, [api, status]);
  return <Group gap="xs" className="connection-line">
    <Text size="xs" c={status === 'connected' ? 'dimmed' : 'orange.8'}>{scope.persona_id}: {connectionLabels[status]}</Text>
    {(status === 'offline' || status === 'denied') &&
      <Button variant="subtle" size="compact-xs" onClick={() => setRevision((n) => n + 1)}>Повторить подключение</Button>}
  </Group>;
}
type ScreenProps = { api: PanelApi; scope: Scope; run?: RunSession; scopes?: Scope[]; connection?: ConnectionState };
// Every area has its own owner-built screen; a missing one would be a compile error, not a silent placeholder.
const screenComponents: Record<typeof screens[number]['path'], (props: ScreenProps) => React.JSX.Element> = {
  dialogue: Dialogue, persona: PersonaScreen, memory: Memory, development: DevelopmentScreen,
  laboratory: LaboratoryScreen, workspace: WorkspaceScreen, llm: LlmScreen, tickets: TicketsScreen, library: LibraryScreen, work: WorkScreen,
};
export function Workspace({ api, auth, onLogout }: { api: PanelApi; auth: OperatorSession; onLogout: () => Promise<void> }) {
  const cache = useQueryClient();
  const header = useRef<HTMLElement>(null);
  useEffect(() => {
    const node = header.current;
    if (!node) return;
    // Scope names and connection notices wrap; keep the content below the actual header.
    const observer = new ResizeObserver(() => {
      node.parentElement?.style.setProperty('--header-offset', `${Math.ceil(node.getBoundingClientRect().height)}px`);
    });
    observer.observe(node);
    return () => observer.disconnect();
  }, []);
  const [connections, setConnections] = useState<Record<string, ConnectionState>>({});
  const recordConnection = useCallback((key: string, state: ConnectionState) => {
    setConnections((previous) => previous[key] === state ? previous : { ...previous, [key]: state });
  }, []);
  const native = useNative(api);
  const [selected, setSelected] = useState(scopeKey(auth.scopes[0]));
  const [stopBusy, setStopBusy] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);
  const [stopped, setStopped] = useState<StopReceipt | null>(null);
  const [logoutError, setLogoutError] = useState<string | null>(null);
  const [logoutBusy, setLogoutBusy] = useState(false);
  const results = useQueries({
    queries: auth.scopes.map((scope) => ({
      queryKey: projectionKey('sessions', scope),
      queryFn: async ({ signal }: { signal: AbortSignal }) => {
        const next = await api.runs(scope, signal);
        return mergeRuns(cache.getQueryData<RunSessionPage>(projectionKey('sessions', scope)), next);
      },
    })),
  });
  const rows = results.flatMap((result) => result.data?.items ?? []);
  const target = stopTarget(rows);
  const scope = auth.scopes.find((item) => scopeKey(item) === selected) ?? auth.scopes[0];
  const harness = useHarness(api, scope);
  const subscribed = [scope, ...(target ? [partition(target.scope)] : [])].filter(
    (item, index, all) => all.findIndex((other) => scopeKey(other) === scopeKey(item)) === index,
  );
  const errors = results.some((result) => result.isError);
  const pending = results.some((result) => result.isPending);
  const confirmed = stopObservation(stopped, target);
  const state = confirmed?.state ?? target?.state;
  const targetRead = target ? results[auth.scopes.findIndex((item) => samePartition(item, target.scope))] : undefined;
  const startReady = !!targetRead?.isSuccess && !targetRead.isFetching;
  async function stop() {
    if (!target || stopBusy) return;
    setStopBusy(true); setStopError(null);
    // Dedicated direct HTTP path: independent from query/WS/login busy states.
    try {
      const receipt = await api.stop(target.scope, target.agent_id);
      setStopped(receipt);
      void cache.invalidateQueries({ queryKey: ['projection'] });
    } catch (error) {
      setStopError(failureMessage(error) + ' Подтверждение остановки не получено.');
      void cache.invalidateQueries({ queryKey: ['projection'] });
    } finally { setStopBusy(false); }
  }
  async function logout() {
    setLogoutBusy(true); setLogoutError(null);
    try { await onLogout(); } catch (error) { setLogoutError(failureMessage(error)); }
    finally { setLogoutBusy(false); }
  }
  return <HarnessEntry.Provider value={harness}><NativeEntry.Provider value={native}><div className="workspace">
    <a className="skip-link" href="#content">Перейти к содержимому</a>
    <header ref={header} className="workspace-header">
      <div className="header-brand"><Text className="brand">AI Agent Platform</Text><Text size="xs" c="dimmed">Пространство: {scope.workspace_id}</Text></div>
      <div className="view-selector"><Select label="Просматриваемый персонаж и память" value={selected}
        data={auth.scopes.map((item) => ({ value: scopeKey(item), label: scopeLabel(item) }))}
        onChange={(value) => { if (value) setSelected(value); }} allowDeselect={false} aria-label="Просматриваемый персонаж и память" /></div>
      <div className="active-run" aria-live="polite">
        <Text size="xs" c="dimmed">Активный агент · независимо от просмотра</Text>
        <Text fw={600}>{target ? target.scope.persona_id : pending ? 'Получаем состояние…' : 'Нет подтверждённого запуска'}</Text>
        <Text size="sm">{state ? runLabels[state] : 'Состояние неизвестно'}{target?.scope.memory_mode === 'test' ? ' · ТЕСТ' : ''}</Text>
      </div>
      <div className="header-actions"><RunPowerControl api={api} run={target} state={state}
        ready={startReady} stopBusy={stopBusy} stopError={stopError} onStop={stop} />
        {harness?.enabled && harness.url ? <Button component="a" href={harness.url} target="_blank" rel="noopener"
          size="compact-sm" variant={harness.ready ? 'filled' : 'light'}
          title={harness.ready ? 'Окно агента в OpenCode' : 'Агент не запущен: нажмите START'}>
          Открыть OpenCode{harness.ready ? '' : ' · не запущен'}</Button>
          : nativeAgent(native, scope) && <Anchor href={nativeHref(nativeAgent(native, scope)!.directory)} size="sm">Открыть OpenCode</Anchor>}
        <Button variant="subtle" color="gray" size="compact-sm" onClick={() => void logout()} disabled={logoutBusy}>Выйти</Button></div>
      <div className="header-status">{subscribed.map((item) => <EventLink key={scopeKey(item)} api={api} scope={item} onState={recordConnection} />)}</div>
    </header>
    <aside className="sidebar"><nav aria-label="Разделы панели">
      {groups.map((group) => <div key={group.id} className="nav-group" role="group" aria-label={group.title}>
        <Text className="eyebrow nav-group-title" aria-hidden="true">{group.title}</Text>
        {screens.filter((screen) => screen.group === group.id).map((screen) => <NavLink key={screen.path} to={'/' + screen.path}
          className={({ isActive }) => `nav-item ${isActive ? 'selected' : ''}`}><span>{native && screen.path === 'dialogue' ? 'Прежний диалог · чтение' : screen.label}</span></NavLink>)}
      </div>)}</nav>
      <div className="sidebar-note"><Text size="xs" c="dimmed">Панель — управление и наблюдение. Один активный агент; рабочая и тестовая память разделены.</Text></div>
    </aside>
    <main id="content" tabIndex={-1} className="main-content">
      {errors && <Alert color="orange" mb="lg" title="Часть состояния недоступна">
        Последние данные могут быть устаревшими. STOP использует отдельный HTTP-запрос.
        <Button variant="subtle" size="compact-sm" ml="sm" onClick={() => void cache.invalidateQueries({ queryKey: ['projection'] })}>Обновить данные</Button>
      </Alert>}
      {!target && !pending && <Alert color="orange" mb="lg">
        {rows.length > 1 ? 'Обнаружено несколько запусков. Требуется проверка ядра; цель STOP нельзя выбирать по профилю просмотра.' :
          'Адрес активного агента ещё не получен. STOP станет доступен после получения состояния ядра.'}
      </Alert>}
      {stopError && <Alert color="red" mb="lg" title="Результат STOP неизвестен">{stopError}</Alert>}
      {confirmed && <Alert color={confirmed.state === 'STOPPED' ? 'teal' : 'orange'} mb="lg" title={runLabels[confirmed.state]}>
        {confirmed.state === 'STOPPED' ? 'Ядро подтвердило остановку. Возобновление требует отдельного действия.' :
          'Допуски отозваны. Подтверждение освобождения ресурсов ещё требуется.'}
        {!!confirmed.unconfirmed_resource_ids?.length && <Text size="sm">{confirmed.unconfirmed_resource_ids.join(', ')}</Text>}
      </Alert>}
      {logoutError && <Alert color="red" mb="lg">{logoutError}</Alert>}
      <PanelGuide scope={scope} run={target ?? undefined} fresh={!!targetRead?.isSuccess && !errors} />
      <Routes>{screens.map((screen) => {
        const Screen = screenComponents[screen.path];
        return <Route key={screen.path} path={'/' + screen.path} element={<Screen key={selected + screen.path} api={api} scope={scope} run={target ?? undefined} scopes={auth.scopes} connection={connections[scopeKey(scope)] ?? 'connecting'} />} />;
      })}
        <Route path="*" element={<Navigate to="/workspace" replace />} /></Routes>
    </main>
  </div></NativeEntry.Provider></HarnessEntry.Provider>;
}
