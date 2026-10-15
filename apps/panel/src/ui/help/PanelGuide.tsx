import { Button, Group, Paper, Stack, Text } from '@mantine/core';
import { useState } from 'react';
import { Link, useLocation } from 'react-router';
import type { RunSession, Scope } from '../../shared/contracts.ts';
import { samePartition } from '../../shared/scope.ts';

const areas = [
  { path: 'dialogue', title: 'Диалог', text: 'Начните с короткого сообщения. Статус под репликой показывает, сохранена ли она и готов ли ответ.' },
  { path: 'persona', title: 'Персонаж', text: 'Здесь канон, стиль и границы развития. Правьте профиль здесь, когда хотите изменить основу персонажа.' },
  { path: 'memory', title: 'Память и люди', text: 'Смотрите обработку разговоров, сводки и факты. Выберите запись, чтобы проверить источник, исправить её или подготовить удаление.' },
  { path: 'development', title: 'Сюжет и развитие', text: 'Смотрите предложения и изменения характера, их основания и решения. Предложение ещё не означает применённое изменение.' },
  { path: 'laboratory', title: 'Лаборатория', text: 'Проверяйте сценарии в отдельной тестовой памяти. Для обычного общения откройте «Диалог» в рабочей области.' },
  { path: 'workspace', title: 'Рабочее пространство', text: 'Проверяйте незавершённые операции, права и резервные копии. При неизвестном исходе сначала обновите состояние операции.' },
  { path: 'llm', title: 'LLM и расходы', text: 'Проверяйте модели, попытки и токены. Неизвестная стоимость означает отсутствие данных о цене, а не бесплатный вызов.' },
];

export function PanelGuide({ scope, run, fresh }: { scope: Scope; run?: RunSession; fresh: boolean }) {
  const { pathname } = useLocation();
  const [expanded, setExpanded] = useState(false);
  const area = areas.find((item) => pathname === '/' + item.path) ?? areas[0];
  let next: string;
  if (!fresh || !run) next = 'Сначала дождитесь состояния ядра или нажмите «Обновить данные». По устаревшим данным нельзя определить, работает ли агент.';
  else if (!samePartition(run.scope, scope)) next = 'Сейчас открыта другая область. Кнопка START / STOP вверху управляет активным агентом, указанным в шапке. Для разговора выберите его рабочую область.';
  else if (run.state === 'STOPPED') next = 'Для разговора нажмите START вверху. Когда появится «Работает», отправьте сообщение в «Диалоге». После перезапуска START нужно нажать снова.';
  else if (run.state === 'FAULTED') next = 'Откройте «Рабочее пространство» и проверьте причину сбоя и незавершённые операции. После устранения причины запуск потребует отдельного действия.';
  else if (run.state === 'STOPPING') next = 'Дождитесь подтверждения «Остановлен». Если остановка затянулась, проверьте незавершённые операции в «Рабочем пространстве».';
  else if (run.state === 'STARTING') next = 'Запуск ещё идёт. Дождитесь «Работает». STOP доступен и во время запуска.';
  else if (run.activity !== 'OPEN' || run.autonomy !== 'autonomous') next = 'Агент активен в ограниченном режиме. Проверьте режим в «Рабочем пространстве»: обычная автоматическая обработка памяти требует автономного открытого запуска.';
  else next = 'Можно общаться в «Диалоге». STOP вверху останавливает агента и фоновые модельные задачи. Закрытие вкладки само по себе агента не останавливает.';
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
