import { Anchor, Badge, Group, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import type { RunSession, Scope } from '../../shared/contracts.ts';
import type { PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { indicators } from '../workspace/model.ts';
import { useHarnessEntry } from '../opencode.ts';
import {
  ago, attention, budgetLine, changeWords, characterNews, gmSummary, headline, lastStep, plan, taskLabels, type Line, type Tone,
} from './model.ts';

const color = { ok: 'teal', wait: 'blue', warn: 'orange', off: 'gray' } as const satisfies Record<Tone, string>;

function Card({ title, to, children }: { title: string; to?: string; children: React.ReactNode }) {
  return <Paper withBorder p="md" radius="md">
    <Stack gap="xs">
      <Group justify="space-between" align="baseline">
        <Title order={2} size="h5">{title}</Title>
        {to && <Anchor component={Link} to={to} size="xs">Подробнее</Anchor>}
      </Group>
      {children}
    </Stack>
  </Paper>;
}
const Dot = ({ line }: { line: Line }) => <Group gap={8} wrap="nowrap" align="flex-start">
  <Badge circle size="xs" color={color[line.tone]} mt={7} aria-hidden="true" />
  <Text size="sm">{line.text}</Text>
</Group>;
const Empty = ({ children }: { children: React.ReactNode }) => <Text size="sm" c="dimmed">{children}</Text>;

// First page of a projection is enough for a summary; a failed read shows a dash, never an invented value.
function useFirst<T>(scope: Scope, resource: string, read: (signal: AbortSignal) => Promise<T>) {
  return useQuery({ queryKey: [...projectionKey(resource, scope), 'now'], queryFn: ({ signal }) => read(signal), refetchInterval: 30_000 });
}

export function NowScreen({ api, scope, run }: { api: PanelApi; scope: Scope; run?: RunSession }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 30_000); return () => clearInterval(timer); }, []);
  const harness = useHarnessEntry() ?? undefined;
  const tasks = useFirst(scope, 'work-tasks', (signal) => api.workTasks(scope, signal));
  const tickets = useFirst(scope, 'tickets', (signal) => api.tickets(scope, signal));
  const changes = useFirst(scope, 'changes', (signal) => api.changes(scope, signal));
  const opportunities = useFirst(scope, 'opportunities', (signal) => api.opportunities(scope, signal));
  const gm = useFirst(scope, 'gm-settings', (signal) => api.gmSettings(scope, signal));

  const head = headline(run);
  const budget = budgetLine(harness);
  const worry = attention(run, tickets.data?.items ?? [], harness, changes.data?.items ?? []);
  const work = plan(tasks.data?.items ?? []);
  const news = characterNews(changes.data?.items ?? []);
  const story = gmSummary(gm.data?.items[0], opportunities.data?.items ?? [], run);
  const doing = work.open.find((task) => task.state === 'active') ?? work.open[0];

  return <Stack gap="lg">
    <div>
      <Title order={1}>Сейчас</Title>
      <Group gap="xs" mt={6}>
        <Badge color={color[head.tone]} size="lg" variant="light">{head.text}</Badge>
        {run && <Text size="sm" c="dimmed">{run.scope.persona_id} · {run.autonomy === 'observation' ? 'наблюдение' : 'автономно'}
          {run.started_at ? ` · с ${ago(run.started_at, now)}` : ''}</Text>}
      </Group>
    </div>

    <Card title={worry.length ? 'Требует внимания' : 'Всё спокойно'}>
      {worry.length ? worry.map((line) => <Dot key={line.text} line={line} />) : <Empty>Ничего не ждёт вашего решения.</Empty>}
    </Card>

    <SimpleGrid cols={{ base: 1, md: 2 }} spacing="md">
      <Card title="Чем занята" to="/work">
        {doing ? <>
          <Text size="sm" fw={600}>{doing.goal}</Text>
          <Text size="xs" c="dimmed">{taskLabels[doing.state]} · {ago(doing.updated_at, now)}</Text>
          {lastStep(doing) && <Text size="sm">Последний шаг: {lastStep(doing)}</Text>}
        </> : <Empty>Явных поручений нет. Что она делает в игре, смотри в окне OpenCode.</Empty>}
      </Card>

      <Card title="Планы" to="/work">
        {work.open.length ? work.open.map((task) => <Group key={task.id} gap={8} wrap="nowrap" align="flex-start">
          <Badge size="xs" variant="light" color={task.state === 'active' ? 'teal' : task.state === 'waiting' ? 'blue' : 'gray'} mt={4}>{taskLabels[task.state]}</Badge>
          <Text size="sm">{task.goal}</Text>
        </Group>) : <Empty>Список пуст.</Empty>}
        {work.done && <Text size="xs" c="dimmed">Последнее сделанное: {work.done.goal} · {ago(work.done.updated_at, now)}</Text>}
      </Card>

      <Card title="Характер" to="/development">
        {news.length ? news.map((change) => <div key={change.id}>
          <Text size="sm">{change.interpretation}</Text>
          <Text size="xs" c="dimmed">{changeWords[change.status]} · {ago(change.updated_at, now)}</Text>
        </div>) : <Empty>Изменений характера пока нет.</Empty>}
      </Card>

      <Card title="Гейммастер" to="/development">
        <Text size="sm">{story.paused ? 'На паузе' : 'Работает'}{story.intensity != null ? ` · интенсивность ${story.intensity}` : ''}</Text>
        <Text size="sm">Предложено сюжетов: {story.offered} · пережито: {story.lived}{story.blocked ? ` · заблокировано: ${story.blocked}` : ''}</Text>
        {story.latest && <Text size="xs" c="dimmed">Последнее: {story.latest.applicability} · {ago(story.latest.updated_at, now)}</Text>}
      </Card>
    </SimpleGrid>

    <Card title="Состояние систем" to="/workspace">
      {run ? indicators(run).map((item) => <Dot key={item.label} line={{ text: `${item.label}: ${item.value}`, tone: item.tone }} />) : <Empty>Нет данных.</Empty>}
      {budget && <Dot line={budget} />}
    </Card>
  </Stack>;
}
