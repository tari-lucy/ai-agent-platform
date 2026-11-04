import { Badge, Group } from '@mantine/core';
import type { HarnessStatus } from '../shared/contracts.ts';

// doc37: the persona's live connections (avatar, voice, hearing, OBS, chats), one badge each.
const labels: Record<string, string> = {
  avatar: 'Аватар', speech: 'Голос', listening: 'Слух', obs: 'OBS', twitch: 'Twitch', youtube: 'YouTube',
};
const colors = { on: 'green', idle: 'gray', down: 'red' } as const;
const states = { on: 'на связи', idle: 'ждёт', down: 'нет связи' } as const;

export function LiveLinks({ harness }: { harness: HarnessStatus | null }) {
  const links = harness?.enabled ? harness.live ?? [] : [];
  if (links.length === 0) return null;
  return <Group gap={4} aria-label="Живые подключения" role="group">
    {links.map((link) => <Badge key={link.name} size="sm" variant="light" color={colors[link.state]}
      title={[states[link.state], link.detail].filter(Boolean).join(': ')}>{labels[link.name] ?? link.name}</Badge>)}
  </Group>;
}
