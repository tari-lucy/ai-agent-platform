import { formatTime } from '../time.ts';
import { Alert, Badge, Button, Checkbox, Group, Paper, Stack, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useInfiniteQuery, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import type { AccountLink, Person, RunSession, RunSessionPage, Scope } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import {
  collisionsOf, confirmationLabels, confirmedAccounts, emptyLink, encountersOf, groundsSummary, involves, label, linkCommand, linkProblem, orderPromises,
  otherAccounts, overdue, promiseLabels, relationshipOf, sharedNames, topicLabels, unlinkCommand, unlinkProblem, type Link,
} from './model.ts';

const when = (value: string) => formatTime(value, 'short');

function usePaged<T extends { items: unknown[]; complete: boolean; next_cursor: string | null }>(
  scope: Scope, resource: string, read: (cursor: string | undefined, signal: AbortSignal) => Promise<T>,
) {
  return useInfiniteQuery({
    queryKey: [...projectionKey(resource, scope), 'people'], initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam, signal }) => read(pageParam, signal),
    getNextPageParam: (last) => (last.complete || !last.next_cursor ? undefined : last.next_cursor),
  });
}

// A link or unlink changes who a person is: it needs a reason, an explicit tick, and the seen version.
function Accounts({ api, scope, run, person, everyone }: { api: PanelApi; scope: Scope; run: RunSession | undefined; person: Person; everyone: Person[] }) {
  const cache = useQueryClient();
  const [link, setLink] = useState<Link>(emptyLink);
  const [problem, setProblem] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [unlinking, setUnlinking] = useState<AccountLink | null>(null);
  const [reason, setReason] = useState('');
  async function send(command: Parameters<PanelApi['command']>[0], done: () => void) {
    setProblem(null); setSaved(false);
    try { await api.command(command); setSaved(true); done(); }
    catch (error) {
      setProblem(error instanceof ApiFailure && error.code === 'VERSION_CONFLICT' ? 'Запись человека уже изменена. Обновите её и пересмотрите правку; чужое изменение не затирается.' : failureMessage(error) + ' Ничего не изменено.');
    } finally { void cache.invalidateQueries({ queryKey: projectionKey('people', scope) }); }
  }
  function submitLink() {
    const issue = linkProblem(person, everyone, link);
    if (issue || !run) { setProblem(issue ?? 'Нет подтверждённого запуска в этой области.'); return; }
    void send(linkCommand(run, person, link, crypto.randomUUID()), () => setLink(emptyLink));
  }
  function submitUnlink() {
    const issue = unlinkProblem(reason);
    if (issue || !run || !unlinking) { setProblem(issue ?? 'Нет подтверждённого запуска в этой области.'); return; }
    void send(unlinkCommand(run, person, unlinking, reason, crypto.randomUUID()), () => { setUnlinking(null); setReason(''); });
  }
  return <Stack gap="xs">
    <Title order={4} size="h5">Подтверждённые аккаунты</Title>
    {!confirmedAccounts(person).length ? <Text size="sm" c="dimmed">Подтверждённых аккаунтов нет: человек идентифицирован только гипотезой.</Text> :
      confirmedAccounts(person).map((account) => <Group key={account.platform_id + account.account_id} justify="space-between" wrap="nowrap">
        <Text size="sm">{account.platform_id}: {account.account_id} <Text span c="dimmed">({account.display_name}) · {groundsSummary(account.evidence)}</Text></Text>
        <Button size="compact-xs" variant="subtle" color="gray" onClick={() => { setUnlinking(account); setReason(''); }}>Отвязать</Button></Group>)}
    {!!otherAccounts(person).length && <><Text size="sm" fw={600}>Не подтверждены</Text>
      {otherAccounts(person).map((account) => <Text key={account.platform_id + account.account_id} size="sm" c="dimmed">{account.platform_id}: {account.account_id} · {confirmationLabels[account.confirmation]}</Text>)}</>}
    {unlinking && <Paper withBorder p="sm" aria-label="Отвязка аккаунта"><Text size="sm">Отвязать {unlinking.platform_id}: {unlinking.account_id}?</Text>
      <Textarea label="Основание отвязки" autosize minRows={1} maxLength={1000} value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
      <Group mt="xs"><Button color="red" variant="light" onClick={submitUnlink}>Отвязать</Button><Button variant="subtle" color="gray" onClick={() => setUnlinking(null)}>Отмена</Button></Group></Paper>}
    <Paper withBorder p="sm" aria-label="Связать аккаунт"><Text size="sm" fw={600}>Связать подтверждённый аккаунт</Text>
      <Group grow mt="xs"><TextInput label="Платформа" value={link.platform} onChange={(e) => setLink({ ...link, platform: e.currentTarget.value.trim() })} />
        <TextInput label="Идентификатор аккаунта" value={link.account} onChange={(e) => setLink({ ...link, account: e.currentTarget.value })} /></Group>
      <Textarea mt="xs" label="Основание: почему это тот же человек" autosize minRows={1} maxLength={1000} value={link.reason} onChange={(e) => setLink({ ...link, reason: e.currentTarget.value })} />
      <Checkbox mt="xs" label="Подтверждаю, что это один и тот же человек" checked={link.confirmed} onChange={(e) => setLink({ ...link, confirmed: e.currentTarget.checked })} />
      <Group mt="xs" justify="flex-end"><Button variant="light" onClick={submitLink}>Связать</Button></Group>
      <Text size="xs" c="dimmed">Совпадение ника ничего не связывает. Слияние двух людей — отдельная операция.</Text></Paper>
    {problem && <Alert color="orange" role="alert">{problem}</Alert>}
    {saved && <Alert color="teal" role="status">Команда принята ядром. Результат подтвердит владелец.</Alert>}
  </Stack>;
}

export function PeopleTab({ api, scope }: { api: PanelApi; scope: Scope }) {
  const runs = useQuery({ queryKey: projectionKey('sessions', scope), queryFn: ({ signal }) => api.runs(scope, signal), enabled: false });
  const run = (runs.data as RunSessionPage | undefined)?.items[0];
  const people = usePaged(scope, 'people', (cursor, signal) => api.people(scope, signal, cursor));
  const encounters = usePaged(scope, 'encounters', (cursor, signal) => api.encounters(scope, signal, cursor));
  const relationships = usePaged(scope, 'relationships', (cursor, signal) => api.relationships(scope, signal, cursor));
  const topics = usePaged(scope, 'topics', (cursor, signal) => api.topics(scope, signal, cursor));
  const promises = usePaged(scope, 'promises', (cursor, signal) => api.promises(scope, signal, cursor));
  const [selected, setSelected] = useState<string | null>(null);
  const [now] = useState(() => Date.now());
  const all = [people, encounters, relationships, topics, promises];
  const flat = <T,>(query: { data?: { pages: { items: T[] }[] } }) => query.data?.pages.flatMap((page) => page.items) ?? [];
  const everyone = flat(people);
  const shared = sharedNames(everyone);
  const person = everyone.find((item) => item.person_id === selected) ?? null;
  const failed = all.filter((query) => query.isError);
  const more = all.filter((query) => query.hasNextPage);
  const own = person ? { meetings: encountersOf(flat(encounters), person.person_id), relation: relationshipOf(flat(relationships), person.person_id),
    topics: involves(flat(topics), person.person_id), promises: orderPromises(involves(flat(promises), person.person_id)) } : null;
  return <Stack gap="md">
    {!!failed.length && <Alert color="orange" title="Часть данных недоступна">{failureMessage(failed[0].error)} Картина может быть неполной.</Alert>}
    {!!more.length && <Alert color="orange">Загружены не все записи.<Button ml="sm" size="compact-sm" variant="light" onClick={() => more.forEach((q) => void q.fetchNextPage())}>Загрузить ещё</Button></Alert>}
    <Group align="flex-start" grow wrap="wrap">
      <Paper withBorder p="md" radius="lg" style={{ flex: '1 1 20rem' }}><Title order={2} size="h4" mb="xs">Люди ({everyone.length})</Title>
        {people.isPending ? <Text size="sm">Получаем людей…</Text> : !everyone.length ? <Text size="sm" c="dimmed">Людей в памяти пока нет.</Text> :
          <ul className="memory-list" aria-label="Список людей">{everyone.map((item) => <li key={item.id}>
            <button type="button" className={`memory-item ${item.person_id === selected ? 'selected' : ''}`} onClick={() => setSelected(item.person_id)}>
              <Text size="sm" fw={600}>{label(item)}</Text><Text size="xs" c="dimmed">{item.person_id}</Text>
              {collisionsOf(item, shared).length > 0 && <Badge size="xs" color="orange" variant="light">такой же ник у другого человека</Badge>}</button></li>)}</ul>}</Paper>
      <div style={{ flex: '1 1 26rem' }}>{person && own ? <Paper withBorder p="md" radius="lg" aria-label="Карточка человека">
        <Title order={2} size="h4">{label(person)}</Title><Text size="xs" c="dimmed">{person.person_id} · версия {person.version}</Text>
        {collisionsOf(person, shared).length > 0 && <Alert color="orange" mt="xs">Тот же ник встречается у других людей: {collisionsOf(person, shared).join(', ')}. Это разные люди, пока аккаунты не подтверждены как один.</Alert>}
        <Stack gap="md" mt="sm">
          <Accounts key={person.person_id + person.version} api={api} scope={scope} run={run} person={person} everyone={everyone} />
          <div><Title order={4} size="h5">История встреч ({own.meetings.length})</Title>
            {!own.meetings.length ? <Text size="sm" c="dimmed">Встреч не записано.</Text> : own.meetings.map((meeting) => <Text key={meeting.id} size="sm">{when(meeting.created_at)} · {groundsSummary(meeting.sources)}{meeting.person_ids.length > 1 ? ` · участников: ${meeting.person_ids.length}` : ''}</Text>)}</div>
          <div><Title order={4} size="h5">Основания отношения</Title>
            {!own.relation ? <Text size="sm" c="dimmed">Отношения ещё не сложились.</Text> : <><Text size="sm">{groundsSummary(own.relation.grounds)} · версия {own.relation.version}</Text>
              <details><summary>Аспекты отношения</summary><Text size="xs" component="pre" className="plain-text">{JSON.stringify(own.relation.aspects.data, null, 2)}</Text></details></>}</div>
          <div><Title order={4} size="h5">Темы и вопросы</Title>
            {!own.topics.length ? <Text size="sm" c="dimmed">Тем нет.</Text> : own.topics.map((topic) => <Paper key={topic.id} withBorder p="xs" mb={4}>
              <Group gap="xs"><Badge size="xs" variant="light">{topicLabels[topic.status]}</Badge><Text size="sm" className="plain-text">{topic.topic_id}</Text></Group>
              {topic.answer != null && <Text size="sm" className="plain-text">Известный ответ: {topic.answer}</Text>}</Paper>)}</div>
          <div><Title order={4} size="h5">Обещания</Title>
            {!own.promises.length ? <Text size="sm" c="dimmed">Обещаний нет.</Text> : own.promises.map((promise) => <Paper key={promise.id} withBorder p="xs" mb={4}>
              <Group gap="xs"><Badge size="xs" variant="light" color={promise.status === 'unknown' || overdue(promise, now) ? 'orange' : promise.status === 'fulfilled' ? 'teal' : 'gray'}>{promiseLabels[promise.status]}</Badge>
                {overdue(promise, now) && <Badge size="xs" color="orange">просрочено</Badge>}</Group>
              <Text size="sm" className="plain-text">{promise.content}</Text><Text size="xs" c="dimmed" className="plain-text">Критерий: {promise.completion_criterion}</Text>
              {promise.closure_result != null && <Text size="xs" className="plain-text">Итог: {promise.closure_result}</Text>}
              {promise.status === 'unknown' && <Text size="xs" c="orange.8">Исход неизвестен: удаление разговора не означает выполнения.</Text>}</Paper>)}</div>
        </Stack></Paper> : <Alert color="gray">Выберите человека, чтобы увидеть аккаунты, встречи, тему и обещания.</Alert>}</div>
    </Group>
  </Stack>;
}
