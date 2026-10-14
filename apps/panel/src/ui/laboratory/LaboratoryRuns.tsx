import { Alert, Button, Group, Paper, Stack, Text, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import type { PanelApi } from '../../shared/api.ts';
import { ApiFailure, failureMessage } from '../../shared/api.ts';
import type { EditCommand, LaboratoryRun, RunSession, Scope } from '../../shared/contracts.ts';
import { eventLaboratory, stopLaboratory } from '../../shared/owner-commands.ts';
import { projectionKey, samePartition } from '../../shared/scope.ts';
const terminal = new Set(['SUCCEEDED', 'FAILED', 'REJECTED', 'CANCELLED', 'EXPIRED', 'OUTCOME_UNKNOWN']);
export function LaboratoryRuns({ api, scope, run, onBusy }: { api: PanelApi; scope: Scope; run?: RunSession; onBusy: (busy: boolean) => void }) {
  const query = useInfiniteQuery({ queryKey: projectionKey('laboratory-runs', run?.scope ?? scope),
    initialPageParam: undefined as string | undefined,
    queryFn: ({ signal, pageParam }) => api.laboratoryRuns(run!.scope, signal, pageParam),
    getNextPageParam: (last) => last.complete || !last.next_cursor ? undefined : last.next_cursor,
    enabled: !!run,
    refetchInterval: (q) => q.state.data?.pages.some((p) => p.items.some((r) => (!terminal.has(r.status) || r.status === 'OUTCOME_UNKNOWN'))) ? 1500 : false,
  });
  const active = query.data?.pages.some((p) => p.items.some((r) => samePartition(r.lab_scope, scope) && !terminal.has(r.status))) ?? false;
  useEffect(() => { onBusy(active); }, [active, onBusy]);
  return <Paper withBorder p="md" radius="lg" aria-label="Задания лаборатории">
    <Title order={2} size="h4">Задания лаборатории</Title>
    <Text size="sm">Событие сохраняется как тестовое наблюдение. Оно не вызывает модель и не меняет канон.</Text>
    {query.isError && <Alert color="orange">{failureMessage(query.error)}</Alert>}
    {(query.data?.pages.flatMap((p) => p.items) ?? []).filter((row) => samePartition(row.lab_scope, scope)).map((row) =>
      <Job key={row.operation_id} api={api} row={row} current={!!run && run.scope.run_id === row.scope.run_id && run.state === 'ACTIVE'} />)}
    <Button mt="xs" variant="subtle" onClick={() => void query.refetch()}>Обновить задания</Button>
    {query.hasNextPage && <Button mt="xs" variant="subtle" onClick={() => void query.fetchNextPage()}>Загрузить ещё задания</Button>}
  </Paper>;
}
function Job({ api, row, current }: { api: PanelApi; row: LaboratoryRun; current: boolean }) {
  const cache = useQueryClient();
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [unknown, setUnknown] = useState(false);
  const [message, setMessage] = useState('');
  async function send(command: EditCommand) {
    setBusy(true); setError(null); setMessage('');
    try {
      await api.command(command);
      setMessage(command.type === 'laboratory.event' ? 'Наблюдение сохранено в тестовой памяти.' : 'Отмена запрошена. Дождитесь фактического состояния задания.');
      setText('');
    } catch (e) {
      setError(failureMessage(e));
      setUnknown(!(e instanceof ApiFailure) || ['NETWORK', 'OUTCOME_UNKNOWN', 'SERVICE_UNAVAILABLE'].includes(e.code));
    }
    finally {
      setBusy(false);
      void cache.invalidateQueries({ queryKey: projectionKey('laboratory-runs', row.scope) });
      void cache.invalidateQueries({ queryKey: projectionKey('laboratory-reports', row.lab_scope) });
    }
  }
  return <Paper withBorder p="xs" mt="sm" aria-label="Лабораторное задание">
    <Text fw={600}>{row.scenario_id}</Text><Text size="sm" role="status">Состояние задания: {row.status}</Text>
    {row.virtual_time && <Text size="xs">Виртуальное время: {row.virtual_time}</Text>}
    {row.accepted_events !== null && <Text size="xs">Принято событий: {row.accepted_events} из 32</Text>}
    {row.status === 'OUTCOME_UNKNOWN' && <Alert color="orange">Исполнитель ещё не подтвердил завершение. Это не подтверждение остановки.</Alert>}
    {!terminal.has(row.status) && <Stack gap="xs" mt="xs">
      <Textarea label="Текст события" maxLength={2000} value={text} onChange={(e) => setText(e.currentTarget.value)} />
      <Group><Button variant="light" disabled={busy || unknown || !current || !row.ready || !text.trim() || row.accepted_events === 32}
        onClick={() => void send(eventLaboratory(row, text, crypto.randomUUID()))}>Подать событие</Button>
      <Button color="red" variant="light" disabled={busy || !current}
        onClick={() => void send(stopLaboratory(row, crypto.randomUUID()))}>Остановить задание</Button></Group>
    </Stack>}
    {message && <Alert color="teal" mt="xs">{message}</Alert>}
    {error && <Alert color="orange" mt="xs">{error}</Alert>}
    {unknown && <Text size="sm">Исход неизвестен. Обновите задания; автоматического повтора команды нет.</Text>}
  </Paper>;
}
