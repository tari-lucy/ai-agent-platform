import { Alert, Badge, Button, Group, Paper, Select, Stack, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import type { RunSession, Scope, WorkSessionTranscript } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import { useWorkSessions } from './sessions.ts';
import {
  closeCommand, MAX_MESSAGE, messageCommand, messageProblem, newestFirst, openCommand, pending as awaiting, sessionStateLabels, titleProblem, turn, writable,
} from './conversation.ts';

const when = (value: string) => formatTime(value, 'short');
const speakerLabels = { operator: 'Вы', character: 'Агент' } as const;

function Start({ api, scope, onOpened }: { api: PanelApi; scope: Scope; onOpened: (id: string) => void }) {
  const cache = useQueryClient();
  const [title, setTitle] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef<string | null>(null); // one request id per draft: a retry replays
  async function submit() {
    const issue = titleProblem(title);
    if (issue) { setProblem(issue); return; }
    setProblem(null); setBusy(true);
    pending.current ??= crypto.randomUUID();
    try {
      const receipt = await api.command(openCommand(scope, title, pending.current));
      pending.current = null; setTitle('');
      if ('resource_id' in receipt && receipt.resource_id) onOpened(receipt.resource_id);
    } catch (error) {
      setProblem(failureMessage(error) + ' Повтор использует тот же запрос и не создаст второй разговор.');
    } finally {
      setBusy(false);
      void cache.invalidateQueries({ queryKey: projectionKey('work-sessions', scope) });
    }
  }
  return <Group align="flex-end" gap="xs" wrap="wrap">
    <TextInput label="Новый разговор о работе" placeholder="Например: конспект справочника" value={title}
      onChange={(event) => { setTitle(event.currentTarget.value); pending.current = null; }} style={{ flex: '1 1 16rem' }} />
    <Button onClick={() => void submit()} loading={busy}>Начать разговор</Button>
    {problem && <Text size="sm" c="red.8" role="alert" w="100%">{problem}</Text>}
  </Group>;
}

function Thread({ api, scope, transcript, running }: { api: PanelApi; scope: Scope; transcript: WorkSessionTranscript; running: boolean }) {
  const cache = useQueryClient();
  const [text, setText] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pending = useRef<string | null>(null);
  const refresh = () => {
    void cache.invalidateQueries({ queryKey: projectionKey('work-session-transcript', scope) });
    void cache.invalidateQueries({ queryKey: projectionKey('work-sessions', scope) });
  };
  async function send() {
    const issue = messageProblem(text);
    if (issue) { setProblem(issue); return; }
    setProblem(null); setBusy(true);
    pending.current ??= crypto.randomUUID();
    try {
      await api.command(messageCommand(scope, transcript, text, pending.current));
      pending.current = null; setText('');
    } catch (error) {
      if (error instanceof ApiFailure && error.code === 'VERSION_CONFLICT') {
        pending.current = null;
        setProblem('Разговор уже изменился. Прочитайте новые сообщения; ваш текст сохранён в поле.');
      } else setProblem(failureMessage(error) + ' Повторная отправка использует тот же запрос и не создаст дубль.');
    } finally { setBusy(false); refresh(); }
  }
  async function close() {
    setBusy(true); setProblem(null);
    try { await api.command(closeCommand(scope, transcript, crypto.randomUUID())); }
    catch (error) { setProblem(failureMessage(error)); }
    finally { setBusy(false); refresh(); }
  }
  return <Paper withBorder p="sm" aria-label={'Разговор: ' + transcript.title}>
    <Group justify="space-between" wrap="wrap" gap="xs">
      <Text fw={600}>{transcript.title}</Text>
      <Badge variant="light">{sessionStateLabels[transcript.state]}</Badge>
    </Group>
    <Text size="sm" c="dimmed">Чей ход: {turn(transcript, running)}</Text>
    {!transcript.complete && transcript.state !== 'invalidated' && <Text size="sm" c="orange.8">Показаны последние сообщения.</Text>}
    <Stack gap={6} mt="xs" component="ol" aria-label="Сообщения разговора" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
      {transcript.entries.map((entry) => <Paper key={entry.sequence} component="li" p="xs" bg={entry.speaker === 'operator' ? 'gray.0' : 'blue.0'}>
        <Text size="xs" c="dimmed">{speakerLabels[entry.speaker]} · {when(entry.at)}{awaiting(transcript, entry.sequence, entry.speaker) ? ' · сохранено, ждёт исполнения' : ''}</Text>
        {entry.withheld ? <Text size="sm" c="dimmed">Скрыто: основание сообщения отозвано, истекло или удалено.</Text>
          : <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{entry.text}</Text>}
      </Paper>)}
      {!transcript.entries.length && <Text size="sm" c="dimmed">Сообщений пока нет.</Text>}
    </Stack>
    {writable(transcript) && <Stack gap={4} mt="sm">
      <Textarea label="Сообщение агенту" autosize minRows={2} maxLength={MAX_MESSAGE} value={text}
        onChange={(event) => { setText(event.currentTarget.value); pending.current = null; }} />
      {problem && <Text size="sm" c="red.8" role="alert">{problem}</Text>}
      <Group gap="xs">
        <Button onClick={() => void send()} loading={busy}>Отправить</Button>
        <Button variant="default" onClick={() => void close()} disabled={busy}>Закрыть разговор</Button>
      </Group>
    </Stack>}
  </Paper>;
}

type ConversationProps = {
  api: PanelApi; scope: Scope; run?: RunSession;
  // Owned by the work screen, so a task card can open its conversation here.
  chosen: string | null; onSelect: (id: string | null) => void;
};
export function Conversation({ api, scope, run, chosen, onSelect: setSelected }: ConversationProps) {
  const sessions = useWorkSessions(api, scope);
  const rows = newestFirst(sessions.data?.items ?? []);
  // The newest conversation is shown until the operator picks another one.
  const selected = chosen ?? rows[0]?.id ?? null;
  const transcript = useQuery({
    queryKey: [...projectionKey('work-session-transcript', scope), selected],
    queryFn: ({ signal }) => api.workSessionTranscript(scope, selected!, signal),
    enabled: selected !== null,
  });
  const running = run?.state === 'ACTIVE';
  return <Stack gap="sm" aria-label="Разговор о работе">
    <Title order={2} size="h5">Разговор о работе</Title>
    <Text size="sm" c="dimmed">Обсудите поручение с самим агентом: уточните цель, передайте материалы, ответьте на вопросы.
      Писать можно и когда агент остановлен: сообщение сохранится и будет ждать исполнения.</Text>
    {sessions.isError && <Alert color="orange" title="Разговоры недоступны">{failureMessage(sessions.error)}</Alert>}
    <Start api={api} scope={scope} onOpened={setSelected} />
    {rows.length > 0 && <Select label="Разговор" data={rows.map((row) => ({ value: row.id, label: `${row.title} — ${sessionStateLabels[row.state ?? 'open']}` }))}
      value={selected} onChange={setSelected} allowDeselect={false} />}
    {transcript.isError && <Alert color="orange" title="Разговор недоступен">{failureMessage(transcript.error)}</Alert>}
    {transcript.data && <Thread key={transcript.data.session_id} api={api} scope={scope} transcript={transcript.data} running={running} />}
  </Stack>;
}
