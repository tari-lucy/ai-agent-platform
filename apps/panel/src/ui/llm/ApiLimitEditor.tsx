import { Alert, Button, Group, Paper, Stack, Text, TextInput, Title } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import { failureMessage, type PanelApi } from '../../shared/api.ts';
import type { EditCommand, RunSession, Scope } from '../../shared/contracts.ts';
import { projectionKey } from '../../shared/scope.ts';
import { useSettingCommand } from './useSettingCommand.ts';

export function ApiLimitEditor({ api, scope, run }: { api: PanelApi; scope: Scope; run: RunSession | undefined }) {
  const cache = useQueryClient();
  const enabled = scope.memory_mode === 'work';
  const query = useQuery({ queryKey: projectionKey('api-limits', scope), queryFn: ({ signal }) => api.apiLimits(scope, signal), enabled });
  const [draft, setDraft] = useState<string | null>(null);
  const refresh = () => { for (const resource of ['api-limits', 'private-api-status']) void cache.invalidateQueries({ queryKey: projectionKey(resource, scope) }); };
  const action = useSettingCommand(api, refresh);
  const current = query.data;
  const value = draft ?? String(current?.attempt_limit ?? '');
  const count = Number(value);
  const valid = current && /^\d+$/.test(value) && Number.isSafeInteger(count) && count >= current.minimum_limit && count <= 1_000_000 && count !== current.attempt_limit;
  async function save() {
    if (action.pending) { await action.send(action.pending); return; }
    if (!current || !valid || run?.state !== 'STOPPED' || query.isFetching || query.isError) return;
    const command: EditCommand = { type: 'llm.limits', request_id: crypto.randomUUID(), scope, target: 'api-limits',
      expected_version: current.version, arguments: { schema_id: 'llm.limits-args', data: { attempt_limit: count } } };
    if (await action.send(command)) setDraft(null);
  }
  return <Paper withBorder p="md" radius="lg" aria-label="Лимит API"><Stack gap="xs">
    <Title order={3} size="h5">Лимит API</Title>
    <Text size="sm">Лимит общий для диалога, памяти и GM. Изменение не обнуляет уже использованные запросы.</Text>
    {!enabled ? <Text size="sm">Лимит рабочего агента меняется в WORK.</Text> : query.isError && !current ? <Alert color="orange">{failureMessage(query.error)}</Alert> : !current ? <Text size="sm">Загружаем лимит…</Text> : <>
      <Text size="sm">Сейчас: {current.attempt_limit} {current.period === 'utc_day' ? 'в сутки, обновление в 00:00 UTC' : 'за всё время'}. Минимум с учётом резервов: {current.minimum_limit}.</Text>
      <TextInput label="Количество API-запросов" inputMode="numeric" value={value} disabled={!!action.pending || action.busy} onChange={(e) => setDraft(e.currentTarget.value.trim())} />
      <Text size="xs" c="dimmed">Для изменения сначала остановите агента. Это предел числа вызовов; денежный лимит задаётся у провайдера.</Text>
      <Group justify="flex-end"><Button disabled={action.busy || (!action.pending && (!valid || run?.state !== 'STOPPED' || query.isFetching))} onClick={() => void save()}>
        {action.pending ? 'Проверить сохранение лимита' : 'Сохранить лимит'}</Button></Group>
    </>}
    {action.problem && <Alert color="orange" role="alert">{action.problem}</Alert>}
    {action.saved && <Alert color="teal" role="status">Лимит сохранён. Расход не сброшен.</Alert>}
  </Stack></Paper>;
}
