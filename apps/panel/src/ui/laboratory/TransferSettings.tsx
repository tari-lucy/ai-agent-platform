import { Alert, Button, Checkbox, Group, Paper, Select, Stack, Table, Text, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import type { PanelApi } from '../../shared/api.ts';
import { failureMessage } from '../../shared/api.ts';
import type { RunSession, Scope, SettingsTransferOperation, SettingsTransferPreview } from '../../shared/contracts.ts';
import { acknowledgeSettingsTransfer, applySettingsTransfer, previewSettingsTransfer, type TransferKey } from '../../shared/owner-commands.ts';
import { projectionKey, samePartition, scopeKey, scopeLabel } from '../../shared/scope.ts';
import { NEVER_TRANSFERRED } from './model.ts';

const keys: TransferKey[] = ['profile', 'gm_intensity', 'story_rules_version'];
const labels = { profile: 'Профиль', gm_intensity: 'Интенсивность гейммастера', story_rules_version: 'Правила и их версия' };
const statuses = { planned: 'Исход требует сверки', completed: 'Перенос завершён', reconciliation_required: 'Перенос выполнен частично — нужна сверка', acknowledged: 'Сверка подтверждена; автоматического отката не было' };
const show = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
export function TransferSettings({ api, source, scopes, run }: { api: PanelApi; source: Scope; scopes: Scope[]; run?: RunSession }) {
  const targets = scopes.filter((s) => s.memory_mode === 'work' && s.workspace_id === source.workspace_id);
  const [selected, setSelected] = useState('');
  const target = targets.find((s) => scopeKey(s) === selected) ?? targets[0];
  return <Paper withBorder p="md" radius="lg" aria-label="Перенос настроек">
    <Title order={2} size="h4" mb="xs">Перенос настроек в рабочую область</Title>
    <Text size="sm">Переносятся только выбранные настройки — после проверки различий и подтверждения.</Text>
    <Text size="sm" mt="xs">Никогда не переносятся: {NEVER_TRANSFERRED.join(', ')}.</Text>
    {!target ? <Text c="orange.8">Нет доступной рабочей области назначения.</Text> : <>
      <Select mt="sm" label="Рабочая область назначения" allowDeselect={false} value={scopeKey(target)}
        data={targets.map((s) => ({ value: scopeKey(s), label: scopeLabel(s) }))} onChange={(v) => { if (v) setSelected(v); }} />
      <TransferForm key={scopeKey(source) + scopeKey(target)} api={api} source={source} target={target} run={run} />
    </>}
  </Paper>;
}
function TransferForm({ api, source, target, run }: { api: PanelApi; source: Scope; target: Scope; run?: RunSession }) {
  const cache = useQueryClient();
  const gm = useQuery({ queryKey: projectionKey('gm-settings', target), queryFn: ({ signal }) => api.gmSettings(target, signal) });
  const history = useInfiniteQuery({
    queryKey: projectionKey('settings-transfers', target), initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => api.settingsTransfers(target, signal, pageParam),
    getNextPageParam: (last) => last.complete || !last.next_cursor ? undefined : last.next_cursor,
  });
  const [chosen, setChosen] = useState<TransferKey[]>(['gm_intensity']);
  const [plan, setPlan] = useState<SettingsTransferPreview | null>(null);
  const [expired, setExpired] = useState(false);
  useEffect(() => {
    if (!plan) return;
    const timer = setTimeout(() => setExpired(true), Math.max(0, Date.parse(plan.expires_at) - Date.now()));
    return () => clearTimeout(timer);
  }, [plan]);
  const [confirmed, setConfirmed] = useState(false);
  const [reviewed, setReviewed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [operation, setOperation] = useState<string | null>(null);
  const [status, setStatus] = useState<SettingsTransferOperation | null>(null);
  const [unknown, setUnknown] = useState(false);
  const stopped = run?.state === 'STOPPED' && samePartition(run.scope, target);
  const state = gm.data?.items.find((r) => r.id === 'gm-runtime');
  async function inspect(id = operation) {
    if (!id) return;
    const result = await api.transferStatus(target, id);
    setStatus(result.record); setUnknown(result.record === null);
    for (const name of ['settings-transfers', 'gm-settings', 'personas', 'character-states']) {
      void cache.invalidateQueries({ queryKey: projectionKey(name, target) });
    }
  }
  async function preview() {
    if (!state || !chosen.length || busy) return;
    setBusy(true); setError(null); setPlan(null); setExpired(false); setConfirmed(false); setStatus(null); setUnknown(false);
    const id = crypto.randomUUID(); setOperation(id);
    try { setPlan(await api.previewSettingsTransfer(previewSettingsTransfer(target, source, chosen, state.version, id, crypto.randomUUID()))); }
    catch (e) { setError(failureMessage(e)); }
    finally { setBusy(false); }
  }
  async function apply() {
    if (!plan || !confirmed || plan.blocked || busy || !stopped || Date.parse(plan.expires_at) <= Date.now()) return;
    setBusy(true); setError(null);
    try { await api.command(applySettingsTransfer(plan, crypto.randomUUID())); await inspect(plan.operation_id); setPlan(null); setConfirmed(false); }
    catch (e) { setError(failureMessage(e)); setUnknown(true); }
    finally { setBusy(false); }
  }
  async function acknowledge() {
    if (!status || !reviewed || busy || !stopped) return;
    setBusy(true); setError(null);
    try { await api.command(acknowledgeSettingsTransfer(status, crypto.randomUUID())); await inspect(status.operation_id); setReviewed(false); }
    catch (e) { setError(failureMessage(e)); }
    finally { setBusy(false); }
  }
  async function refresh(id?: string) {
    setBusy(true); setError(null);
    if (id) setOperation(id);
    try { await inspect(id ?? operation); } catch (e) { setError(failureMessage(e)); }
    finally { setBusy(false); }
  }
  return <Stack gap="sm" mt="sm">
    <Group>{keys.map((key) => <Checkbox key={key} label={labels[key]} checked={chosen.includes(key)} disabled={busy}
      onChange={(e) => { const next = e.currentTarget.checked; setChosen(keys.filter((k) => k === key ? next : chosen.includes(k))); setPlan(null); setConfirmed(false); }} />)}</Group>
    {!stopped && <Alert color="orange">Для применения и подтверждения сверки остановите агента рабочей области.</Alert>}
    {gm.isError && <Alert color="orange">Настройки назначения недоступны: {failureMessage(gm.error)}</Alert>}
    <Text size="xs" c="dimmed">Для переноса нужен предпросмотр различий и блокировки совместимости. План действует пять минут.</Text>
    <Button variant="light" disabled={busy || unknown || !state || !chosen.length} onClick={() => void preview()}>Предпросмотр переноса</Button>
    {plan && <Paper withBorder p="xs" aria-label="Предпросмотр переноса">
      <Text size="sm">Из {scopeLabel(source)} в {scopeLabel(target)}.</Text>
      {plan.items.map((item) => <div key={item.key}><Text fw={600}>{labels[item.key]}</Text>
        <Table><Table.Thead><Table.Tr><Table.Th>До</Table.Th><Table.Th>После</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody><Table.Tr><Table.Td><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{show(item.before)}</pre></Table.Td>
            <Table.Td><pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{show(item.after)}</pre></Table.Td></Table.Tr></Table.Tbody></Table>
        <Text size="sm">{item.changes.join('; ') || 'Без изменений'}</Text>
        {!!item.blockers.length && <Alert color="red">{item.blockers.join('; ')}</Alert>}
      </div>)}
      <Text size="xs">Действует до {new Date(plan.expires_at).toLocaleTimeString('ru-RU')}.</Text>
    </Paper>}
    {plan && expired && <Alert color="orange">План истёк. Запросите новый предпросмотр.</Alert>}
    <Checkbox label="Подтверждаю перенос выбранных настроек в рабочую область" checked={confirmed} disabled={!plan || plan.blocked || busy || expired}
      onChange={(e) => setConfirmed(e.currentTarget.checked)} />
    <Button disabled={busy || unknown || status !== null || !plan || plan.blocked || !confirmed || !stopped || expired} onClick={() => void apply()}>Перенести настройки</Button>
    {error && <Alert color="orange" role="alert">{error}</Alert>}
    {unknown && <Alert color="orange">Исход неизвестен. Не запускайте новый перенос вслепую. Чтение состояния не повторяет применение.</Alert>}
    {operation && <Button variant="subtle" disabled={busy} onClick={() => void refresh()}>Проверить состояние переноса</Button>}
    {status && <Alert color={status.status === 'completed' ? 'teal' : 'orange'} role="status" aria-label="Состояние переноса">
      {statuses[status.status]}<Text size="sm">Журнал подтвердил: {(status.applied_keys ?? []).map((k) => labels[k]).join(', ') || 'нет записанных подтверждений; это не доказывает отсутствие изменений'}.</Text>
    </Alert>}
    {status && ['planned', 'reconciliation_required'].includes(status.status) && <>
      <Checkbox label="Я сверила фактическое состояние; подтверждение не выполняет откат" checked={reviewed} onChange={(e) => setReviewed(e.currentTarget.checked)} />
      <Button color="orange" disabled={busy || !reviewed || !stopped || !status.operation_id} onClick={() => void acknowledge()}>Подтвердить сверку переноса</Button>
    </>}
    {history.isError && <Text c="orange.8">История недоступна: {failureMessage(history.error)}</Text>}
    {(history.data?.pages.flatMap((p) => p.items) ?? []).filter((r) => samePartition(r.source_scope, source)).map((r) =>
      <Group key={r.id}><Text size="sm">{statuses[r.status]}</Text><Button size="compact-xs" variant="subtle" disabled={busy || !r.operation_id}
        onClick={() => { setReviewed(false); void refresh(r.operation_id ?? undefined); }}>Открыть состояние</Button></Group>)}
    {history.hasNextPage && <Button variant="subtle" onClick={() => void history.fetchNextPage()}>Загрузить ещё переносы</Button>}
  </Stack>;
}
