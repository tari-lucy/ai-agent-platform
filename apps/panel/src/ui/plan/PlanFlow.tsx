import { formatTime } from '../time.ts';
import { Alert, Button, Checkbox, Group, Paper, Stack, Text, TextInput } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { OwnerOperation, OperationProjection, ConsequencePlan, ExecutePlanCommand, PlanCommand, Receipt, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { afterExecute, progress, type PlanPhase } from './plan.ts';


// One flow for every operation with consequences: plan first, explicit confirmation, one command,
// an unknown outcome is checked with the identical command, progress comes from the owner.
export type PlanSpec = {
  ownerOperation?: boolean;
  download?: (operationId: string) => Promise<Blob>;
  hideRelated?: boolean;
  label: string; previewLabel: string; executeLabel: string; danger?: boolean;
  confirmText?: string;      // checkbox that must be ticked
  typedConfirm?: string;     // text the operator must type exactly
  note?: string;
  doneNote?: string;         // shown once the owner has accepted the command
  blocked?: string | null;   // why preparing is closed right now
  preview: (requestId: string) => PlanCommand;
  execute: (plan: ConsequencePlan, requestId: string) => ExecutePlanCommand;
  problem: (plan: ConsequencePlan, now: number) => string | null;
  related: (plan: ConsequencePlan) => number;
  refresh: string[];         // projections to reread afterwards
};

export function PlanFlow({ api, scope, spec }: { api: PanelApi; scope: Scope; spec: PlanSpec }) {
  const cache = useQueryClient();
  const [state, setState] = useState<PlanPhase>({ phase: 'idle' });
  const [understood, setUnderstood] = useState(false);
  const [typed, setTyped] = useState('');
  const [downloading, setDownloading] = useState(false);
  const [downloadProblem, setDownloadProblem] = useState<string | null>(null);
  const [opened] = useState(() => Date.now()); // render stays pure; the click checks the term again
  const operationId = state.phase === 'running' ? (state.receipt.operation_id ?? state.receipt.resource_id ?? null) : null;
  const status = useQuery<OwnerOperation | OperationProjection>({
    queryKey: [...projectionKey('operations', scope), 'one', operationId], enabled: operationId !== null,
    queryFn: ({ signal }) => spec.ownerOperation ? api.ownerOperation(scope, operationId!, signal) : api.operation(scope, operationId!, signal),
  });
  const problem = state.phase === 'ready' ? spec.problem(state.plan, opened) : null;
  const confirmed = (!spec.confirmText || understood) && (!spec.typedConfirm || typed === spec.typedConfirm);
  async function preview() {
    setState({ phase: 'previewing' }); setUnderstood(false); setTyped('');
    try {
      const receipt = await api.command(spec.preview(crypto.randomUUID())) as Receipt;
      if (!receipt.resource_id) throw new ApiFailure('INVALID_ARGUMENTS');
      setState({ phase: 'ready', plan: await api.plan(scope, receipt.resource_id) });
    } catch (error) { setState({ phase: 'refused', code: error instanceof ApiFailure ? error.code : 'NETWORK' }); }
  }
  async function execute(plan: ConsequencePlan, again?: ExecutePlanCommand) {
    if (!again && spec.problem(plan, Date.now())) { setState({ phase: 'refused', code: 'EXPIRED' }); return; }
    const command = again ?? spec.execute(plan, crypto.randomUUID());
    setState({ phase: 'executing', plan, command });
    try {
      setState(afterExecute(plan, command, { receipt: await api.command(command) as Receipt }));
    } catch (error) {
      setState(afterExecute(plan, command, { code: error instanceof ApiFailure ? error.code : 'NETWORK' }));
    } finally {
      for (const resource of [...spec.refresh, 'operations']) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) });
    }
  }
  const view = progress(status.data ?? null, spec.label);
  async function download() {
    if (!spec.download || !operationId) return;
    setDownloading(true); setDownloadProblem(null);
    try {
      const blob = await spec.download(operationId);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a'); link.href = url; link.download = 'memory-source.json';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 0);
    } catch (error) { setDownloadProblem(failureMessage(error)); }
    finally { setDownloading(false); }
  }
  return <Stack gap="xs" mt="sm">
    {(state.phase === 'idle' || state.phase === 'refused' || state.phase === 'previewing') &&
      <><Button variant="light" disabled={state.phase === 'previewing' || !!spec.blocked} onClick={() => void preview()}>
        {state.phase === 'previewing' ? 'Готовим план…' : spec.previewLabel}</Button>
        {spec.blocked && <Text size="xs" c="orange.8" role="status">{spec.blocked}</Text>}</>}
    {state.phase === 'refused' && <Alert color="orange" role="alert">{failureMessage(new ApiFailure(state.code))} Операция не подтверждена. Проверьте текущее состояние.</Alert>}
    {(state.phase === 'ready' || state.phase === 'executing' || state.phase === 'unknown') && <Paper withBorder p="sm" aria-label="План последствий">
      <Text fw={600}>План: {spec.label}</Text>
      <Text className="plain-text" size="sm" mt={4}>{state.plan.consequences}</Text>
      <Text size="sm" mt={4}>{!spec.hideRelated && <>Связанных записей: {spec.related(state.plan)} · </>}действует до {formatTime(state.plan.expires_at, 'short')}</Text>
      {spec.note && <Text size="xs" c="dimmed">{spec.note}</Text>}
      {problem && <Alert color="orange" mt="xs" role="alert">{problem}</Alert>}
      {state.phase === 'ready' && <>
        {spec.confirmText && <Checkbox mt="xs" label={spec.confirmText} checked={understood} onChange={(event) => setUnderstood(event.currentTarget.checked)} />}
        {spec.typedConfirm && <TextInput mt="xs" label={`Введите «${spec.typedConfirm}» для подтверждения`} value={typed}
          onChange={(event) => setTyped(event.currentTarget.value)} />}
        <Group mt="xs"><Button color={spec.danger ? 'red' : 'blue'} disabled={!!problem || !confirmed} onClick={() => void execute(state.plan)}>{spec.executeLabel}</Button>
          <Button variant="subtle" color="gray" onClick={() => setState({ phase: 'idle' })}>Отмена</Button></Group></>}
      {state.phase === 'executing' && <Text size="sm" role="status" mt="xs">Отправляем команду…</Text>}
      {state.phase === 'unknown' && <Alert color="orange" mt="xs" role="alert" title="Результат неизвестен">
        Команда могла быть выполнена. Проверка повторяет ту же команду: ядро вернёт сохранённую квитанцию, второго выполнения не будет.
        <Button mt="xs" size="compact-sm" variant="light" onClick={() => void execute(state.plan, state.command)}>Проверить исход</Button></Alert>}
    </Paper>}
    {state.phase === 'running' && <Paper withBorder p="sm" role="status" aria-label="Ход выполнения">
      <Text fw={600}>{spec.label}: {view.text}</Text>
      {!!view.done.length && <Text size="sm">Выполнено: {view.done.join(', ')}</Text>}
      {!!view.pending.length && <Text size="sm">Ожидает: {view.pending.join(', ')}</Text>}
      {spec.doneNote && <Text size="sm" c="orange.8">{spec.doneNote}</Text>}
      {spec.download && status.data && 'state' in status.data && status.data.state === 'completed' &&
        <Button mt="xs" size="compact-sm" disabled={downloading} onClick={() => void download()}>Скачать JSON</Button>}
      {downloadProblem && <Alert color="orange" role="alert">{downloadProblem}</Alert>}
      <Button mt="xs" size="compact-sm" variant="subtle" onClick={() => void status.refetch()}>Обновить статус</Button>
      {spec.ownerOperation && status.data && 'state' in status.data && status.data.state !== 'completed' &&
        <Button mt="xs" ml="sm" size="compact-sm" variant="light" onClick={() => void execute(state.plan, state.command)}>Проверить исход</Button>}
    </Paper>}
  </Stack>;
}
