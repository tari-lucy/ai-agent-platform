import { Alert, Button, Group, Select, Stack, Text, Textarea } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { PersonaTextEntry, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';

const groups = [
  { id: 'rules', title: 'Правила оператора' }, { id: 'plans', title: 'Цели и планы' },
  { id: 'guides', title: 'Инструкции (playbook)' }, { id: 'servers', title: 'Серверы' },
] as const;

function Editor({ api, scope, entry }: { api: PanelApi; scope: Scope; entry: PersonaTextEntry }) {
  const cache = useQueryClient();
  const key = [...projectionKey('persona-text', scope), entry.id];
  const query = useQuery({ queryKey: key, queryFn: ({ signal }) => api.personaText(scope, entry.id, signal), refetchOnWindowFocus: false, staleTime: Infinity });
  // The draft is the operator's own text; a re-read never replaces it silently.
  const [draft, setDraft] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ tone: 'ok' | 'bad' | 'conflict'; text: string } | null>(null);
  const data = query.data;
  if (query.isPending) return <Text size="sm" c="dimmed">Открываем…</Text>;
  if (!data) return <Alert color="orange">{failureMessage(query.error)}</Alert>;
  const text = draft ?? data.content;
  const dirty = draft !== null && draft !== data.content;

  async function save() {
    if (!data || draft === null) return;
    setBusy(true); setMessage(null);
    try {
      const saved = await api.savePersonaText(scope, entry.id, draft, data.version);
      cache.setQueryData(key, saved);
      void cache.invalidateQueries({ queryKey: [...projectionKey('persona-texts', scope)] });
      setDraft(null); setMessage({ tone: 'ok', text: `Сохранено. ${saved.effect}` });
    } catch (error) {
      const code = error instanceof ApiFailure ? error.code : '';
      if (code === 'VERSION_CONFLICT') setMessage({ tone: 'conflict', text: 'Файл изменился, пока вы правили: возможно, агент дописала в него сама. Ваш текст остался в поле и не потерян.' });
      else setMessage({ tone: 'bad', text: failureMessage(error) + ' Если сомневаетесь, нажмите «Загрузить из файла» и проверьте текст.' });
    } finally { setBusy(false); }
  }
  async function reload() {
    setBusy(true);
    try { const fresh = await api.personaText(scope, entry.id); cache.setQueryData(key, fresh); setDraft(null); setMessage(null); }
    catch (error) { setMessage({ tone: 'bad', text: failureMessage(error) }); } finally { setBusy(false); }
  }
  async function bring(rev: string | null) {
    if (!rev) return;
    try { const old = await api.personaTextRevision(scope, rev); setDraft(old.content); setMessage({ tone: 'ok', text: 'Старая версия подставлена в поле. Нажмите «Сохранить», чтобы вернуть её.' }); }
    catch (error) { setMessage({ tone: 'bad', text: failureMessage(error) }); }
  }
  return <Stack gap="xs">
    <Group justify="space-between" align="baseline">
      <Text fw={600}>{data.title}</Text>
      <Text size="xs" c="dimmed">{data.version === 'new' ? 'файла ещё нет' : `изменён ${formatTime(data.updated_at, 'short')}`}</Text>
    </Group>
    <Text size="xs" c="dimmed">{data.effect}</Text>
    <Textarea aria-label={data.title} autosize minRows={14} maxRows={32} value={text} spellCheck={false}
      styles={{ input: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', fontSize: 13 } }}
      onChange={(event) => { setDraft(event.currentTarget.value); setMessage(null); }} />
    <Group justify="space-between" wrap="wrap">
      <Group gap="xs">
        <Button onClick={() => void save()} loading={busy} disabled={!dirty}>Сохранить</Button>
        <Button variant="subtle" color="gray" disabled={busy || (!dirty && !message)} onClick={() => void reload()}>Загрузить из файла</Button>
      </Group>
      {!!data.history?.length && <Select size="xs" w={260} placeholder={`Прежние версии (${data.history.length})`} value={null} clearable={false}
        data={data.history.map((item) => ({ value: item.rev, label: `${formatTime(item.at, 'short')} · ${item.size} Б` }))}
        onChange={(value) => void bring(value)} aria-label="Прежние версии" />}
    </Group>
    {message && <Alert color={message.tone === 'ok' ? 'teal' : message.tone === 'conflict' ? 'orange' : 'red'} role={message.tone === 'ok' ? 'status' : 'alert'}>{message.text}
      {message.tone === 'conflict' && <Button size="compact-xs" variant="light" ml="xs" onClick={() => void reload()}>Загрузить свежую версию (мои правки заменятся)</Button>}</Alert>}
  </Stack>;
}

export function TextsTab({ api, scope }: { api: PanelApi; scope: Scope }) {
  const list = useQuery({ queryKey: [...projectionKey('persona-texts', scope)], queryFn: ({ signal }) => api.personaTexts(scope, signal) });
  const [selected, setSelected] = useState<string | null>(null);
  const items = list.data?.items ?? [];
  const entry = items.find((item) => item.id === selected) ?? null;
  if (list.isPending) return <Text size="sm">Получаем список…</Text>;
  if (list.isError) return <Alert color="orange">{failureMessage(list.error)}</Alert>;
  if (!list.data?.enabled) return <Alert color="gray">Тексты доступны, когда агент настроен и запущен хотя бы один раз.</Alert>;
  return <Group align="flex-start" grow wrap="wrap" gap="lg">
    <Stack gap="md" style={{ flex: '1 1 16rem', maxWidth: 320 }}>
      <Text size="sm" c="dimmed">Здесь правятся тексты, по которым она живёт. Что именно изменится и когда, написано над полем.</Text>
      {groups.map((group) => {
        const rows = items.filter((item) => item.group === group.id);
        return rows.length ? <div key={group.id}>
          <Text size="xs" c="dimmed" mb={4}>{group.title}</Text>
          <Stack gap={2}>{rows.map((row) => <button key={row.id} type="button" className={`memory-item ${row.id === selected ? 'selected' : ''}`} onClick={() => setSelected(row.id)}>
            <Text size="sm" fw={row.id === selected ? 600 : 400}>{row.title}</Text>
            <Text size="xs" c="dimmed">{row.updated_at ? formatTime(row.updated_at, 'short') : 'нет файла'}{!row.writable ? ' · слишком большой' : ''}</Text>
          </button>)}</Stack>
        </div> : null;
      })}
    </Stack>
    <div style={{ flex: '3 1 28rem' }}>
      {entry ? (entry.writable || entry.size === 0 ? <Editor key={entry.id} api={api} scope={scope} entry={entry} /> :
        <Alert color="orange">Файл больше 64 КБ: править его в панели нельзя, откройте его в OpenCode.</Alert>) :
        <Alert color="gray">Выберите текст слева.</Alert>}
    </div>
  </Group>;
}
