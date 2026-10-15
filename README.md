# AI Agent Platform — рабочее пространство агентов

Независимая платформа для настраиваемых AI-персонажей, многослойной памяти
и подключаемых инструментов, сред и каналов общения. Minecraft, голос,
Live2D, эфиры и медиапроизводство — применения общего рабочего пространства.

**Статус: базовый план 1.0 от 2026-10-13; M0-01–M0-06, M1-01–M1-06, M2-01–M2-07, M3-01–M3-04, M5-01–M5-02 интегрированы.**
Проверены среда, каркас Windows/Linux, Compose, RU recall, безопасный bootstrap,
контрактные схемы/OpenAPI/TS, SQLite repositories/миграции и внутренний supervisor
с direct STOP/pause/resume и durable jobs/Redis. Модули профилей персонажей и памяти/recall
и социальная память проверены на изолированной SQLite; их подключение к runtime остаётся отдельной работой.
HTTP управления/auth/service identity и WebSocket events проверены в M1-05.
Фоновая M3 composition (SQLite/jobs/GM/reflection/usage/mock-lab) проверена:
[отчёт](docs/implementation-reports/M3-runtime-integration.md).
Приватный mock HTTP-диалог подключён и проверен на подготовленной SQLite: [отчёт](docs/implementation-reports/Integration-dialogue.md).
Ручные решения по развитию/предпросмотр и компенсирующий откат подключены:
[приёмка](docs/implementation-reports/Integration-development-controls.md).
Production local host init/serve с optional собранной панелью и mock smoke на
Windows/Linux: [инструкция](docs/local-host-setup.md),
[combined review](docs/implementation-reports/Integration-local-host-review.md).
Явный init --operator-controls готовит ручные решения и TEST→WORK настройки:
[приёмка](docs/implementation-reports/Integration-host-controls.md).
Ограниченный private API pilot для роли character:
[подготовка и границы](docs/implementation-reports/Integration-live-api.md).
Повседневный private pilot: запуск панели через `Start-Panel.cmd`, ограниченная
история разговора после restart и явный суточный предел API:
[инструкция](docs/local-host-setup.md),
[связность](docs/implementation-reports/Integration-dialogue-continuity.md),
[эксплуатация](docs/implementation-reports/Integration-daily-local-use.md).
Локальные retention/daily backup и закрытый restore gate подключаются явной
командой enable-recovery: [инструкция](docs/local-host-setup.md),
[приёмка и ограничения](docs/implementation-reports/Integration-host-recovery.md).
Остальные live роли и owner actions остаются следующими стыками; исходный
порядок согласования — [сверка стыков](docs/implementation-reports/M4-M5-integration-readiness.md).
Отчёты: [M0-01](docs/implementation-reports/M0-01.md),
[M0-02](docs/implementation-reports/M0-02.md),
[M0-03](docs/implementation-reports/M0-03.md),
[M0-04](docs/implementation-reports/M0-04.md),
[M0-05](docs/implementation-reports/M0-05.md),
[M0-06](docs/implementation-reports/M0-06.md),
[M1-01](docs/implementation-reports/M1-01.md),
[M1-02](docs/implementation-reports/M1-02.md),
[M1-03](docs/implementation-reports/M1-03.md),
[M1-04](docs/implementation-reports/M1-04.md),
[M1-05](docs/implementation-reports/M1-05.md),
[M1-06](docs/implementation-reports/M1-06.md),
[M2-01](docs/implementation-reports/M2-01.md),
[M2-02](docs/implementation-reports/M2-02.md),
[M2-03](docs/implementation-reports/M2-03.md),
[M2-04](docs/implementation-reports/M2-04.md),
[M2-05](docs/implementation-reports/M2-05.md),
[M2-06](docs/implementation-reports/M2-06.md),
[M2-07](docs/implementation-reports/M2-07.md),
[M3-01](docs/implementation-reports/M3-01.md),
[M3-02](docs/implementation-reports/M3-02.md),
[M3-03](docs/implementation-reports/M3-03.md),
[M3-04](docs/implementation-reports/M3-04.md),
[M5-01](docs/implementation-reports/M5-01.md),
[M5-02](docs/implementation-reports/M5-02.md),
[M4-02](docs/implementation-reports/M4-02.md) (review, UI-only),
[M4-04](docs/implementation-reports/M4-04.md) (review, UI-only),
[M4-07](docs/implementation-reports/M4-07.md) (review, UI-only),
[M4-08](docs/implementation-reports/M4-08.md) (review, UI-only),
[M4-05](docs/implementation-reports/M4-05.md) (review, UI-only),
[M4-06](docs/implementation-reports/M4-06.md) (review, UI-only),
[M4-03](docs/implementation-reports/M4-03.md) (review, UI-only),
[M5-03](docs/implementation-reports/M5-03.md) (review),
[M5-04 подготовка](docs/implementation-reports/M5-04-prep.md) (in progress, без реального прогона).

Начинать чтение с [итога планирования](docs/25-preimplementation-readiness.md)
и [списка задач](docs/24-mvp-implementation-plan.md). Для разработчиков:
[AGENTS.md](AGENTS.md), [CLAUDE.md](CLAUDE.md),
[параллельная работа в ветках](docs/27-coding-agents-workflow.md).

## Основа проекта

- Первый MVP: персонаж, текстовый диалог, память/отношения, развитие и семь
  областей локальной веб-панели. Один workspace/активный экземпляр, несколько
  сохранённых профилей; рабочая и тестовая биография изолированы.
- Личность и правила развития — версионируемые данные. Убеждения, привычки,
  интересы и отношения меняются по проверенному опыту в пределах профиля;
  существенный канон/ценности меняет оператор. Внутренний GM предлагает
  заметные возможности без собственного голоса и предрешённых исходов.
- У каждой области состояния один владелец. LLM выбирает смысл; контроллер
  поведения удерживает задачи, исполнители обеспечивают движение/звук/аватар.
  STOP, права, сроки, версии и ограничения работают независимо от LLM.
- Публичный выход проверяет другая модель по точной редакции. Правовые цели:
  ЕС, США, СНГ и РФ; точная применимость/площадки проверяются до реального
  выпуска. Текстовая модерация не гарантирует безопасность всего OBS-сигнала.
- Основная платформа — Docker Compose: core с панелью, worker, Redis Streams.
  Задания/outbox в БД, bounded retries и сверка результатов; Windows-приложения
  подключаются нативными адаптерами. Панель публикуется только на loopback.
- Выбранный стек: Python 3.14/FastAPI/Pydantic, TypeScript/React/Vite;
  Markdown + SQLite, FTS5/sqlite-vec, FastEmbed CPU. MemPalace — сравнительный
  адаптер recall, LightRAG — будущая база знаний. Exact versions и resource
  measurements фиксирует M0; выбор не является уже испытанной сборкой.
- LLM — OpenAI-совместимый агрегатор, streaming, модели по ролям. Endpoint/ключи
  только локально в `.env`. Отдельный dashboard: ручные цены/версии, tokens,
  attempts, используемые модели и неизвестные расходы. Модели/цены не выбираем
  за пользователя; ориентир первых текстовых испытаний — $20/месяц.
- Полные разговоры — 30 дней, затем значимые выдержки/сводки. Биография,
  отношения и незавершённые обещания сохраняются длительно; удаление работает
  независимо от срока, включая производные данные и восстановление копий.
- Люди/зрители/комментаторы имеют подтверждённые IDs и историю встреч/тем;
  совпадение ника не объединяет аккаунты. Пересказ опыта не удваивает основания.
- STT и TTS независимо поддерживают local/API. Minecraft — Fabric и управление
  персонажем оператора на чужих серверах; навигация и finite stuck recovery
  отделены от LLM. Позже VTS, Twitch/YouTube, Telegram и отдельный монтаж.
- Проект независим от NetTyan. Публичный Git содержит документацию и затем
  код с вымышленными примерами; реальные profiles/memory/media/env локальны.

## Документы

| Документ | Содержание |
| --- | --- |
| [01. Требования](docs/01-requirements.md) | R01–R26 и границы проекта |
| [02. Архитектура](docs/02-architecture.md) | Ядро, события, владельцы, исполнители |
| [03. Персонаж и память](docs/03-character-memory.md) | Слои, опыт, RAG, отношения и сроки |
| [04. Интеграции](docs/04-integrations.md) | Игровой мост, звук, аватар и площадки |
| [05. Панель и медиа](docs/05-panel-media.md) | Сквозной пользовательский цикл и будущие медиа |
| [06. Этапы и проверка](docs/06-roadmap-validation.md) | Общая дорожная карта |
| [07. Решения](docs/07-decisions.md) | Подтверждённые/делегированные решения и будущие входные данные |
| [08. Гейммастер](docs/08-game-master.md) | Внутренние линии и возможности |
| [09. Контроль развития](docs/09-development-oversight.md) | Вкладка, основания, управление и откат |
| [10. Модерация](docs/10-moderation.md) | Публичные шлюзы, независимая LLM и политики |
| [11. Рабочее пространство](docs/11-agent-workspace.md) | Профили, инструменты, permissions и ресурсы |
| [12. Исследование памяти](docs/12-memory-research.md) | MemPalace, LightRAG и альтернативы |
| [13. Поведение и внимание](docs/13-behavior-attention.md) | Мотивы, задачи, инициатива и спокойные периоды |
| [14. Предварительный аудит](docs/14-planning-audit.md) | Найденные уточнения, закрытые финальным пакетом |
| [15. Docker и очереди](docs/15-docker-queues.md) | Streams/outbox/lease/recovery |
| [16. LLM и расходы](docs/16-llm-dashboard.md) | Роли, streaming, ставки и учёт |
| [17. Контракты и состояния](docs/17-module-contracts-state.md) | Входы/результаты, владельцы и переходы |
| [18. Модель данных](docs/18-data-model.md) | Сущности, источники, версии и связи |
| [19. API](docs/19-api-contracts.md) | Поля, чтение/команды, events/streaming/STOP |
| [20. Выбранный стек и recall](docs/20-stack-recall-selection.md) | Базовый выбор, ресурсы, сравнение и fallback |
| [21. Экраны MVP](docs/21-mvp-panel-spec.md) | Семь областей, формы и состояния интерфейса |
| [22. Тестовый персонаж](docs/22-reference-persona.md) | Заменяемый профиль, границы/темп и рубрика качества |
| [23. Структура и эксплуатация](docs/23-project-delivery-operations.md) | Каталоги, configuration, auth, budgets, lifecycle/runbook |
| [24. Каталог задач](docs/24-mvp-implementation-plan.md) | M0–M5/F1–F7, dependencies, результаты и приёмка |
| [25. Готовность плана](docs/25-preimplementation-readiness.md) | Финальный аудит покрытия и граница до кода |
| [26. Будущие интеграции](docs/26-future-integrations.md) | Voice/game/VTS/stream/TG/editing contracts и gates |
| [27. Работа кодинговых агентов](docs/27-coding-agents-workflow.md) | Codex/Claude Code, ветки, владение и интеграция |

## Следующий этап

M1-01–M1-06 приняты: contracts/storage, supervisor/direct STOP, durable jobs,
операторский HTTP/WS/service access и bounded telemetry/resource scheduler.
M1-06 опубликована перед M1-05 по поручению оператора. Блок M2 ведёт второй агент;
его бизнес-модули и отчёты этой задачей не менялись. Все зависимости M2-04 закрыты.

После обновления main выполнить uv sync --locked (общий recall suite использует
--extra recall), затем python scripts/check_combined.py своим Python3.14.
Добавлен required WebSocket backend websockets17.2; остальные lock versions сохранены.
Локальные .env/session/worker bindings через Git не передаются; создание нового
binding и trusted owner composition описаны в [access README](apps/core/src/ai_agent_core/access/README.md).
Без настройки доступа и владельцев bootstrap остаётся STOPPED/fail closed.

Совместная версия M1 + M2-01–03: Windows510 / Linux511 tests passed,
ruff/types77/import/export/lock checks passed. 78 access cases также прошли
в CPU1/RAM768 MiB isolated Linux container без OOM; actual HTTP/WS Upgrade/STOP,
scope/CSRF/Origin/session revocation и SQLite owner paths проверены.
4 Redis fixture cases пропущены; дополнительный Windows skip — POSIX FIFO.
6 legacy WS deprecation warnings учтены; обновление backend требует framing/backpressure пробы.
Точный VPS venv, реальные provider calls, полный dialogue/worker loop и cold image
build остаются соответствующим этапам. Работа — по назначениям [doc24](docs/24-mvp-implementation-plan.md).


### Основа панели M4-01 — 2026-10-14

Оболочка, operator login/session, shared API/WS/cache и independent STOP
подготовлены в task/m4-01-panel-shell; приёмка **UI-only / review**.
Windows/Linux client23/build и Edge11 browser cases прошли; полный
backend cycle M2/M3 и остальные экраны ещё требуют интеграционной проверки.
Порты: apps/panel/README.md; приёмка: docs/implementation-reports/M4-01.md.
Shared panel shell/API client остаются у M4-01; будущие screen owners
подключают свой экран через эти порты, не создают альтернативные клиенты.
