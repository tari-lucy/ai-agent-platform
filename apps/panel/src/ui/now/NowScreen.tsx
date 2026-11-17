import { Anchor, Badge, Group, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import type { RunSession, Scope } from '../../shared/contracts.ts';
import type { PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { useHarnessEntry } from '../opencode.ts';
import { LaunchCard } from './LaunchCard.tsx';
import { Activities, FunnelBar, HourRibbon, PictureStrip, TraitBars } from './Widgets.tsx';
import {
  ago, attention, budgetLine, chips, funnel, gmSummary, headline, ribbon, traits, type Tone,
} from './model.ts';

const color = { ok: 'teal', wait: 'blue', warn: 'orange', off: 'gray' } as const satisfies Record<Tone, string>;
const verb = { said: 'сказала', heard: 'услышала', step: 'шаг', event: 'событие' } as const;

// Soft panels: no borders, a quiet fill, generous air. Details hide behind a click.
function Soft({ title, to, children }: { title?: string; to?: string; children: React.ReactNode }) {
  return <section className="soft">
    {(title || to) && <Group justify="space-between" align="baseline" mb={6}>
      <Text size="sm" fw={600}>{title}</Text>
      {to && <Anchor component={Link} to={to} size="xs" c="dimmed">подробнее</Anchor>}
    </Group>}
    {children}
  </section>;
}
const Quiet = ({ children }: { children: React.ReactNode }) => <Text size="sm" c="dimmed">{children}</Text>;

function useFirst<T>(scope: Scope, resource: string, read: (signal: AbortSignal) => Promise<T>) {
  return useQuery({ queryKey: [...projectionKey(resource, scope), 'now'], queryFn: ({ signal }) => read(signal), refetchInterval: 30_000 });
}

export function NowScreen({ api, scope, run }: { api: PanelApi; scope: Scope; run?: RunSession }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(timer); }, []);
  const [more, setMore] = useState(false);
  const harness = useHarnessEntry() ?? undefined;
  const tickets = useFirst(scope, 'tickets', (signal) => api.tickets(scope, signal));
  const changes = useFirst(scope, 'changes', (signal) => api.changes(scope, signal));
  const states = useFirst(scope, 'character-states', (signal) => api.characterStates(scope, signal));
  const opportunities = useFirst(scope, 'opportunities', (signal) => api.opportunities(scope, signal));
  const gm = useFirst(scope, 'gm-settings', (signal) => api.gmSettings(scope, signal));
  const live = useFirst(scope, 'harness-now', (signal) => api.harnessNow(scope, signal));

  const head = headline(run);
  const budget = budgetLine(harness);
  const worry = attention(run, tickets.data?.items ?? [], harness, changes.data?.items ?? []);
  const settings = gm.data?.items[0];
  const story = gmSummary(settings, opportunities.data?.items ?? [], run);
  const picture = live.data;
  const feed = picture?.lines ?? [];
  const activities = picture?.activities ?? [];
  const day = ribbon(picture?.hours);
  const character = traits(states.data?.items[0], changes.data?.items ?? []);

  return <Stack gap="lg" className="now">
    <div>
      <Group gap="sm" align="center">
        <Title order={1} size="h2">{head.text}</Title>
        <Badge circle size="md" color={color[head.tone]} aria-hidden="true" />
      </Group>
      {run && <Text size="sm" c="dimmed">{run.scope.persona_id}{run.state === 'ACTIVE' && run.started_at ? ` · работает ${ago(run.started_at, now).replace(' назад', '')}` : ''}</Text>}
    </div>

    <Soft><LaunchCard api={api} scope={scope} run={run} settings={settings} harness={harness} /></Soft>

    {worry.length > 0 && <Soft title="Нужно ваше внимание">
      <Stack gap={4}>{worry.map((item) => <Group key={item.text} gap={8} wrap="nowrap">
        <Badge circle size="xs" color={color[item.tone]} aria-hidden="true" /><Text size="sm">{item.text}</Text></Group>)}</Stack>
    </Soft>}

    <Soft title="Чем занята">
      {activities.length || picture?.doing ? <Activities rows={activities} doing={picture?.doing} /> :
        <Quiet>{run?.state === 'ACTIVE' ? 'Занятия появятся, когда она запишет их в свой список.' : 'Не работает.'}</Quiet>}
    </Soft>

    <Soft title="Активность за сутки">
      {day.busy ? <HourRibbon rows={day.rows} peak={day.peak} /> : <Quiet>За последние сутки тихо.</Quiet>}
    </Soft>

    {!!picture?.pictures?.length && <Soft title="Нарисовала"><PictureStrip api={api} scope={scope} pictures={picture.pictures} now={now} /></Soft>}

    <Group grow align="stretch" wrap="wrap" gap="md">
      <Soft title="Характер" to="/development">
        {character.length ? <TraitBars rows={character} now={now} /> : <Quiet>Черты пока не записаны.</Quiet>}
      </Soft>
      <Soft title="Гейммастер" to="/development">
        <FunnelBar funnel={funnel(opportunities.data?.items ?? [])} />
        <Text size="xs" c="dimmed" mt={10}>{story.paused ? 'на паузе' : 'работает'}
          {story.latest ? ` · последнее: ${story.latest.applicability} · ${ago(story.latest.updated_at, now)}` : ''}</Text>
      </Soft>
    </Group>

    <Soft title="Что происходит">
      {feed.length ? <Stack gap={6}>
        {feed.slice(0, more ? 25 : 5).map((item) => <Group key={item.at + item.kind + item.text} gap={10} wrap="nowrap" align="baseline">
          <Text size="xs" c={item.kind === 'said' ? 'teal' : item.kind === 'heard' ? 'blue' : 'dimmed'} w={64}>{verb[item.kind]}</Text>
          <Text size="sm" style={{ flex: 1 }}>{item.text}</Text>
          <Text size="xs" c="dimmed" style={{ whiteSpace: 'nowrap' }}>{ago(item.at, now)}</Text>
        </Group>)}
        {feed.length > 5 && <Anchor component="button" type="button" size="xs" c="dimmed" ta="left" onClick={() => setMore(!more)}>
          {more ? 'свернуть' : 'показать ещё'}</Anchor>}
      </Stack> : <Quiet>Пока тихо.</Quiet>}
    </Soft>

    <Group gap="xs" aria-label="Состояние связей">
      {chips(run, harness).map((item) => <Badge key={item.text} variant="light" color={color[item.tone]} size="md" tt="none" fw={400}>{item.text}</Badge>)}
      {budget?.tone === 'warn' && <Badge variant="light" color="orange" size="md" tt="none" fw={400}>{budget.text}</Badge>}
    </Group>
  </Stack>;
}
