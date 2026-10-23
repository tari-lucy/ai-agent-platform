import { Button, Group, Paper, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { Link, useLocation } from 'react-router';
import type { RunSession, Scope } from '../../shared/contracts.ts';
import { samePartition } from '../../shared/scope.ts';

import { areaIntros as areas } from '../areas.ts';

export function PanelGuide({ scope, run, fresh }: { scope: Scope; run?: RunSession; fresh: boolean }) {
  const { pathname } = useLocation();
  const [expanded, setExpanded] = useState(false);
  const area = areas.find((item) => pathname === '/' + item.path) ?? areas[0];
  let next: string;
  if (!fresh || !run) next = 'Сначала дождитесь состояния ядра или нажмите «Обновить данные». По устаревшим данным нельзя определить, работает ли агент.';
  else if (!samePartition(run.scope, scope)) next = 'Сейчас открыта другая область. Кнопка START / STOP вверху управляет активным агентом, указанным в шапке. Его данные смотрите, выбрав его рабочую область.';
  else if (run.state === 'STOPPED') next = 'Агент остановлен: ничего не исполняется. Чтобы он работал и отвечал, нажмите START вверху. После перезапуска START нужно нажать снова.';
  else if (run.state === 'FAULTED') next = 'Откройте «Обзор и права» и проверьте причину сбоя и незавершённые операции. После устранения причины запуск потребует отдельного действия.';
  else if (run.state === 'STOPPING') next = 'Дождитесь подтверждения «Остановлен». Если остановка затянулась, проверьте незавершённые операции в «Обзоре и правах».';
  else if (run.state === 'STARTING') next = 'Запуск ещё идёт. Дождитесь «Работает». STOP доступен и во время запуска.';
  else if (run.activity !== 'OPEN' || run.autonomy !== 'autonomous') next = 'Агент активен в ограниченном режиме. Проверьте режим в «Обзоре и правах»: обычная автоматическая обработка памяти требует автономного открытого запуска.';
  else next = 'Агент работает. Ответьте на обращения, если они ждут вас. Пока OpenCode не подключён, временный вход для разговора — «Диалог». STOP вверху останавливает агента и фоновые модельные задачи; закрытие вкладки агента не останавливает.';
  return <Paper component="section" withBorder p="md" radius="lg" mb="lg" aria-label="Подсказки по панели">
    <Stack gap="xs">
      <Group justify="space-between" align="flex-start" gap="xs">
        <Text fw={600}>С чего начать</Text>
        <Button size="compact-xs" variant="subtle" aria-expanded={expanded} aria-controls="panel-guide-details"
          onClick={() => setExpanded(!expanded)}>{expanded ? 'Свернуть подсказки' : 'Как пользоваться панелью'}</Button>
      </Group>
      <Text size="sm">{next}</Text>
      <Text size="sm" c="dimmed"><strong>{area.title}:</strong> {area.text}</Text>
      {scope.memory_mode === 'test' && <Text size="sm" c="violet.8">Открыта тестовая память. Её записи не становятся рабочей биографией персонажа.</Text>}
      {expanded && <Stack id="panel-guide-details" gap="sm" pt="xs">
        <Text size="sm" fw={600}>Где что смотреть</Text>
        <ul className="panel-guide-links">{areas.map((item) => <li key={item.path}>
          <Link to={'/' + item.path}>{item.title}</Link><Text size="sm" component="span"> — {item.text}</Text>
        </li>)}</ul>
        <Text size="sm"><strong>Если ответ не появился:</strong> проверьте связь, статус сообщения и остаток запросов.
          При неизвестном исходе обновите состояние, прежде чем отправлять то же сообщение ещё раз.</Text>
        <Text size="sm"><strong>Если связь пропала:</strong> дождитесь переподключения или обновите данные.
          Кнопка STOP отправляет отдельный запрос; остановка подтверждена только после ответа ядра.</Text>
        <Text size="sm"><strong>Если персонаж что-то забыл:</strong> откройте «Память и люди» и проверьте цикл обработки и записи.
          Свежие разговоры обычно обрабатываются после смены суток, когда агент работает автономно.</Text>
      </Stack>}
    </Stack>
  </Paper>;
}
