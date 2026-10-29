import { Alert, Button, Stack, Text } from '@mantine/core';
import { useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ControlCommand, Receipt, RunSession } from '../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../shared/api.ts';
import { samePartition } from '../shared/scope.ts';
import { controlCommand } from './workspace/model.ts';

type Attempt = { command: ControlCommand; phase: 'sending' | 'waiting' | 'unknown' };
type Props = {
  api: PanelApi; run: RunSession | null; state: RunSession['state'] | undefined;
  ready: boolean; stopBusy: boolean; stopError: string | null; onStop: () => Promise<void>;
};

// START needs a fresh confirmed stopped owner. STOP keeps its independent direct HTTP path,
// including while a start request or unrelated reads are blocked. No optimistic ACTIVE state.
export function RunPowerControl({ api, run, state, ready, stopBusy, stopError, onStop }: Props) {
  const cache = useQueryClient();
  const [attempt, setAttempt] = useState<Attempt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const unresolved = !!attempt && (!run || !samePartition(run.scope, attempt.command.scope) ||
    (run.run_id === attempt.command.scope.run_id && run.version <= (attempt.command.expected_version ?? 0)));
  const showStart = state === 'STOPPED' && !unresolved && !stopBusy && !stopError;
  // STOP was accepted and the owner is removing the sandbox: nothing to press again.
  const stopping = state === 'STOPPING';
  const canStart = !!run && ready && run.state === 'STOPPED' && (run.unconfirmed_resource_ids?.length ?? 0) === 0;

  async function start(command: ControlCommand) {
    const token = ++generation.current;
    setAttempt({ command, phase: 'sending' }); setError(null);
    try {
      const result = await api.command(command) as Receipt;
      if (generation.current !== token) return;
      if (result.status === 'rejected') {
        setAttempt(null); setError('Ядро отклонило запуск. Проверьте состояние рабочего пространства.');
      } else setAttempt({ command, phase: 'waiting' });
    } catch (caught) {
      if (generation.current !== token) return;
      const code = caught instanceof ApiFailure ? caught.code : 'NETWORK';
      if (['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(code)) {
        setAttempt({ command, phase: 'unknown' });
      } else setAttempt(null);
      setError(failureMessage(caught));
    } finally { void cache.invalidateQueries({ queryKey: ['projection'] }); }
  }
  async function stop() {
    // A late start response must never hide STOP or reintroduce a pending start after STOP.
    generation.current++; setAttempt(null); setError(null);
    await onStop();
  }
  const unknown = unresolved && attempt?.phase === 'unknown';
  return <Stack gap="xs" align="flex-end">
    <Button className={showStart ? 'start-button' : 'stop-button'} color={showStart ? 'teal' : 'red'} size="md"
      aria-label={showStart ? 'START — запустить агента' : 'STOP — остановить активного агента'}
      title={showStart ? canStart ? 'Запустить общение с персонажем' : 'Обновляем подтверждённое состояние ядра' : 'Остановить агента'}
      disabled={showStart ? !canStart : !run || stopBusy || stopping}
      onClick={() => {
        if (showStart) {
          if (canStart && run) void start(controlCommand(run, 'agent.start', 'autonomous', crypto.randomUUID()));
        } else void stop();
      }}>{showStart ? 'START' : stopBusy || stopping ? 'Останавливаем…' : 'STOP'}</Button>
    {stopping && <Text size="xs" c="dimmed" role="status">STOP принят: убираем контейнер и шлюз, это несколько секунд.</Text>}
    {unresolved && !unknown && <Text size="xs" c="dimmed" role="status">Запускаем… STOP доступен.</Text>}
    {error && <Alert color="orange" title={unknown ? 'Результат запуска неизвестен' : 'Запуск не выполнен'} maw={360}>
      {error}
      {unknown && <Button mt="xs" variant="light" size="compact-sm"
        onClick={() => attempt && void start(attempt.command)}>Проверить исход запуска</Button>}
    </Alert>}
  </Stack>;
}
