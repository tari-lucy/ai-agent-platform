import { Group, Image, Modal, Skeleton, Stack, Text } from '@mantine/core';
import { useQuery } from '@tanstack/react-query';
import { useState } from 'react';
import type { NowAbility, NowActivity, NowPicture, Scope } from '../../shared/contracts.ts';
import type { PanelApi } from '../../shared/api.ts';
import { projectionKey } from '../../shared/scope.ts';
import { ago, type Funnel, type Ribbon, type Trait } from './model.ts';

const stateWord = { doing: 'идёт', waiting: 'ждёт', planned: 'в планах', done: 'готово' } as const;
const stateColor = { doing: '#12b886', waiting: '#4c6ef5', planned: '#adb5bd', done: '#ced4da' } as const;

// One activity per row; the details open on a click, so the list stays short.
export function Activities({ rows, doing }: { rows: NowActivity[]; doing?: string | null }) {
  return <Stack gap={2}>
    {doing && <Text size="sm" c="dimmed" mb={6}>Прямо сейчас: {doing}</Text>}
    {rows.map((row) => <details key={row.title} className="activity">
      <summary>
        <span className="activity-dot" style={{ background: stateColor[row.state] }} aria-hidden="true" />
        <Text span fw={row.state === 'doing' ? 600 : 400} c={row.state === 'done' ? 'dimmed' : undefined}>{row.title}</Text>
        <Text span size="xs" c="dimmed" ml={8}>{stateWord[row.state]}</Text>
      </summary>
      <Text size="sm" c="dimmed" mt={4} ml={20} style={{ whiteSpace: 'pre-wrap' }}>{row.detail || 'Подробностей нет.'}</Text>
    </details>)}
  </Stack>;
}

const kinds = [
  { key: 'said', label: 'сказала', color: '#12b886' }, { key: 'heard', label: 'услышала', color: '#4c6ef5' },
  { key: 'posted', label: 'опубликовала', color: '#7950f2' }, { key: 'drew', label: 'нарисовала', color: '#fd7e14' },
] as const;

// Twenty-four bars, one per hour, stacked by what she did; empty hours stay as thin ticks.
export function HourRibbon({ rows, peak, mode = 'hours' }: { rows: Ribbon[]; peak: number; mode?: 'hours' | 'days' }) {
  const slot = mode === 'hours' ? 28 : 96; const bar = mode === 'hours' ? 20 : 64;
  const width = rows.length * slot; const height = 84;
  return <div>
    <svg viewBox={`0 0 ${width} ${height + 20}`} role="img" width="100%" aria-label={mode === 'hours' ? 'Активность по часам за последние сутки' : 'Активность по дням за неделю'}>
      {rows.map((row, index) => {
        let top = height;
        const x = index * slot + 4;
        const stacks = kinds.map((kind) => {
          const size = (row[kind.key] / peak) * (height - 4);
          top -= size;
          return size > 0 ? <rect key={kind.key} x={x} y={top} width={bar} height={size} rx={4} fill={kind.color}><title>{`${kind.label}: ${row[kind.key]}`}</title></rect> : null;
        });
        const moment = new Date(row.hour);
        const label = mode === 'hours' ? `${String(moment.getHours()).padStart(2, '0')}:00` : moment.toLocaleDateString('ru-RU', { weekday: 'short', day: 'numeric' });
        return <g key={row.hour}>
          {row.total === 0 && <rect x={x} y={height - 3} width={bar} height={3} rx={1.5} fill="#e9e6f2" />}
          {stacks}
          {(mode === 'days' || index % 6 === 0) && <text x={x} y={height + 15} fontSize="11" fill="#8a84a0">{label}</text>}
        </g>;
      })}
    </svg>
    <Group gap="md" mt={4}>{kinds.map((kind) => <Group key={kind.key} gap={5}>
      <span className="activity-dot" style={{ background: kind.color }} aria-hidden="true" /><Text size="xs" c="dimmed">{kind.label}</Text></Group>)}</Group>
  </div>;
}

const dots = (n: number) => (n > 0 ? n : 0);
export function FunnelBar({ funnel }: { funnel: Funnel }) {
  const parts = [
    { label: 'предложено', value: dots(funnel.proposed), color: '#a5b4fc' }, { label: 'принято', value: dots(funnel.accepted), color: '#4c6ef5' },
    { label: 'пережито', value: dots(funnel.lived), color: '#12b886' }, { label: 'остановлено', value: dots(funnel.stopped), color: '#ced4da' },
  ];
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  return <Stack gap={8}>
    <div className="funnel" role="img" aria-label={parts.map((part) => `${part.label}: ${part.value}`).join(', ')}>
      {total === 0 ? <span style={{ flex: 1, background: '#eeecf5' }} /> :
        parts.filter((part) => part.value > 0).map((part) => <span key={part.label} style={{ flex: part.value, background: part.color }} />)}
    </div>
    <Group gap="lg">{parts.map((part) => <div key={part.label}>
      <Text fw={600} size="lg" lh={1}>{part.value}</Text><Text size="xs" c="dimmed">{part.label}</Text></div>)}</Group>
  </Stack>;
}

// Each trait is a bar of 0..1 with its last real movement (an applied change), never a guess.
export function TraitBars({ rows, now }: { rows: Trait[]; now: number }) {
  return <Stack gap={10}>{rows.map((row) => <div key={row.key}>
    <Group justify="space-between" gap="xs" wrap="nowrap">
      <Text size="sm">{row.label}</Text>
      <Text size="xs" c={row.delta === null ? 'dimmed' : row.delta > 0 ? 'teal' : 'orange'}>
        {row.delta === null ? '' : `${row.delta > 0 ? '▲ +' : '▼ '}${row.delta.toFixed(2)} · ${ago(row.when, now)}`}</Text>
    </Group>
    <div className="trait" role="img" aria-label={`${row.label}: ${Math.round(row.value * 100)} из 100`}>
      <span style={{ width: `${Math.round(row.value * 100)}%` }} /></div>
  </div>)}</Stack>;
}

function Thumb({ api, scope, picture, onOpen }: { api: PanelApi; scope: Scope; picture: NowPicture; onOpen: (src: string) => void }) {
  const query = useQuery({
    queryKey: [...projectionKey('harness-picture', scope), picture.id], queryFn: ({ signal }) => api.harnessPicture(scope, picture.id, signal),
    staleTime: Infinity, retry: 1,
  });
  if (query.isPending) return <Skeleton h={110} w={150} radius="md" />;
  if (!query.data) return <div className="thumb-empty"><Text size="xs" c="dimmed">не загрузилась</Text></div>;
  const src = `data:${query.data.media_type};base64,${query.data.data}`;
  return <button type="button" className="thumb" onClick={() => onOpen(src)} aria-label={`Открыть: ${picture.caption || picture.id}`}>
    <Image src={src} alt={picture.caption || 'Картинка'} h={110} w={150} fit="cover" radius="md" />
  </button>;
}

const kindWord = { art: 'нарисовала', shot: 'снимок' } as const;

// Her shelf: drawings and game shots together, newest first, however old they are.
export function PictureStrip({ api, scope, pictures, now }: { api: PanelApi; scope: Scope; pictures: NowPicture[]; now: number }) {
  const [open, setOpen] = useState<{ src: string; caption: string; at: string } | null>(null);
  return <>
    <div className="strip">{pictures.map((picture) => <figure key={picture.id} style={{ margin: 0 }}>
      <Thumb api={api} scope={scope} picture={picture} onOpen={(src) => setOpen({ src, caption: picture.caption ?? '', at: picture.at })} />
      <Text size="xs" c="dimmed" mt={4} w={150} lineClamp={2}>{picture.caption || ' '}</Text>
      <Text size="xs" c="dimmed">{kindWord[picture.kind]} · {ago(picture.at, now)}</Text>
    </figure>)}</div>
    <Modal opened={!!open} onClose={() => setOpen(null)} size="xl" centered title={open?.caption || 'Картинка'}>
      {open && <Image src={open.src} alt={open.caption || 'Картинка'} radius="md" />}
    </Modal>
  </>;
}

const abilityColor = { on: '#12b886', idle: '#4c6ef5', off: '#ced4da' } as const;
const abilityWord = { on: 'работает', idle: 'подключено, ждёт', off: 'не подключено' } as const;

// What she can do, as a row of chips; the tools behind each one open on a click.
export function Abilities({ rows }: { rows: NowAbility[] }) {
  return <>
    <Group gap={8}>{rows.map((row) => <span key={row.title} className={`ability ${row.state}`} title={`${row.title}: ${abilityWord[row.state]}`}>
      <span className="activity-dot" style={{ background: abilityColor[row.state], marginRight: 6 }} aria-hidden="true" />{row.title}</span>)}</Group>
    <details className="soft-details">
      <summary><Text span size="sm" c="dimmed">Инструменты подробно</Text></summary>
      <Stack gap={8} mt={8}>{rows.map((row) => <div key={row.title}>
        <Text size="sm" fw={500}>{row.title} <Text span size="xs" c="dimmed">· {abilityWord[row.state]}</Text></Text>
        <Text size="xs" c="dimmed">{row.detail}</Text>
        {!!row.tools?.length && <Text size="xs" c="dimmed">Инструменты: {row.tools.join(', ')}</Text>}
      </div>)}</Stack>
    </details>
  </>;
}
