// The panel controls and observes the platform. Ordinary conversation is meant for OpenCode;
// until it is connected to this person, "Диалог" stays as an explicitly temporary entry.
export const screens = [
  { path: 'workspace', group: 'control', label: 'Обзор и права', intro: 'Состояние агента, незавершённые операции, возможности, права и резервные копии. Смотрите сюда первым; действуйте, если операция в неизвестном состоянии или права пора изменить.' },
  { path: 'tickets', group: 'control', label: 'Обращения', intro: 'Вопросы, проблемы, идеи и запросы доступа от агента. Действуйте, когда обращение ждёт вашего ответа или решения по доступу.' },
  { path: 'llm', group: 'control', label: 'Расходы и лимиты', intro: 'Вызовы моделей, токены, оценка стоимости и дневные лимиты. Лимит меняйте у остановленного агента; неизвестная цена — не бесплатный вызов.' },
  { path: 'persona', group: 'person', label: 'Персонаж', intro: 'Канон, стиль и границы развития. Меняйте профиль, когда хотите изменить основу персонажа.' },
  { path: 'development', group: 'person', label: 'Сюжет и развитие', intro: 'Предложения и изменения характера с основаниями. Предложение ещё не применённое изменение — решение за вами.' },
  { path: 'memory', group: 'person', label: 'Память и люди', intro: 'Источники, сводки, люди и обещания. Проверьте источник записи, исправьте её или подготовьте удаление.' },
  { path: 'library', group: 'person', label: 'Библиотека', intro: 'Навыки, заметки и файлы агента. Новый навык остаётся экспериментальным, пока результат не подтверждён.' },
  { path: 'work', group: 'watch', label: 'Работа агента', intro: 'Поручения, собственные цели агента, ход выполнения и результаты. Разговор о работе здесь — временный вход до подключения OpenCode.' },
  { path: 'laboratory', group: 'watch', label: 'Лаборатория', intro: 'Сценарии в отдельной тестовой памяти. Открытие экрана тест не запускает.' },
  { path: 'dialogue', group: 'watch', label: 'Диалог · временно', intro: 'Временный вход для разговора, пока OpenCode не подключён к этой личности. Статус под репликой показывает, сохранена ли она и готов ли ответ.' },
] as const;
export const groups = [
  { id: 'control', title: 'Управление' },
  { id: 'person', title: 'Персонаж и память' },
  { id: 'watch', title: 'Наблюдение и диагностика' },
] as const;
export const areaIntros = screens.map(({ path, label, intro }) => ({ path, title: label, text: intro }));
