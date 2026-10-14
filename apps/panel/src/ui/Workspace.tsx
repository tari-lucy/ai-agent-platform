import { Alert, Badge, Button, Group, Paper, Select, Stack, Text, Title } from '@mantine/core';
import { useEffect, useState } from 'react';
import { NavLink, Navigate, Route, Routes } from 'react-router';
import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import type { OperatorSession, ProfilePage, RunSessionPage, Scope, StopReceipt } from '../shared/contracts.ts';
import { failureMessage, type PanelApi } from '../shared/api.ts';
import { affectedResources, connectEvents, eventsUrl, type ConnectionState } from '../shared/events.ts';
import { partition, projectionKey, scopeKey, scopeLabel } from '../shared/scope.ts';
import { mergeRuns, stopTarget, stopObservation } from '../shared/projections.ts';
import { Dialogue } from './dialogue/Dialogue.tsx';
import { Memory } from './memory/Memory.tsx';
import { WorkspaceScreen } from './workspace/WorkspaceScreen.tsx';
import { LlmScreen } from './llm/LlmScreen.tsx';

const screens = [
  { path: 'dialogue', label: 'Диалог', mark: '01', title: 'Место для разговора', detail: 'Разговор, текущая цель и вводные появятся после подключения диалогового контроллера.', task: 'M4-02' },
  { path: 'persona', label: 'Персонаж', mark: '02', title: 'Характер и его история', detail: 'Редактирование канона, стиля и версий профиля будет доступно в разделе персонажа.', task: 'M4-03' },
  { path: 'memory', label: 'Память и люди', mark: '03', title: 'Опыт, который остаётся', detail: 'Здесь будут источники, встречи, отношения, темы и обещания выбранной личности.', task: 'M4-03 / M4-04' },
  { path: 'development', label: 'Сюжет и развитие', mark: '04', title: 'Путь персонажа', detail: 'Линии, изменения и их основания появятся после подключения гейммастера и развития.', task: 'M4-05' },
  { path: 'laboratory', label: 'Лаборатория', mark: '05', title: 'Проверить в безопасной среде', detail: 'Сценарии будут выполняться в отдельной тестовой памяти. Открытие этого экрана не запускает тест.', task: 'M4-06' },
  { path: 'workspace', label: 'Рабочее пространство', mark: '06', title: 'Состояние рабочего пространства', detail: 'Текущее состояние получено от ядра. Настройка прав, подключений и восстановления появится отдельно.', task: 'M4-07' },
  { path: 'llm', label: 'LLM и расходы', mark: '07', title: 'Понятный учёт', detail: 'Модели, токены и расходы появятся после подключения учёта. Отсутствие данных не означает нулевой расход.', task: 'M4-08' },
] as const;
const runLabels = { STOPPED: 'Остановлен', STARTING: 'Запускается', ACTIVE: 'Работает', STOPPING: 'Останавливается', FAULTED: 'Требуется проверка' };
const connectionLabels: Record<ConnectionState, string> = {
  connecting: 'Подключение событий', connected: 'События подключены', reconnecting: 'Связь прервана · переподключение',
  offline: 'События недоступны', denied: 'Подписка отклонена',
};
function EventLink({ api, scope }: { api: PanelApi; scope: Scope }) {
  const cache = useQueryClient();
  const [status, setStatus] = useState<ConnectionState>('connecting');
  const [revision, setRevision] = useState(0);
  const key = scopeKey(scope);
  useEffect(() => {
    // Construct the exact authorised partition from the key; no render-time socket.
    const [workspace_id, persona_id, memory_space_id, memory_mode] = JSON.parse(key) as [string, string, string, 'work' | 'test'];
    const selected: Scope = { workspace_id, persona_id, memory_space_id, memory_mode };
    return connectEvents({
      scope: selected, url: eventsUrl(window.location), state: setStatus,
      refresh: () => { void cache.invalidateQueries({ queryKey: ['projection', key] }); },
      changed: (event) => {
        for (const resource of affectedResources[event.type]) {
          void cache.invalidateQueries({ queryKey: projectionKey(resource, selected) });
        }
      },
    });
  }, [cache, key, revision]);
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
function ScopeContent({ api, scope, path }: { api: PanelApi; scope: Scope; path: typeof screens[number]['path'] }) {
  const screen = screens.find((item) => item.path === path)!;
  const profile = useQuery({
    queryKey: projectionKey('personas', scope),
    queryFn: ({ signal }) => api.profiles(scope, signal),
    enabled: path === 'persona',
  });
  return <Stack gap="lg">
    <div><Text className="eyebrow">{screen.mark} / {screen.label}</Text><Title order={1}>{screen.title}</Title></div>
    {scope.memory_mode === 'test' &&
      <Alert color="violet" title="Тестовая память">Эти данные изолированы от рабочей биографии. Просмотр не запускает агента.</Alert>}
    <Paper withBorder p="xl" radius="lg">
      <Group justify="space-between" mb="md"><Title order={2} size="h3">Просматриваемая область</Title>
        <Badge color={scope.memory_mode === 'test' ? 'violet' : 'gray'} variant="light">{scope.memory_mode === 'test' ? 'ТЕСТ' : 'РАБОЧАЯ'}</Badge></Group>
      <Text fw={600}>{scope.persona_id}</Text>
      <Text size="sm" c="dimmed">{scope.memory_space_id}</Text>
      <Text mt="md" size="sm">Просмотр профиля не переключает активного агента.</Text>
    </Paper>
    {path === 'persona' && <Paper withBorder p="xl" radius="lg">
      <Title order={2} size="h3" mb="md">Принятый профиль</Title>
      {profile.isPending ? <Text>Получаем профиль…</Text> : profile.isError ?
        <Alert color="orange">{failureMessage(profile.error)}</Alert> :
        <ProfilePreview page={profile.data} />}
    </Paper>}
    <Paper className="future-card" p="xl" radius="lg">
      <Badge color="gray" variant="outline" mb="md">Раздел готовится</Badge>
      <Text>{screen.detail}</Text>
      <Text size="sm" c="dimmed" mt="sm">Основа панели доступна. Функции этого раздела будут подключены следующим этапом.</Text>
    </Paper>
  </Stack>;
}
function ProfilePreview({ page }: { page: ProfilePage | undefined }) {
  if (!page?.items.length) return <Text c="dimmed">Принятых профилей в этой области пока нет.</Text>;
  return <Stack>{!page.complete && <Text size="sm" c="orange.8">Показана первая страница профилей.</Text>}
    {page.items.map((profile) => <div key={profile.id}>
      <Text fw={600}>{profile.persona_id} · версия {profile.version}</Text>
      <Text mt="sm" className="plain-text">{profile.canon}</Text>
      <Text size="sm" c="dimmed" mt="sm" className="plain-text">{profile.style}</Text>
    </div>)}</Stack>;
}
export function Workspace({ api, auth, onLogout }: { api: PanelApi; auth: OperatorSession; onLogout: () => Promise<void> }) {
  const cache = useQueryClient();
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
  const subscribed = [scope, ...(target ? [partition(target.scope)] : [])].filter(
    (item, index, all) => all.findIndex((other) => scopeKey(other) === scopeKey(item)) === index,
  );
  const errors = results.some((result) => result.isError);
  const pending = results.some((result) => result.isPending);
  const confirmed = stopObservation(stopped, target);
  const state = confirmed?.state ?? target?.state;
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
  return <div className="workspace">
    <a className="skip-link" href="#content">Перейти к содержимому</a>
    <header className="workspace-header">
      <div className="header-brand"><Text className="brand">AI Agent Platform</Text><Text size="xs" c="dimmed">Пространство: {scope.workspace_id}</Text></div>
      <div className="view-selector"><Select label="Просматриваемый персонаж и память" value={selected}
        data={auth.scopes.map((item) => ({ value: scopeKey(item), label: scopeLabel(item) }))}
        onChange={(value) => { if (value) setSelected(value); }} allowDeselect={false} aria-label="Просматриваемый персонаж и память" /></div>
      <div className="active-run" aria-live="polite">
        <Text size="xs" c="dimmed">Активный агент · независимо от просмотра</Text>
        <Text fw={600}>{target ? target.scope.persona_id : pending ? 'Получаем состояние…' : 'Нет подтверждённого запуска'}</Text>
        <Text size="sm">{state ? runLabels[state] : 'Состояние неизвестно'}{target?.scope.memory_mode === 'test' ? ' · ТЕСТ' : ''}</Text>
      </div>
      <div className="header-actions"><Button className="stop-button" color="red" size="md" onClick={() => void stop()}
        disabled={!target || stopBusy} aria-label="STOP — остановить активного агента">{stopBusy ? 'Останавливаем…' : 'STOP'}</Button>
        <Button variant="subtle" color="gray" size="compact-sm" onClick={() => void logout()} disabled={logoutBusy}>Выйти</Button></div>
      <div className="header-status">{subscribed.map((item) => <EventLink key={scopeKey(item)} api={api} scope={item} />)}</div>
    </header>
    <aside className="sidebar"><Text className="eyebrow" mb="md">Рабочие области</Text><nav aria-label="Разделы панели">
      {screens.map((screen) => <NavLink key={screen.path} to={'/' + screen.path}
        className={({ isActive }) => `nav-item ${isActive ? 'selected' : ''}`}>
        <span className="nav-number" aria-hidden="true">{screen.mark}</span><span>{screen.label}</span>
      </NavLink>)}</nav>
      <div className="sidebar-note"><Text size="xs" c="dimmed">Один активный агент.<br />Рабочая и тестовая память разделены.</Text></div>
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
      <Routes>{screens.map((screen) => <Route key={screen.path} path={'/' + screen.path}
        element={screen.path === 'dialogue' ? <Dialogue key={selected + screen.path} api={api} scope={scope} /> :
          screen.path === 'memory' ? <Memory key={selected + screen.path} api={api} scope={scope} /> :
          screen.path === 'llm' ? <LlmScreen key={selected + screen.path} api={api} scope={scope} /> :
          screen.path === 'workspace' ? <WorkspaceScreen key={selected + screen.path} api={api} scope={scope} /> :
          <ScopeContent key={selected + screen.path} api={api} scope={scope} path={screen.path} />} />)}
        <Route path="*" element={<Navigate to="/dialogue" replace />} /></Routes>
    </main>
  </div>;
}
