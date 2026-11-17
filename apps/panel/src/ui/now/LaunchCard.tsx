import { Alert, Anchor, Group, SegmentedControl, Stack, Switch, Text } from '@mantine/core';
import { useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { Link } from 'react-router';
import type { GmRuntimeState, HarnessStatus, RunSession, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { gmIntensityCommand, gmPauseCommand, intensities, type Intensity } from '../development/model.ts';
import { loadAutonomy, saveAutonomy, type Autonomy } from './model.ts';

// Modes are chosen here, before START. START itself stays in the header; it reads the same choice.
const levelOf = (value: number | null | undefined): Intensity => (value == null ? 'normal' : value < 0.35 ? 'low' : value > 0.65 ? 'high' : 'normal');

export function LaunchCard({ api, scope, run, settings, harness }: {
  api: PanelApi; scope: Scope; run?: RunSession; settings?: GmRuntimeState; harness?: HarnessStatus;
}) {
  const cache = useQueryClient();
  const [mode, setMode] = useState<Autonomy>(() => loadAutonomy(globalThis.localStorage));
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const stopped = run?.state === 'STOPPED';
  async function send(make: (requestId: string) => Parameters<PanelApi['command']>[0]) {
    setProblem(null); setBusy(true);
    try { await api.command(make(crypto.randomUUID())); }
    catch (error) { setProblem(failureMessage(error instanceof ApiFailure ? error : new ApiFailure('NETWORK'))); }
    finally {
      setBusy(false);
      for (const resource of ['gm-settings', 'sessions']) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
    }
  }
  const budget = harness?.enabled && harness.daily_budget
    ? `${harness.spent_today?.toFixed(2) ?? '—'} из ${harness.daily_budget} ${harness.currency === 'RUB' ? '₽' : harness.currency ?? ''} за сегодня` : null;
  return <Stack gap="sm">
    <Group justify="space-between" wrap="wrap" gap="md">
      <div>
        <Text size="xs" c="dimmed">Режим работы</Text>
        <SegmentedControl size="sm" value={mode} aria-label="Режим работы при запуске"
          onChange={(value) => { setMode(value as Autonomy); saveAutonomy(globalThis.localStorage, value as Autonomy); }}
          data={[{ value: 'autonomous', label: 'Автономно' }, { value: 'observation', label: 'Наблюдение' }]} />
      </div>
      <div>
        <Text size="xs" c="dimmed">Гейммастер</Text>
        <Group gap="sm">
          <Switch checked={!run?.gm_paused} disabled={!run || !settings || busy} aria-label="Гейммастер включён"
            label={run?.gm_paused ? 'на паузе' : 'включён'}
            onChange={() => run && settings && void send((id) => gmPauseCommand(run, settings, !run.gm_paused, id))} />
          <SegmentedControl size="xs" disabled={!settings || busy} value={levelOf(settings?.intensity)} aria-label="Интенсивность гейммастера"
            onChange={(value) => settings && void send((id) => gmIntensityCommand({ scope }, settings, value as Intensity, id))}
            data={Object.entries(intensities).map(([value, label]) => ({ value, label }))} />
        </Group>
      </div>
      <div>
        <Text size="xs" c="dimmed">Бюджет</Text>
        <Text size="sm">{budget ?? 'не задан'} · <Anchor component={Link} to="/llm" size="sm">изменить</Anchor></Text>
      </div>
    </Group>
    <Text size="xs" c="dimmed">
      {mode === 'autonomous' ? 'Автономно: сама выбирает занятия и действует.' : 'Наблюдение: смотрит и отвечает, но сама ничего не начинает.'}
      {stopped ? ' Режим применится при следующем START.' : ' Режим запуска меняется только при следующем START.'}
    </Text>
    {problem && <Alert color="orange" role="alert">{problem}</Alert>}
  </Stack>;
}
