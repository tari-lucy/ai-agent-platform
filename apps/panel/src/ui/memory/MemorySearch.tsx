import { Alert, Badge, Button, Group, Paper, Stack, Text, TextInput, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type { PanelApi } from '../../shared/api.ts';
import { failureMessage } from '../../shared/api.ts';
import type { Scope } from '../../shared/contracts.ts';
import { projectionKey } from '../../shared/scope.ts';

const states = {
  ready: 'Индекс готов.', lagging: 'Индекс отстаёт: часть памяти могла не попасть в результаты.',
  degraded: 'Поиск по словам. Семантическая модель не подключена.',
  unavailable: 'Поиск сейчас недоступен. Загруженные записи можно просматривать ниже.',
};
export function MemorySearch({ api, scope, onSelect }: { api: PanelApi; scope: Scope; onSelect: (id: string) => void }) {
  const [text, setText] = useState('');
  const [query, setQuery] = useState('');
  const result = useQuery({
    queryKey: [...projectionKey('sources', scope), 'recall', query],
    queryFn: ({ signal }) => api.recall(scope, query, signal), enabled: query !== '', gcTime: 0,
  });
  function submit() {
    const value = text.trim();
    if (!value) return;
    if (query === value) void result.refetch(); else setQuery(value);
  }
  return <Paper withBorder p="md" radius="lg" aria-label="Поиск по памяти">
    <Title order={2} size="h4">Найти в памяти</Title>
    <Text size="sm" c="dimmed" mb="sm">Ищет в серверном индексе доступных источников, включая старые разговоры. Модель и платный API не используются.</Text>
    <form onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <Group align="end"><TextInput label="Что найти" value={text} maxLength={1000} style={{ flex: 1 }}
        onChange={(e) => setText(e.currentTarget.value)} placeholder="Слово или фраза из разговора" />
        <Button type="submit" disabled={!text.trim() || result.isFetching}>Найти в памяти</Button>
        {query && <Button variant="subtle" onClick={() => { setQuery(''); setText(''); }}>Очистить поиск</Button>}
      </Group>
    </form>
    {result.isFetching ? <Text size="sm" mt="sm" role="status">Ищем…</Text> : result.isError ?
      <Alert color="orange" mt="sm">{failureMessage(result.error)} Результаты этого запроса не показаны.</Alert> : query && result.data &&
      <Stack gap="xs" mt="sm">
        <Text size="sm" fw={500}>Результаты запроса «{query}»</Text>
        <Text size="sm" role="status">{states[result.data.index_state]}</Text>
        {result.data.index_state !== 'unavailable' && !result.data.fragments.length && <Text size="sm">Доступных совпадений не найдено. Попробуйте другое слово или проверьте загруженные записи.</Text>}
        {!!result.data.fragments.length && <Text size="xs" c="dimmed">Релевантные фрагменты: {result.data.fragments.length}. Это ограниченная выдача, а не перечень всех совпадений. Фильтры списка ниже к ней не применяются.</Text>}
        {result.data.fragments.map((fragment, index) => <Paper key={fragment.source.source_id + ':' + index} withBorder p="sm">
          <Text className="plain-text" size="sm">{fragment.content}</Text>
          <Text size="xs" c="dimmed" mt={4}>{fragment.attribution}</Text>
          {!!fragment.conflicts?.length && <Badge mt={4} color="orange" variant="light">Есть связанные противоречия</Badge>}
          <Group justify="flex-end"><Button size="compact-xs" variant="light" onClick={() => onSelect(fragment.source.source_id)}>Открыть источник</Button></Group>
        </Paper>)}
      </Stack>}
  </Paper>;
}
