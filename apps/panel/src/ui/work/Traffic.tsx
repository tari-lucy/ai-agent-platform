import { Badge, Button, Group, Paper, Stack, Table, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type { EgressRequest, Scope } from '../../shared/contracts.ts';
import { failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import { megabytes, utcDay } from './traffic.ts';

const statusLabels: Record<EgressRequest['status'], string> = { ok: 'пропущен', denied: 'отказ', limit: 'лимит', error: 'ошибка' };
const statusColor: Record<EgressRequest['status'], string> = { ok: 'teal', denied: 'orange', limit: 'red', error: 'gray' };

// The persona's internet log (W3b): read only when the operator opens it; local data, never sent on.
export function Traffic({ api, scope }: { api: PanelApi; scope: Scope }) {
  const [back, setBack] = useState<number | null>(null);
  const day = back === null ? null : utcDay(new Date(), back);
  const log = useQuery({
    queryKey: [...projectionKey('egress-log', scope), day],
    queryFn: ({ signal }) => api.egressLog(scope, day as string, signal),
    enabled: day !== null,
  });
  const data = log.data;
  return <Paper withBorder p="md" aria-label="Интернет">
    <Stack gap="xs">
      <Group justify="space-between">
        <Title order={2} size="h5">Интернет</Title>
        {back === null ? <Button variant="subtle" size="compact-sm" onClick={() => setBack(0)}>Показать журнал</Button> :
          <Group gap={4}>
            <Button variant="subtle" size="compact-sm" onClick={() => setBack(back + 1)} disabled={back >= 30}>Раньше</Button>
            <Button variant="subtle" size="compact-sm" onClick={() => setBack(back - 1)} disabled={back === 0}>Позже</Button>
            <Button variant="subtle" size="compact-sm" onClick={() => setBack(null)}>Свернуть</Button>
          </Group>}
      </Group>
      <Text size="sm" c="dimmed">Запросы агента через шлюз платформы: у HTTP полный адрес, у HTTPS только сайт. Журнал хранится только на этом компьютере.</Text>
      {day !== null && (log.isError ? <Text size="sm" c="orange.8">{failureMessage(log.error)}</Text> :
        !data ? <Text size="sm">Получаем журнал…</Text> :
        !data.enabled ? <Text size="sm">Интернет для рабочих разговоров не включён на этом хосте (enable-work-sessions --internet).</Text> : <>
          <Text size="sm">
            {data.day} (UTC): {data.requests} запросов, отказов {data.denied}, трафик {megabytes(data.used_bytes)}
            {data.daily_limit_bytes != null && ' из ' + megabytes(data.daily_limit_bytes) + ' в сутки'}
            {data.turn_limit_bytes != null && '; за ход не больше ' + megabytes(data.turn_limit_bytes)}.
          </Text>
          {data.items.length === 0 ? <Text size="sm" c="dimmed">Запросов за этот день нет.</Text> :
            <Table striped withTableBorder fz="xs" aria-label={'Журнал ' + data.day}>
              <Table.Thead><Table.Tr><Table.Th>Время</Table.Th><Table.Th>Адрес</Table.Th><Table.Th>Итог</Table.Th><Table.Th>Объём</Table.Th></Table.Tr></Table.Thead>
              <Table.Tbody>{data.items.map((item, index) => <Table.Tr key={index}>
                <Table.Td>{formatTime(item.at, 'short')}</Table.Td>
                <Table.Td style={{ wordBreak: 'break-all' }}>{item.method} {item.target}</Table.Td>
                <Table.Td><Badge size="xs" color={statusColor[item.status]}>{statusLabels[item.status]}</Badge>{item.reason ? ' ' + item.reason : ''}</Table.Td>
                <Table.Td>{megabytes(item.up + item.down)}</Table.Td>
              </Table.Tr>)}</Table.Tbody>
            </Table>}
          {!data.complete && <Text size="xs" c="dimmed">Показаны последние {data.items.length} запросов.</Text>}
        </>)}
    </Stack>
  </Paper>;
}
