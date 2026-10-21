import { Alert, Badge, Button, Group, Paper, Stack, Text, Title } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import type { ManagedFile, Scope, SkillVersion } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import { areaLabels, kindLabels, knowledge, provenanceLabels, skillOrder, sourcesLabel, statusLabels } from './model.ts';

const when = (value: string) => formatTime(value, 'short');
const statusColor: Record<SkillVersion['status'], string> = { verified: 'teal', experimental: 'yellow', retired: 'gray' };

function Skill({ skill }: { skill: SkillVersion }) {
  const uses = skill.uses_capabilities ?? [];
  return <Paper withBorder p="sm" component="li" aria-label={skill.title}>
    <Group justify="space-between" wrap="wrap" gap="xs">
      <Text fw={600}>{skill.title}</Text>
      <Badge color={statusColor[skill.status]} variant="light">{statusLabels[skill.status]}</Badge>
    </Group>
    <Text size="sm" mt={4}>Когда применять: {skill.applicability}</Text>
    <Text size="sm">Ограничения: {skill.limitations}</Text>
    {uses.length > 0 && <Text size="xs" c="dimmed">Описывает использование: {uses.join(', ')} — это не права.</Text>}
    <Text size="xs" c="dimmed">Версия {skill.version} · метод v{skill.method.version} · {when(skill.updated_at)} · {skill.change_reason}</Text>
  </Paper>;
}

function File({ file }: { file: ManagedFile }) {
  return <Paper withBorder p="sm" component="li" aria-label={file.title}>
    <Group justify="space-between" wrap="wrap" gap="xs">
      <Text fw={600}>{file.title}</Text>
      <Badge variant="light" color={file.kind === 'hypothesis' ? 'grape' : 'blue'}>{kindLabels[file.kind]}</Badge>
    </Group>
    <Text size="xs" c="dimmed">{areaLabels[file.area]} · {file.path} · {provenanceLabels[file.provenance]} · {sourcesLabel(file)} · v{file.version} · {when(file.updated_at)}</Text>
  </Paper>;
}

function Section<T>({ title, rows, empty, render }: { title: string; rows: T[]; empty: string; render: (row: T) => React.JSX.Element }) {
  return <Stack gap="xs">
    <Title order={2} size="h5">{title}</Title>
    {rows.length ? <Stack gap="xs" component="ul" style={{ listStyle: 'none', padding: 0, margin: 0 }}>{rows.map(render)}</Stack> :
      <Text size="sm" c="dimmed">{empty}</Text>}
  </Stack>;
}

export function LibraryScreen({ api, scope }: { api: PanelApi; scope: Scope }) {
  const library = useQuery({
    queryKey: projectionKey('workplace-library', scope),
    queryFn: ({ signal }) => api.workplaceLibrary(scope, signal),
    retry: (count, error) => !(error instanceof ApiFailure && error.code === 'CAPABILITY_UNAVAILABLE') && count < 2,
  });
  const off = library.error instanceof ApiFailure && library.error.code === 'CAPABILITY_UNAVAILABLE';
  const data = library.data;
  const parts = data ? knowledge(data) : null;
  return <Stack gap="md">
    <Group justify="space-between" wrap="wrap">
      <div><Title order={1} size="h3">Библиотека</Title>
        <Text size="sm" c="dimmed">Навыки и знания агента. Заметка — собственный текст, гипотеза — предположение; ни то, ни другое не подтверждённый опыт.</Text></div>
      <Button variant="default" size="xs" loading={library.isFetching} onClick={() => void library.refetch()}>Обновить</Button>
    </Group>
    {off ? <Paper withBorder p="md"><Text size="sm">Библиотека не включена на этом хосте. Её включает оператор офлайн-командой enable-workplace-files; доступ агента к областям выдаётся отдельным решением по обращению.</Text></Paper> :
      library.isError ? <Alert color="orange" title="Библиотека недоступна">{failureMessage(library.error)}</Alert> :
      !data || !parts ? <Text size="sm">Получаем библиотеку…</Text> : <>
        {!data.complete && <Text size="sm" c="orange.8">Показана только часть: список достиг предела.</Text>}
        <Section title="Навыки" rows={skillOrder(data.skills)} empty="Навыков пока нет." render={(skill) => <Skill key={skill.id} skill={skill} />} />
        <Section title="Заметки" rows={parts.notes} empty="Заметок пока нет." render={(file) => <File key={file.id} file={file} />} />
        <Section title="Гипотезы" rows={parts.hypotheses} empty="Гипотез пока нет." render={(file) => <File key={file.id} file={file} />} />
        {parts.other.length > 0 && <Section title="Другие файлы" rows={parts.other} empty="" render={(file) => <File key={file.id} file={file} />} />}
        <Text size="xs" c="dimmed">Проверено {when(data.checked_at)}. Файлы с отозванными или истёкшими источниками не показываются.</Text>
      </>}
  </Stack>;
}
