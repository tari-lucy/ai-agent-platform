import { Badge, Button, Group, Text } from '@mantine/core';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { formatTime } from '../time.ts';
import { statusLabels, VISIBLE_RATIO, type Entry } from './model.ts';
import { atBottom, unseen } from './scroll.ts';

const when = (value: string) => formatTime(value, 'medium');
export type Stream = { text: string; incomplete: boolean };

function EntryRow({ entry, onShown }: { entry: Entry; onShown?: (entry: Entry, ratio: number) => void }) {
  const element = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (!onShown || !element.current) return;
    let ratio = 0;
    // The ratio is measured against the scrolling viewport and the browser window together.
    const check = () => { if (ratio >= VISIBLE_RATIO && document.visibilityState === 'visible') onShown(entry, ratio); };
    const observer = new IntersectionObserver(([seen]) => { ratio = seen.isIntersecting ? seen.intersectionRatio : 0; check(); },
      { threshold: [0, 0.25, VISIBLE_RATIO, 1] });
    observer.observe(element.current);
    document.addEventListener('visibilitychange', check);
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', check); };
  }, [entry, onShown]);
  const color = entry.status === 'done' || entry.status === 'saved' ? 'teal' :
    entry.status === 'failed' || entry.status === 'unknown' ? 'orange' :
      entry.status === 'cancelled' || entry.status === 'removed' ? 'gray' : 'blue';
  return <li ref={element} data-key={entry.key} className={`entry ${entry.kind}`}>
    <Group justify="space-between" gap="xs">
      <Text size="sm" fw={600}>{entry.author}</Text>
      <Text size="xs" c="dimmed" title={entry.at}>{when(entry.at)}</Text>
    </Group>
    {entry.text === null ? <Text size="sm" c="dimmed" fs="italic">Текст недоступен.</Text> :
      <Text className="plain-text">{entry.text}</Text>}
    <Group gap="xs" mt={4}>
      <Badge variant="light" color={color}>{statusLabels[entry.status]}</Badge>
      <Text size="xs" c="dimmed">{entry.detail}</Text>
    </Group>
  </li>;
}

// A scrolling feed that keeps the reader's place. Older records may arrive above the reader (the
// server pages by id, not by time), so the entry nearest the top is held in place; a new reply moves
// the view only when the reader is already at the end, otherwise a jump button counts what is new.
export function Timeline({ entries, stream, onShown }: {
  entries: Entry[]; stream: Stream | null; onShown?: (entry: Entry, ratio: number) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const keep = useRef<{ bottom: boolean; key: string | null; top: number }>({ bottom: true, key: null, top: 0 });
  const [bottom, setBottom] = useState(true);
  const [seen, setSeen] = useState<string | null>(entries.at(-1)?.key ?? null);
  const keys = entries.map((entry) => entry.key);

  function record() {
    const el = box.current;
    if (!el) return;
    const edge = el.getBoundingClientRect().top;
    const nodes = el.querySelectorAll<HTMLElement>('[data-key]');
    for (const node of nodes) {
      const rect = node.getBoundingClientRect();
      if (rect.bottom > edge) { keep.current.key = node.dataset.key ?? null; keep.current.top = rect.top - edge; break; }
    }
    keep.current.bottom = atBottom({ top: el.scrollTop, height: el.scrollHeight, client: el.clientHeight });
  }
  function onScroll() {
    record();
    setBottom(keep.current.bottom);
    if (keep.current.bottom) setSeen(entries.at(-1)?.key ?? null);
  }
  function jump() {
    const el = box.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
    record();
    setBottom(true);
    setSeen(entries.at(-1)?.key ?? null);
  }

  useLayoutEffect(() => {
    const el = box.current;
    if (!el) return;
    const mark = keep.current;
    if (mark.bottom) {
      el.scrollTop = el.scrollHeight;
    } else if (mark.key !== null) {
      const node = [...el.querySelectorAll<HTMLElement>('[data-key]')].find((item) => item.dataset.key === mark.key);
      if (node) el.scrollTop += node.getBoundingClientRect().top - el.getBoundingClientRect().top - mark.top;
    }
    record();
  }, [entries, stream]);
  useEffect(() => { if (keep.current.bottom) setSeen(keys.at(-1) ?? null); }, [keys.at(-1)]); // eslint-disable-line react-hooks/exhaustive-deps

  const fresh = bottom ? 0 : unseen(keys, seen);
  return <div className="timeline-frame" style={{ position: 'relative' }}>
    <div ref={box} className="timeline-scroll" tabIndex={0} role="region" aria-label="Прокручиваемая лента" onScroll={onScroll}
      style={{ maxHeight: '60vh', overflowY: 'auto', overflowAnchor: 'none' }}>
      <ol className="timeline" aria-label="Лента диалога" aria-live="off">
        {entries.map((entry) => <EntryRow key={entry.key} entry={entry} onShown={entry.kind === 'reply' ? onShown : undefined} />)}
        {stream && <li data-key="stream" className="entry reply" aria-label="Ответ формируется">
          <Text size="xs" c="dimmed">Ответ формируется{stream.incomplete ? ' · поток неполный' : ''} · предварительно, ещё не доставлен</Text>
          <Text className="plain-text">{stream.text}</Text>
        </li>}
      </ol>
    </div>
    {fresh > 0 && <Button className="timeline-jump" size="compact-sm" onClick={jump}
      style={{ position: 'absolute', right: 12, bottom: 12 }}>Новые сообщения: {fresh} ↓</Button>}
  </div>;
}
