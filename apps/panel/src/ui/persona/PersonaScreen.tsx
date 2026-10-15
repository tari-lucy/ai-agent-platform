import { formatTime } from '../time.ts';
import { Alert, Badge, Button, Group, Paper, SegmentedControl, Stack, Table, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { Profile, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import {
  applyBlocker, applyCommand, changedLines, checkedFrom, draftProblems, emptyNew, hasProblems, lineDiff, newCommand, newProblem, propertyRows, stateNote,
  validateCommand, type Checked, type Draft, type NewPersona,
} from './model.ts';


function Diff({ title, before, after }: { title: string; before: string; after: string }) {
  const lines = lineDiff(before, after);
  const changed = changedLines(lines);
  return <div><Text size="sm" fw={600}>{title}: {changed ? `изменено строк — ${changed}` : 'без изменений'}</Text>
    {changed > 0 && <pre className="diff" aria-label={`Различия: ${title}`}>{lines.filter((l) => l.op !== 'same').map((line, index) =>
      <div key={index} className={line.op}>{line.op === 'add' ? '+ ' : '− '}{line.text}</div>)}</pre>}</div>;
}

function Editor({ api, scope, profile, onReload }: { api: PanelApi; scope: Scope; profile: Profile; onReload: () => void }) {
  const [draft, setDraft] = useState<Draft>({ canon: profile.canon, style: profile.style });
  const [checked, setChecked] = useState<Checked | null>(null);
  const [busy, setBusy] = useState<'validate' | 'apply' | null>(null);
  const [message, setMessage] = useState<{ tone: 'ok' | 'bad'; text: string } | null>(null);
  const [unknown, setUnknown] = useState<'validate' | 'apply' | null>(null);
  const [conflict, setConflict] = useState(false);
  const problems = draftProblems(profile, draft);
  const blocker = applyBlocker(profile, draft, checked);
  const edit = (patch: Partial<Draft>) => { setDraft({ ...draft, ...patch }); setMessage(null); };
  async function validate() {
    if (hasProblems(problems)) return;
    setBusy('validate'); setMessage(null); setConflict(false); setUnknown(null);
    try {
      const report = await api.validateProfile(validateCommand(scope, profile, draft, crypto.randomUUID()));
      setChecked(checkedFrom(profile, draft, report));
      if (report.version_conflict) { setConflict(true); setMessage({ tone: 'bad', text: 'Профиль уже изменён. Загрузите актуальную редакцию.' }); }
      else setMessage({ tone: report.ready ? 'ok' : 'bad', text: report.ready ? 'Правка проверена. Различия показаны ниже; профиль ещё не изменён.' : 'Правка не готова: ' + [...report.issues, ...report.blockers].map((i) => i.message).join(' ') });
    } catch (error) { fail(error, 'validate'); }
    finally { setBusy(null); }
  }
  async function apply() {
    if (!checked?.digest) return;
    setBusy('apply'); setMessage(null); setUnknown(null);
    try {
      await api.command(applyCommand(scope, profile, draft, checked.digest, crypto.randomUUID()));
      setMessage({ tone: 'ok', text: 'Версия принята ядром. Накопленное состояние сохраняется, канон и состояние остаются раздельными.' }); setChecked(null); onReload();
    } catch (error) { fail(error, 'apply'); }
    finally { setBusy(null); }
  }
  function fail(error: unknown, step: 'validate' | 'apply') {
    const code = error instanceof ApiFailure ? error.code : 'NETWORK';
    if (code === 'VERSION_CONFLICT') { setConflict(true); setMessage({ tone: 'bad', text: 'Профиль уже изменён. Загрузите актуальную редакцию и пересмотрите правку: автоматического перезаписывания нет.' }); return; }
    if (['NETWORK', 'OUTCOME_UNKNOWN', 'STALE_CONTEXT'].includes(code)) { setUnknown(step); setMessage({ tone: 'bad', text: 'Результат неизвестен. Обновите профиль: версия покажет, применена ли правка. Повтор вслепую не делается.' }); return; }
    setMessage({ tone: 'bad', text: failureMessage(new ApiFailure(code)) + ' Ничего не изменено.' });
  }
  return <Paper withBorder p="md" radius="lg" aria-label="Редактор профиля"><Title order={2} size="h4" mb="xs">Редактор профиля (v{profile.version})</Title>
    <Textarea label="Канон" autosize minRows={4} maxRows={14} value={draft.canon} error={problems.canon} onChange={(e) => edit({ canon: e.currentTarget.value })} />
    <Textarea label="Стиль" mt="xs" autosize minRows={3} maxRows={10} value={draft.style} error={problems.style} onChange={(e) => edit({ style: e.currentTarget.value })} />
    {problems.general && <Text size="xs" c="dimmed" mt="xs">{problems.general}</Text>}
    <Stack gap="xs" mt="sm" aria-label="Различия до применения"><Diff title="Канон" before={profile.canon} after={draft.canon} /><Diff title="Стиль" before={profile.style} after={draft.style} /></Stack>
    <Group mt="sm">
      <Button variant="light" disabled={hasProblems(problems) || busy !== null} onClick={() => void validate()}>{busy === 'validate' ? 'Проверяем…' : 'Проверить правку'}</Button>
      <Button disabled={!!blocker || busy !== null} onClick={() => void apply()}>{busy === 'apply' ? 'Применяем…' : 'Применить версию'}</Button>
      <Button variant="subtle" color="gray" onClick={() => { setDraft({ canon: profile.canon, style: profile.style }); setChecked(null); setMessage(null); }}>Сбросить черновик</Button></Group>
    {blocker && <Text size="xs" c="dimmed" mt="xs" role="status">{blocker}</Text>}
    {message && <Alert mt="xs" color={message.tone === 'ok' ? 'teal' : 'orange'} role={message.tone === 'ok' ? 'status' : 'alert'}>{message.text}</Alert>}
    {(conflict || unknown) && <Button mt="xs" size="compact-sm" variant="light" onClick={onReload}>Загрузить актуальную редакцию</Button>}
    <Text size="xs" c="dimmed" mt="xs">Текст профиля — данные: он не исполняется. Свойства, пределы развития и начальные значения меняются через импорт профиля.</Text>
  </Paper>;
}

function NewProfile({ api, scope, source, existing }: { api: PanelApi; scope: Scope; source: Profile | undefined; existing: string[] }) {
  const cache = useQueryClient();
  const [input, setInput] = useState<NewPersona>(emptyNew);
  const [problem, setProblem] = useState<string | null>(null);
  const [state, setState] = useState<'idle' | 'sending' | 'done'>('idle');
  const set = (patch: Partial<NewPersona>) => setInput({ ...input, ...patch });
  async function submit() {
    const invalid = newProblem(input, existing) ?? (input.mode === 'copy' && !source ? 'Нет принятого профиля для копирования.' : null);
    if (invalid) { setProblem(invalid); return; }
    setProblem(null); setState('sending');
    try { await api.command(newCommand(scope, source, input, crypto.randomUUID())); setState('done'); setInput(emptyNew); }
    catch (error) { setProblem(failureMessage(error) + ' Ничего не создано.'); setState('idle'); }
    finally { void cache.invalidateQueries({ queryKey: projectionKey('personas', scope) }); }
  }
  return <Paper withBorder p="md" radius="lg" aria-label="Новая личность"><Title order={2} size="h4" mb="xs">Новая личность</Title>
    <SegmentedControl value={input.mode} onChange={(v) => set({ mode: v as NewPersona['mode'] })} data={[{ value: 'copy', label: 'Копия профиля' }, { value: 'create', label: 'С нуля' }]} />
    <TextInput mt="xs" label="Идентификатор личности" value={input.persona} onChange={(e) => set({ persona: e.currentTarget.value.trim() })} />
    <TextInput mt="xs" label="Область памяти новой личности" value={input.space} onChange={(e) => set({ space: e.currentTarget.value.trim() })} />
    {input.mode === 'create' && <><Textarea mt="xs" label="Канон" autosize minRows={2} value={input.canon} onChange={(e) => set({ canon: e.currentTarget.value })} />
      <Textarea mt="xs" label="Стиль" autosize minRows={2} value={input.style} onChange={(e) => set({ style: e.currentTarget.value })} /></>}
    <Text size="xs" c="dimmed" mt="xs">{input.mode === 'copy' ? 'Копируется только принятый канон. Память, состояние и отношения не копируются: у новой личности чистая биография. Перенос опыта — отдельный импорт с происхождением.' : 'Новая личность получает собственное состояние и память.'}</Text>
    <Group mt="xs" justify="flex-end"><Button variant="light" disabled={state === 'sending'} onClick={() => void submit()}>{input.mode === 'copy' ? 'Создать копию' : 'Создать личность'}</Button></Group>
    {problem && <Alert color="orange" mt="xs" role="alert">{problem}</Alert>}
    {state === 'done' && <Alert color="teal" mt="xs" role="status">Команда принята ядром. Новая личность не запускается автоматически.</Alert>}
  </Paper>;
}

export function PersonaScreen({ api, scope }: { api: PanelApi; scope: Scope }) {
  const cache = useQueryClient();
  const profiles = useQuery({ queryKey: projectionKey('personas', scope), queryFn: ({ signal }) => api.profiles(scope, signal) });
  const states = useQuery({ queryKey: projectionKey('character-states', scope), queryFn: ({ signal }) => api.characterStates(scope, signal) });
  const profile = profiles.data?.items.find((item) => item.persona_id === scope.persona_id) ?? profiles.data?.items[0];
  const state = states.data?.items.find((item) => item.persona_id === scope.persona_id) ?? states.data?.items[0];
  const rows = propertyRows(profile, state);
  const note = stateNote(profile, state);
  const reload = () => { void cache.invalidateQueries({ queryKey: projectionKey('personas', scope) }); void cache.invalidateQueries({ queryKey: projectionKey('character-states', scope) }); };
  return <Stack gap="lg">
    <div><Text className="eyebrow">02 / Персонаж</Text><Title order={1}>Персонаж</Title></div>
    {scope.memory_mode === 'test' && <Alert color="violet" title="Тестовая память">Профиль и состояние этой области изолированы от рабочей биографии.</Alert>}
    {(profiles.isError || states.isError) && <Alert color="orange" title="Часть данных недоступна">{failureMessage(profiles.error ?? states.error)} Показанное может быть неполным.</Alert>}
    {profiles.isPending ? <Text>Получаем профиль…</Text> : !profile ? <Paper withBorder p="md" radius="lg"><Title order={2} size="h4">Профиля нет</Title>
      <Text size="sm">Создайте первую личность ниже: без профиля у агента нет канона, а фиктивные воспоминания не создаются.</Text></Paper> : <>
      <Paper withBorder p="md" radius="lg" aria-label="Принятый профиль"><Group justify="space-between"><Title order={2} size="h4">Принятый профиль {profile.persona_id}</Title>
        <Badge variant="light">версия {profile.version}</Badge></Group>
        <Text size="xs" c="dimmed">Редакция {profile.accepted_revision_id} · обновлён {formatTime(profile.updated_at, 'short')}</Text>
        <Text fw={600} mt="sm">Канон</Text><Text className="plain-text">{profile.canon}</Text>
        <Text fw={600} mt="sm">Стиль</Text><Text className="plain-text">{profile.style}</Text></Paper>
      <Paper withBorder p="md" radius="lg" aria-label="Свойства"><Title order={2} size="h4" mb="xs">Свойства: канон и живое значение</Title>
        {note && <Alert color="orange" mb="xs">{note}</Alert>}
        {!rows.length ? <Text size="sm" c="dimmed">Свойств нет.</Text> : <Table aria-label="Свойства личности"><Table.Thead><Table.Tr><Table.Th>Свойство</Table.Th><Table.Th>Определение и автономность</Table.Th><Table.Th>Начальное (канон)</Table.Th><Table.Th>Живое</Table.Th></Table.Tr></Table.Thead>
          <Table.Tbody>{rows.map((row) => <Table.Tr key={row.name}><Table.Td>{row.name}</Table.Td><Table.Td className="plain-text">{row.definition}</Table.Td>
            <Table.Td>{row.initial ?? '—'}</Table.Td><Table.Td>{row.live ?? 'нет данных'}{row.drifted && <Badge ml="xs" size="xs" color="blue" variant="light">отличается от начального</Badge>}</Table.Td></Table.Tr>)}</Table.Tbody></Table>}
        <details><summary>Пределы развития</summary><Text size="xs" component="pre" className="plain-text">{JSON.stringify(profile.development_bounds.data, null, 2)}</Text></details></Paper>
      <Editor key={profile.id + profile.version} api={api} scope={scope} profile={profile} onReload={reload} /></>}
    <NewProfile api={api} scope={scope} source={profile} existing={(profiles.data?.items ?? []).map((item) => item.persona_id)} />
  </Stack>;
}
