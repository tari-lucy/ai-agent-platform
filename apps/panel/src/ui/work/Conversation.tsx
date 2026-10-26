import { Alert, Anchor, Badge, Button, Group, Paper, Select, Stack, Text, TextInput, Textarea, Title } from '@mantine/core';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { RunSession, Scope, WorkSessionDraft, WorkSessionProgress, WorkSessionTranscript } from '../../shared/contracts.ts';
import { ApiFailure, failureMessage, type PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { formatTime } from '../time.ts';
import { useWorkSessions } from './sessions.ts';
import { nativeHref } from '../opencode.ts';
import type { NativeAgent } from '../../shared/validation.ts';
import {
  callsText, closeCommand, elapsed, endedStates, MAX_MESSAGE, messageCommand, messageProblem, newestFirst, nextStep, openCommand, pending as awaiting, progressLabels,
  sessionStateLabels, stopNotice, titleProblem, turn, writable,
} from './conversation.ts';

const when = (value: string) => formatTime(value, 'short');
const speakerLabels = { operator: 'Вы', character: 'Агент', wake: '⏰ Будильник агента' } as const;

function PendingReply({ api, draft, running, refresh }: {
  api: PanelApi; draft: WorkSessionDraft; running: boolean; refresh: () => void;
}) {
  const element = useRef<HTMLDivElement>(null);
  const attempted = useRef(false);
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const receive = useCallback(async () => {
    if (inFlight.current || !running || !draft.acknowledgeable || draft.withheld) return;
    inFlight.current = true; setBusy(true); setError(null);
    try {
      await api.command({ type: 'dialogue.acknowledge', request_id: 'work-receipt-' + draft.utterance_id + '-' + draft.revision,
        scope: draft.scope, target: draft.utterance_id, expected_version: draft.version,
        arguments: { revision: draft.revision } });
      refresh();
    } catch (failure) { setError(failureMessage(failure)); }
    finally { inFlight.current = false; setBusy(false); }
  }, [api, draft, running, refresh]);
  useEffect(() => {
    if (!element.current || draft.withheld || !draft.acknowledgeable || !running) return;
    let ratio = 0;
    const check = () => {
      if (ratio < 0.6 || document.visibilityState !== 'visible' || attempted.current) return;
      attempted.current = true;
      void receive();
    };
    const observer = new IntersectionObserver(([seen]) => {
      ratio = seen.isIntersecting ? seen.intersectionRatio : 0; check();
    }, { threshold: [0, 0.6, 1] });
    observer.observe(element.current);
    document.addEventListener('visibilitychange', check);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', check); };
    // One automatic attempt per exact draft; refresh replaces it after a confirmed ACK.
  }, [draft, running, receive]);
  return <Paper ref={element} withBorder p="xs" bg="blue.0" aria-label="Ответ агента, ожидающий получения">
    <Text size="xs" c="dimmed">Агент · ответ ещё не подтверждён как полученный</Text>
    {draft.withheld ? <Text size="sm">Текст скрыт: его основания больше недоступны.</Text> :
      <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{draft.text}</Text>}
    {error && <Text size="sm" c="red.8" role="alert">{error} Можно подтвердить получение повторно.</Text>}
    {draft.acknowledgeable && running && <Button mt={4} size="xs" variant="light" loading={busy}
      onClick={() => void receive()}>Подтвердить получение ответа</Button>}
    {!draft.acknowledgeable && !draft.withheld && <Text size="xs" c="dimmed">
      Подтверждение пока недоступно. После остановки прежний ответ не применяется; новое сообщение начнёт новый ход.
    </Text>}
  </Paper>;
}

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

// The current turn from recorded facts only: state, time since it was recorded, confirmed steps,
// saved files and what to do next. Shown with the server's check time, never animated.
function Turn({ progress, checkedAt }: { progress: WorkSessionProgress; checkedAt: string }) {
  const ended = endedStates.has(progress.state);
  const since = elapsed(progress.since, checkedAt);
  const calls = callsText(progress);
  return <Paper withBorder p="xs" mt="xs" aria-label="Ход выполнения" bg={ended ? 'orange.0' : 'gray.0'}>
    <Text size="sm" fw={600} role={ended ? 'alert' : 'status'}>{progressLabels[progress.state]}
      {since && <Text span size="xs" c="dimmed" fw={400}> · {progress.state === 'running' ? 'идёт' : 'прошло'} {since} (на {when(checkedAt)})</Text>}</Text>
    {calls && <Text size="xs" c="dimmed">{calls}</Text>}
    {!!progress.files?.length && <Stack gap={2} mt={4} aria-label="Сохранённые файлы">
      <Text size="xs" fw={600}>Сохранено:</Text>
      {progress.files.map((file) => <Text key={file.file_id + ':' + file.version} size="xs">
        <Anchor href="#/library">{file.title}</Anchor> · {file.path}{file.current ? '' : ' (позже изменён)'}</Text>)}
    </Stack>}
    {progress.state === 'done' && !progress.files?.length && <Text size="xs" c="dimmed">Файлов этот ход не сохранил.</Text>}
    <Text size="xs" mt={4}>{nextStep[progress.state]}</Text>
    {progress.error_code && <details><summary>Подробности</summary><Text size="xs" c="dimmed">Код: {progress.error_code}
      {progress.attempt_no ? ` · попытка ${progress.attempt_no}` : ''}</Text></details>}
    <Text size="xs" c="dimmed">Обновление страницы ничего не отправляет повторно.</Text>
  </Paper>;
}

function Thread({ api, scope, transcript, running, native }: {
  api: PanelApi; scope: Scope; transcript: WorkSessionTranscript; running: boolean; native?: NativeAgent | null;
}) {
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
    {transcript.access_ticket_id && <Text size="sm">Запрос разрешения: <Anchor href="#/tickets">открыть обращения</Anchor></Text>}
    {!!transcript.open_ticket_ids?.length && <Text size="sm">Агент ждёт вашего ответа в обращении: {transcript.open_ticket_ids.map((id, index) =>
      <Anchor key={id} href={'#/tickets?ticket=' + encodeURIComponent(id)}>{index ? ', ' : ''}открыть</Anchor>)}</Text>}
    {stopNotice(transcript) && <Text size="sm" c="orange.8" role="status">{stopNotice(transcript)}</Text>}
    {!transcript.complete && transcript.state !== 'invalidated' && <Text size="sm" c="orange.8">Показаны последние сообщения.</Text>}
    <Stack gap={6} mt="xs" component="ol" aria-label="Сообщения разговора" style={{ listStyle: 'none', padding: 0, margin: 0 }}>
      {transcript.entries.map((entry) => <Paper key={entry.sequence} component="li" p="xs" bg={entry.speaker !== 'character' ? 'gray.0' : 'blue.0'}>
        <Text size="xs" c="dimmed">{speakerLabels[entry.speaker]} · {when(entry.at)}{awaiting(transcript, entry.sequence, entry.speaker) ? (transcript.progress && endedStates.has(transcript.progress.state) ? ' · ответа нет' : ' · сохранено, ждёт исполнения') : ''}</Text>
        {entry.withheld ? <Text size="sm" c="dimmed">Скрыто: основание сообщения отозвано, истекло или удалено.</Text>
          : <Text size="sm" style={{ whiteSpace: 'pre-wrap' }}>{entry.text}</Text>}
      </Paper>)}
      {!transcript.entries.length && <Text size="sm" c="dimmed">Сообщений пока нет.</Text>}
    </Stack>
    {transcript.progress && transcript.progress.state !== 'reply_ready' && <Turn progress={transcript.progress} checkedAt={transcript.checked_at} />}
    {transcript.pending_reply && <PendingReply key={transcript.pending_reply.utterance_id}
      api={api} draft={transcript.pending_reply} running={running} refresh={refresh} />}
    {native !== undefined ? native && <Anchor mt="sm" display="block" href={nativeHref(native.directory, transcript.session_id)}>Открыть в OpenCode</Anchor> :
      writable(transcript) && <Stack gap={4} mt="sm">
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
  // PUI-03: undefined — no native window (old forms); null/agent — read-only, OpenCode link.
  native?: NativeAgent | null;
};
export function Conversation({ api, scope, run, chosen, onSelect: setSelected, native }: ConversationProps) {
  const sessions = useWorkSessions(api, scope);
  const rows = newestFirst(sessions.data?.items ?? []);
  // The newest conversation is shown until the operator picks another one.
  const selected = chosen ?? rows[0]?.id ?? null;
  const running = run?.state === 'ACTIVE';
  const transcript = useQuery({
    queryKey: [...projectionKey('work-session-transcript', scope), selected],
    queryFn: ({ signal }) => api.workSessionTranscript(scope, selected!, signal),
    enabled: selected !== null,
    // Re-read recorded state while a draft waits or a turn is queued/running (no event marks a
    // job start); the view itself never animates or resends anything.
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!running || !data) return false;
      if (data.pending_reply) return 1500;
      return data.progress && (data.progress.state === 'queued' || data.progress.state === 'running') ? 3000 : false;
    },
  });
  const reading = native !== undefined;
  return <Stack gap="sm" aria-label={reading ? 'Разговоры о работе' : 'Разговор о работе'}>
    <Title order={2} size="h5">{reading ? 'Разговоры о работе · чтение' : 'Разговор о работе'}</Title>
    {reading ? <Text size="sm" c="dimmed">Прежние и текущие разговоры для чтения; писать — в OpenCode.</Text> :
      <Text size="sm" c="dimmed">Обсудите поручение с самим агентом: уточните цель, передайте материалы, ответьте на вопросы.
        Писать можно и когда агент остановлен: сообщение сохранится и будет ждать исполнения.</Text>}
    {sessions.isError && <Alert color="orange" title="Разговоры недоступны">{failureMessage(sessions.error)}</Alert>}
    {!reading && <Start api={api} scope={scope} onOpened={setSelected} />}
    {rows.length > 0 && <Select label="Разговор" data={rows.map((row) => ({ value: row.id, label: `${row.title} — ${sessionStateLabels[row.state ?? 'open']}` }))}
      value={selected} onChange={setSelected} allowDeselect={false} />}
    {transcript.isError && <Alert color="orange" title="Разговор недоступен">{failureMessage(transcript.error)}</Alert>}
    {transcript.data && <Thread key={transcript.data.session_id} api={api} scope={scope} transcript={transcript.data} running={running} native={native} />}
  </Stack>;
}
