# Список задач и порядок реализации

Дата: 2026-10-13. Планирование завершено; реализация начинается по явному назначению задач.
Текущее состояние и файловые зоны назначенных задач приведены ниже.
Остальные задачи не разрешены одним фактом наличия этого каталога.
Как назначать/объединять задачи — [руководство](27-coding-agents-workflow.md).

Каждая строка — самостоятельный brief: ID, зависимости, результат/зона файлов,
приёмка. При назначении добавляются исполнитель, base commit, branch/worktree
и исключённые файлы. «Зависит от» означает объединённый и проверенный результат,
а не только наличие незавершённой ветки другого агента.

## Назначения и интеграция

Обновлено координатором 2026-10-13 по явному согласованию оператора.
Неперечисленные задачи сохраняют статус `planned`.

| ID | Статус | Исполнитель / ветка | База / результат |
| --- | --- | --- | --- |
| M0-01 | integrated | Codex / task/m0-01-environment-audit | 4872a17; отчёт принят оператором, fast-forward в main, combined diff/check пройдены |
| M0-02 | integrated | Codex / task/m0-02-project-scaffold | 4280cb1; Windows/Linux clean lock install, lint/types/7 tests/build прошли; combined revision совпадает |
| M0-03 | integrated | Codex / task/m0-03-storage-recall-probe | 1d797bf; load/crash/restart/delete/rebuild/RU/RAM/digests прошли; combined 9 tests passed |
| M0-04 | integrated | Codex / task/m0-04-compose-foundation | be20427; health/persistence/recreate/loopback/auth/resources прошли; combined config/lint/types passed |
| M0-05 | integrated | Codex / task/m0-05-recall-baseline | cffe60f; 60 RU/1k/10k gates прошли, combined Linux 21 tests/lint passed |
| M0-06 | integrated | Codex / task/m0-06-safe-startup | 19f7862; safe config/STOPPED/source image hashes passed, combined Linux 55 tests/types/lint passed |
| M1-01 | integrated | Codex / task/m1-01-contracts | 43d8687 + eb321c3; schemas/OpenAPI/TS, Windows 119 и combined main Linux 133 tests/types/lint passed |
| M1-02 | integrated | Codex / task/m1-02-storage | 04b454c + 4807f8f; SQLite head 0002, 40 storage tests; combined main Windows 171 / equivalent-source Linux 173, lint/types/export/hash checks passed |
| M1-03 | integrated | Codex / task/m1-03-supervisor | add6ebd + 5c82995; 42 control cases, combined main Windows 213 / equivalent-source Linux 215; types/lint/export/hash and installed no-dev image passed; cold Docker build ограничена metadata cache |
| M1-04 | integrated | Codex / task/m1-04-jobs | 8e16ff8 + 864f84a + 8ec1058; head 0003, Windows 247 / Linux 253, real Redis/restart/lease/crash/STOP tests, types/lint/export/hash passed |
| M1-05 | integrated | Codex / task/m1-05-access | 6081bdf/957a9c6 + a69f404/585b73d/62a8fb7; Windows510/Linux511, 78 auth cases, real HTTP/WS/SQLite/STOP, strict types/lint/export/lock; core768 MiB no OOM |
| M1-06 | integrated | Codex / task/m1-06-telemetry-scheduler | ebc32bf; 47 operations cases, combined Windows429 / Linux430, bounded buffers/reserved budgets/UNKNOWN/actual SQLite retry, types/lint/export passed; M1-05 также интегрирована |
| M4-01 | review | Codex / task/m4-01-panel-shell | b0c5c71 + 0838d43; UI-only: Windows/Linux23 client tests/build, Edge11; full backend cycle не принят |
| M2-01 | integrated | Claude Code / codex/m2-01-persona-profile | 88ed27e..5b8ff96, fast-forward в main от 542da6e; 79 persona tests, combined 244 passed + 2 известных env-failure (нет метаданных пакета в venv без `uv sync`, те же на чистом дереве), lint/types/imports passed; **runtime-интеграция не проверена** |
| M2-02 | integrated | Claude Code / codex/m2-02-memory-recall | 4a84304, fast-forward в main от 7abe59c; 36 memory tests, combined 280 passed + 2 известных env-failure, lint/types/imports passed; FTS Recall@8 0.8125 на 1k/10k = baseline M0-05; **embedder FastEmbed и runtime-интеграция не проверены** |
| M2-03 | integrated | Claude Code / codex/m2-03-social-memory | 9563493..fdeec25, fast-forward в main от 094149e; 21 social tests, combined 301 passed + 2 известных env-failure, lint/types/imports passed; **runtime-интеграция не проверена**, слияние людей не реализовано |
| M2-04 | integrated | Claude Code / codex/m2-04-llm-adapter | 2bd70cc..aae3938, fast-forward в main от a614c36; 94 llm tests (mock, MockTransport, loopback SSE), combined 474 passed + 4 известных env-failure (метаданные пакета в venv), lint/types/imports passed; **реальный агрегатор и runtime-интеграция не проверены** |
| M2-05 | integrated | Claude Code / codex/m2-05-usage-ledger | e21ff97..18a627b, fast-forward в main от 8256a8a; 39 usage tests (реальная SQLite), combined 513 passed + 4 известных env-failure (метаданные пакета в venv), lint/types/imports passed; **сверка с биллингом агрегатора и runtime-интеграция не проверены** |
| M2-06 | integrated | Claude Code / codex/m2-06-dialogue-controller | fast-forward в main после M1-05 (841b52c); 57 dialogue tests (реальная SQLite memory/recall/social, шлюз M2-04 mock), combined 573 passed + известные env-failure (метаданные пакета в venv), lint/types/imports passed; **реальный провайдер и runtime-интеграция не проверены** |
| M2-07 | integrated | Claude Code / codex/m2-07-moderation-gate | fast-forward в main; 41 moderation tests (реальная SQLite, mock-модератор), combined 614 passed + 4 известных env-failure, lint/types/imports passed; **реальная модель-модератор, качество классификации и runtime не проверены** |
| M3-01 | integrated | Claude Code / codex/m3-01-extraction | fast-forward в main от a50b2e3; 21 extraction tests (реальная SQLite, mock-экстрактор), combined 635 passed + 4 известных env-failure, lint/types/imports passed; **качество реальной модели-экстрактора и runtime не проверены** |
| M3-02 | integrated | Claude Code / codex/m3-02-gamemaster | fast-forward в main от a32cdd4; 22 gamemaster tests (реальная SQLite, mock-модель), combined 657 passed + 4 известных env-failure, lint/types/imports passed; **реальная модель-гейммастер, качество и runtime не проверены** |
| M3-03 | integrated | Claude Code / codex/m3-03-development | fast-forward в main от 16581c0; 34 development tests (реальная SQLite persona+memory, mock-рефлексия), combined 691 passed + 4 известных env-failure, lint/types/imports passed; **реальная модель-рефлексия и проявление изменений не проверены** |
| M3-04 | integrated | Claude Code / codex/m3-04-test-runner | fast-forward в main от a3b48cf; 32 laboratory tests (12 сценариев на реальной SQLite с mock-моделью, replay по seed, изоляция, перенос настроек), combined 731 passed + 4 известных env-failure, lint/types/imports passed; **оценка качества на реальной модели не проводилась; исходный relationship gap закрыт coordinator review M3-review-integration, runtime gates остаются открыты** |
| M5-01 | integrated | Claude Code / codex/m5-01-retention | fast-forward в main от 6755f43; 26 retention tests (реальная SQLite/FTS5/sqlite-vec, виртуальное время, проверка байтов БД/WAL/индекса), мутации 44 защит без незакрытых; lint/types/imports passed; **runtime-интеграция, Windows, контейнер, backup (M5-02) не проверены**; [отчёт](implementation-reports/M5-01.md) |
| M5-02 | integrated | Claude Code / codex/m5-02-backup | fast-forward в main от 263430e; 37 backup tests (реальные файлы SQLite/индекса/ledger, консистентный снимок при активной записи, restore старой копии + независимый ledger с проверкой байт БД/WAL/индекса), мутации 40 защит без выживших; lint/types/imports passed; **runtime-интеграция, Windows, контейнер, kill -9 между закрытием и переключением не проверены**; [отчёт](implementation-reports/M5-02.md) |

M0-02 владеет: root pyproject.toml/uv.lock/.python-version, apps/core,
apps/worker, apps/panel (manifest/lock/build/tooling/bootstrap),
packages (только каркас, без contract schemas), scripts, tests/tooling,
LICENSE/THIRD_PARTY_NOTICES.md и своим implementation report.
Отдельный worktree; manifest/lock owner один — исполнитель M0-02.
Исключены: Compose/deploy, migrations, schema/API/business contracts,
реализация памяти/LLM/STOP/UI экранов. Последующие назначения отражены в таблице выше. M0-02 проверяет установку/минимальную сборку, без моделей,
платных API и запуска полного runtime. Общие статусы обновляет координатор.

M0-03 владеет tests/probes/m0_03 и своим implementation report; отдельный
worktree task/m0-03-storage-recall-probe. Исключены manifests/locks, contracts,
migrations, Compose/deploy и application packages. Разрешены локальная
публичная MiniLM ONNX загрузка в изолированный cache и одна synthetic Docker
проба с 2 CPU threads/batch 4/лимитом 1,5 ГиБ; без LLM/игры/публикации.
M0-05 corpus/quality benchmark не входит в M0-03.

M0-04 владеет root .dockerignore (allowlist build context), deploy
(Compose/Docker build/resource/health scripts),
tests/probes/m0_04 и своим report. Исключены manifests/locks, migrations,
contracts/application logic, config M0-06 и общий UI client. Разрешён один
локальный synthetic Compose stack core/worker/Redis с отдельным project name
и named volumes; published port только loopback core. Реальные LLM/игра/эфир
не разрешены. Runtime entrypoints остаются остановленным scaffold; настоящий
supervisor/jobs/access будут в M1.

M0-05 владеет tests/fixtures/recall, tests/probes/m0_05 и своим report.
Отдельный worktree task/m0-05-recall-baseline; 60 synthetic RU queries,
1k/10k corpus, offline MiniLM/FTS5/sqlite-vec probe. Разрешено read-only
повторное использование public pinned model cache M0-03; отдельный Linux
volume, CPU 2/1536 MiB/batch 4. Исключены application schemas, manifests,
locks, Compose, contracts, migrations и реальная LLM/публикация.

M0-06 владеет config, root .env.example, apps/core и apps/worker bootstrap/settings,
packages/adapters/src/ai_agent_adapters/configuration.py (общий config adapter),
deploy/core.py и deploy/worker.py (только wiring проверенного bootstrap),
deploy/Dockerfile (только forced rebuild ai-agent-platform при uv cache),
tests/probes/m0_06 и своим report. Отдельный worktree task/m0-06-safe-startup.
Исключены manifests/locks, contracts, migrations, Compose/resource files, base images и UI client.
Разрешены offline validation/bootstrap checks на synthetic profiles;
LLM/игра/публичный выход не разрешены. Supervisor/STOP implementation — M1.

M1-01 владеет packages/contracts (Python schemas/converters, offline OpenAPI,
детерминированный TypeScript/schema export), tests/contracts и своим report.
База назначения 9ddabcc; отдельный worktree task/m1-01-contracts. Исключены
application/runtime handlers, access implementation, migrations, manifests/locks,
Compose/deploy и общий UI client. Проверки только synthetic/offline; не запускать
LLM/игру/публичный выход. Схемы API не предоставляют права и не открывают listener.
M1-02 владеет packages/application/src/ai_agent_application/storage.py
(repository/UoW ports), packages/adapters/src/ai_agent_adapters/storage,
migrations (единственная Alembic history 0001 → 0002), tests/storage и своим
report. База назначения c587409; ветка task/m1-02-storage. Исключены contracts,
manifests/locks, Compose/deploy, UI/access/supervisor/job dispatcher и recall.
Разрешены synthetic SQLite tests, isolated offline Linux named volume;
рабочая DB/profiles/env, LLM, игра и публичный выход не используются.
M1-02 интегрирована fast-forward; head 0002 — принятая база следующей миграции.
Packaging request из отчёта: перед runtime wiring назначить одного владельца
.dockerignore/deploy/Dockerfile для включения migrations только в core image.
Это не изменение схемы/контрактов; текущий Compose/runtime ещё не использует DB adapter.

M1-03 владеет packages/application/src/ai_agent_application/control,
packages/adapters/src/ai_agent_adapters/control, apps/core bootstrap wiring,
tests/control и своим report. Дополнительно назначены .dockerignore и
deploy/Dockerfile только для упаковки принятой migrations history в core image.
База назначения d9af647; ветка task/m1-03-supervisor, отдельный checkout.
Исключены contracts, migrations, Compose, access/HTTP handlers,
job dispatcher, UI и реальные внешние адаптеры. Разрешены synthetic SQLite,
offline Linux checks и одна локальная image packaging probe с отдельными данными.
STOP не ждёт DB/Redis/LLM; restart и сон не возобновляют старые допуски.
Дополнение координатора после installed-image probe: M1-03 назначен единственным
владельцем pyproject.toml/uv.lock только для SQLAlchemy asyncio extra. Runtime
без dev dependencies не содержал greenlet, хотя SQLite async adapter требует его.
Версии остальных пакетов не обновлять; lock и no-dev image проверить отдельно.
M1-03 интегрирована fast-forward, combined main проверки прошли. Migrations
packaging request M1-02 закрыт; schema head не менялся. Admission resolve/finish
и direct STOP — принятые внутренние ports, не HTTP authorization. До длинных
jobs назначить supervisor-owner heartbeat/refresh; full cold core/worker build
по новому lock остаётся отдельной пробой при подготовленном cache.

M1-04 владеет packages/application/src/ai_agent_application/jobs,
packages/adapters/src/ai_agent_adapters/jobs и broker, tests/jobs и своим report.
Назначены узкие shared changes: supervisor.refresh для продления исходного
admission без восстановления отозванного допуска; единственная миграция 0003
для scheduling/attempt/reconciliation metadata и storage/migrations.py HEAD.
Интерфейс refresh принят отдельным коммитом до реализации потребителя.
Также назначен tests/storage/test_storage.py только для ожидаемого single head
0003; остальные storage acceptance не менялись. Contracts, manifests,
locks, Compose, HTTP/access/UI и весь блок M2 исключены: M2 ведёт второй агент.
Зависимости M1-02/M0-04/M1-03 интегрированы. Только synthetic SQLite/Redis
проверки, отдельные данные/volumes, один тяжёлый runtime; без live LLM/API.
M1-04 интегрирована fast-forward, combined main проверки прошли; source hashes
совпадают с финальной Linux квитанцией. Приняты JobOwner/OutboxDispatcher,
WorkerAuthority/Policy ports, claim acquired flag, verify/renew/cancel receipt и
metadata-only settle_unknown. Реальная auth/composition — M1-05, runtime loops/
ресурсный scheduler — M1-06. Вторая расходящаяся migration history не допускается;
следующий номер после 0003 назначает координатор. Блок M2 остаётся за вторым агентом.

M1-06 интегрирована перед M1-05 по прямому поручению оператора; база a48ae0f.
Владеет packages/application/src/ai_agent_application/telemetry и scheduling,
tests/operations, своим отчётом. Узкие coordinator shared changes:
apps/core/src/ai_agent_core/bootstrap.py для закрытой runtime-композиции и
scripts/check_combined.py только для включения нового acceptance набора.
Contracts, migrations, manifests/lock, Compose, API/auth, M2 feature development
исключены. Используются принятые Supervisor/Admission/ResourceTicket и
ExecutionCommand/Capability; scheduler даёт бюджет, не заменяет Policy/JobOwner.
Компоненты/пулы/лимиты конфигурируются доверенной композицией. Неизвестный
результат не освобождает ресурс без фактической квитанции; restart не start.
Только синтетические bounded проверки Windows/Linux; без live API/LLM/игры.
Приняты ports/README и ebc32bf; integrated main source/lock hashes совпали с
финальной квитанцией Linux. Network access/authentication — M1-05; реальные
provider/worker IO не проверены. M1-05 впоследствии интегрирована; итог ниже.

M2-01 владеет packages/application/src/ai_agent_application/persona,
packages/adapters/src/ai_agent_adapters/persona, tests/persona и своим report.
База 542da6e; задача выдана оператором напрямую, исполнитель сдал её в ветке
codex/m2-01-persona-profile, координатор принял fast-forward. Принято решение
координатора: формат Markdown `ai-agent-persona-profile/1` (README модуля)
утверждён как формат профиля MVP; снимок профиля = неизменяемая версия Profile,
`accepted_revision_id = rev-<sha256>`; persona.edit (серверные черновики) не входит —
для него потребуется новый тип записи и следующая миграция отдельным назначением;
номер 0003 уже принят для jobs (M1-04), не использовать его повторно.
Открытые запросы из отчёта, адресованные владельцу core bootstrap: регистрация
`register_persona_extensions` в общем ExtensionRegistry, grants persona-владельцу
и reader_access оператору/Readiness. Потребители (Readiness M1-03, M2-06) проверяют
версию профиля при допуске; хот-релоада профиля нет. M2-06 теперь ждёт M2-02/03/04.

M2-02 владеет packages/application/src/ai_agent_application/memory,
packages/adapters/src/ai_agent_adapters/recall, tests/memory и своим report; зона
подтверждена координатором. Решения: производный индекс FTS5/sqlite-vec живёт в
отдельном SQLite-файле вне Alembic; recall перепроверяет каждый фрагмент через
repository для цели доступа; дедуп по исходному опыту только через эпизоды, не по тексту.
Открытые запросы из отчёта: (1) storage API для чтения/очистки просроченных (TTL) Source
владельцем — M1-02-владелец или M5-01; (2) отдельная задача на FastEmbed-адаптер и упаковку
extra `recall` в core/worker image; (3) переключение синхронизации индекса на outbox после
M1-04; (4) grants memory-owner и reader_access в core bootstrap.

M2-03 владеет packages/application/src/ai_agent_application/social, tests/social и своим
report; зона подтверждена координатором. Решения: идентичность только по подтверждённой
паре platform_id+account_id в области (ник, текст и тот же ID на другой площадке/сервере людей
не объединяют); резерв вопроса — атомарный через версию темы; обещание создаётся только по
DELIVERED реплике; точные обязательства читаются полным сканом без recall. Открытые запросы:
(1) storage-запрос по семантическим ключам/полям вместо сканирования — владелец storage; следующая миграция после 0003 только по назначению;
(2) отдельное назначение на подтверждённое слияние/разделение людей; (3) grants social-владельцу
(SOCIAL, MEMORY read, CHANNEL read) и RecordAccess на Source/Delivery в core bootstrap.
M1-04 теперь интегрирована вместе с M2; её dependency для M2-04 закрыта.
M1-06 теперь интегрирована; все зависимости M2-04 (M1-03/04/06) закрыты.
Блоки M2 (M2-01..M2-07) и M3 (M3-01..M3-04) интегрированы; M5-01 и M5-02 интегрированы (M5-03 ждёт M4).

M5-01 владеет packages/application/.../retention, packages/adapters/.../retention, tests/retention и своим report. Решения: порядок ledger → блокировка → tombstone → индекс/кэши → физическое стирание → purged; независимый ledger в отдельном файле (restore gate `apply_ledger`, применять до запуска агента); физический слой работает рядом с owner API, потому что storage скрывает просроченное даже от владельца; зависимые записи других владельцев не стираются. Открытые запросы (миграция агрегатов расхода, retention-API storage, версионный tombstone, composition, DerivedPurger для кэшей/экспорта) — в отчёте.

M5-02 владеет packages/application/.../backup, packages/adapters/.../backup, tests/backup и своим report (малая правка в своём ledger M5-01). Решения: снимок через backup API SQLite; копия не допускается без того же ledger с не меньшим счётчиком; restore только при STOPPED, через staging (ledger → sweep → индекс → заморозка outbox → проверки), живая БД меняется последней, замещённые файлы остаются в replaced/; агент не запускается. Открытые запросы (composition, jobs-владелец: OUTCOME_UNKNOWN для операций после копии, M4-07 preview) — в отчёте.

M2-04 владеет packages/application/src/ai_agent_application/llm,
packages/adapters/src/ai_agent_adapters/llm, tests/llm и своим report; зона подтверждена
координатором. Решения: шлюз не знает про Job/Redis/scheduler — доверенный исполнитель держит claim
и BudgetLease и передаёт Admission; журнал попыток и тарифы остаются за M2-05 (шлюз отдаёт
AttemptReport в AttemptSink); результат — только целый строгий JSON после завершающего события.
Открытые запросы из отчёта: (1) регистрация `register_llm_extensions` и настроек `AI_PLATFORM_*`
в core bootstrap; (2) проверка реального агрегатора оператором (usage при обрыве, параметр длины).

M2-05 владеет packages/application/src/ai_agent_application/usage, tests/usage и своим report; зона
подтверждена координатором. Решения: одна попытка — одна запись (id из attempt_id), поздний usage
заменяет, не складывается; тариф версионируется, попытка запоминает версию, пересчёт только явный;
неизвестный usage/ставка/тариф — unknown/partial, не 0. Открытые запросы из отчёта: (1) storage-запрос
по полям (период/роль/модель) и политика хранения агрегатов; (2) хук «перед вызовом» шлюза для записи
running и composition AttemptSink→ledger, grants Owner.LLM.

M2-06 владеет packages/application/src/ai_agent_application/dialogue и tests/dialogue; M2-07 —
packages/application/src/ai_agent_application/moderation и tests/moderation; зоны подтверждены
координатором. Решения M2-06: контроллер выдаёт черновик и предложение действия, не публикует и не
исполняет; обещание, вопрос и память — по факту доставки (`settle`); инициатива из точного скана
обязательств и лимитов doc22. Решения M2-07: проверка другой моделью по точной редакции,
недоступность/невалидность = отказ, выпуск только через `release` с привязкой к hash, аудитории, каналам,
контексту, версии политики и поколению управления. Открытые запросы: composition (grants, ControlView,
ProposalSink, PrivateStream, роль moderator с другой моделью, Publisher), читаемый ключ темы в social/storage,
запись Utterance.moderation_status владельцем gateway, реальные политики и испытания классификатора,
уникальные basename тестов (tests/access/test_service.py ↔ tests/persona/test_service.py).

## M0. Воспроизводимая основа и технические пробы

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M0-01 | Разрешение начать реализацию | Инвентарь Docker/WSL, runtime, лицензий; безопасные локальные настройки | Проверены целевые версии/ресурс; нет секретов в отчёте; никаких игровых/публичных воздействий |
| M0-02 | M0-01 | Каркас каталогов, uv/npm manifests и lockfiles, базовый lint/types/test; packages/apps/scripts | Чистая установка из lockfile и минимальная сборка на целевой среде; единственный владелец manifest |
| M0-03 | M0-02 | Проба SQLite/FTS5/sqlite-vec/FastEmbed; tests/probes, отчёт версий | Load/restart/delete/rebuild проходят; MiniLM на RU; измерены RAM/latency, recorded image/model digests |
| M0-04 | M0-02 | Три Compose-сервиса, named volumes, health/ресурс; deploy | Перезапуск сохраняет данные; published ports только loopback core, Redis не открыт |
| M0-05 | M0-03 | Recall-корпус/разметка; tests/fixtures/recall | 60 запросов, 1k/10k chunks, provenance/scope/deletion; baseline соответствует doc20 либо оформлена замена |
| M0-06 | M0-04 | Safe startup/config validation и редактируемый пример env; config/apps | Нет dump secret; невалидный профиль объяснён; startup всегда остановлен |

M0-03 и M0-04 могут идти параллельно после M0-02 с разными файлами/временными
ресурсами. M0-05 не требует реальной LLM для измерения retrieval; генерационное
качество будет проверено позднее. Необязательный MemPalace/E5-пилот отдельный
task, если baseline прошёл; обязательный, если требуется замена.

## M1. Контракты, владельцы и устойчивость

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M1-01 | M0-06 | Contract schemas/converters/OpenAPI, command/result/errors/scope; packages/contracts | doc17–19 реализованы; version/idempotency/size проверены; type export стабилен |
| M1-02 | M1-01, M0-03 | DB repositories и линейная миграция logical model; migrations/storage | Транзакционные версии/уникальности/права; empty→current и previous→current; нет shared DB writes worker |
| M1-03 | M1-01, M1-02 | Supervisor/state owner, direct STOP, separate pause/resume; application/control | STOP при зависшем LLM/Redis/DB; stale epochs не коммитят воздействия; restart без autorun |
| M1-04 | M1-02, M0-04 | Job/outbox dispatcher, lease/retry/reconciliation; adapters/broker/application/jobs | Crash до/после commit/ACK, broker loss, reclaim, duplicate и bounded retries проходят |
| M1-05 | M1-01, M0-06 | Operator auth/session/CSRF/Origin и service identity; apps/core/access | Чужое чтение/команды/ws запрещены; STOP независим от DB; credentials не в API/logs |
| M1-06 | M1-03, M1-04 | Bounded telemetry/health/trace, resource scheduler | Очередь/буферы конечны; known/unknown результат различим; секреты/полные prompt не логируются |

После M1-02 возможно параллельное выполнение M1-03/M1-04/M1-05 при заранее
выданных interfaces. Supervisor и job owner не создаются в двух альтернативных
реализациях. Изменения общей схемы проходят владельца контрактов/миграций.

## M2. Личность, память и модельный диалог

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M2-01 | M1-02 | Persona/profile lifecycle, Markdown import/snapshots; persona adapter | Новый профиль не копирует память; invalid edits/version conflict; public reference profile заменяем |
| M2-02 | M1-02, M0-05 | Source/episode/archive lifecycle + recall adapter | Access перед контекстом, дедуп по исходному опыту, FTS fallback, происхождение/конфликты |
| M2-03 | M1-02 | People/encounters/relationships/topics/promises | Раздельные platform IDs, резервы вопросов, повторная встреча; точные обязательства без top-k |
| M2-04 | M1-03, M1-04, M1-06 | LLM provider adapter, roles, private streaming/mock; adapters/llm | Full/broken stream, cancellation, finite retry, malformed result, configurable endpoint/model |
| M2-05 | M2-04, M1-02 | Usage/attempt ledger, tariffs and safe projections | Duplicate receipt ≠ новая попытка; missing usage/rate ≠ 0; model/rate versions, cached/reasoning не удвоены |
| M2-06 | M2-01, M2-02, M2-03, M2-04, M1-03 | Dialogue/context/attention task controller | Multi-session память, обязательства/инициатива, partial JSON не выполняет tool, STOP closes output |
| M2-07 | M2-04, M1-03 | Independent moderation model/policies, exact-revision public draft gate | Недоступность/отказ блокируют simulated public output; исправленная редакция проверяется заново |

M2-01/02/03/04 идут независимо после M1 gate, если файловые зоны разделены.
Реальные модельные пробы требуют локального агрегатора и выбранных оператором
role models; до этого mock показывает только корректность протокола.

## M3. Опыт, развитие и лаборатория

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M3-01 | M2-06, M2-02 | Extraction/evidence proposals and consolidation | Чужое сообщение/наблюдение/вывод разделены, источник проверен, self-summary не даёт независимого доказательства |
| M3-02 | M2-06, M2-07 | Internal GM/arcs/opportunities/seed policy | Нет своего голоса/forced outcomes; cooldown/границы/отказы, опыт только по фактическому исходу |
| M3-03 | M3-01, M2-01, M2-03 | Reflection/development validator/history/rollback | Bounded changes и expected_version; identity требует оператора; pause/STOP, preview compensating rollback |
| M3-04 | M3-02, M3-03, M1-04 | Test runner/virtual clock/scenario report | Изоляция work/test, seed replay, сценарии doc22; перенос только выбранных настроек |

## M4. Семь областей панели

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M4-01 | M1-01, M1-05 | Panel shell/login/API client/ws/query cache; apps/panel/shared | Constant STOP, selected vs active persona, test label, origin/session, reconnect без повторного model call |
| M4-02 | M4-01, M2-06 | Dialogue/current task/inputs | Send/save/stream/final/error; версионные вводные, STOP во время генерации |
| M4-03 | M4-01, M2-01, M2-03 | Persona + people/encounters/topics | Form validation/diff/conflict, identities не смешиваются, state отдельно от канона |
| M4-04 | M4-01, M2-02, M5-01 | Memory/search/source/correct/delete/export | Provenance/TTL/deleted состояния, preview области, нет hidden raw archive exposure |
| M4-05 | M4-01, M3-03, M3-02 | Development four views/change card | Основания/manifestation/границы/откат, pause GM независимо |
| M4-06 | M4-01, M3-04 | Laboratory | Каталог/steps/clock/report, explicit real API, isolated biography |
| M4-07 | M4-01, M1-06, M5-02 | Workspace/operations/rights/backup restore | Partial failure/STOPPING/OUTCOME_UNKNOWN, explicit resume, recovery preview |
| M4-08 | M4-01, M2-05 | LLM dashboard | Roles/rates/tokens/currencies/attempts, missing data, historical repricing preview |

M4-01 можно начать после M1, пока backend M2/M3 готовится, со стабильными
fixtures. Экранная задача не меняет API по своей инициативе. До готового
backend приёмка отмечается как UI-only, задача не объявляется интегрированной.

## M5. Данные, восстановление и приёмка

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M5-01 | M2-02, M2-03, M2-05 | Retention/delete/tombstone/caches/metadata lifecycle | 30/14/90-day policy virtual time; удалённое не выдаётся до физической очистки; версии не хранят raw forever |
| M5-02 | M5-01, M1-04 | Backup/restore/migration recovery tools | 7 copies/pre-migration; consistent snapshot; restore с последующим deletion ledger; agent stopped |
| M5-03 | M1–M4, M5-02 | CI synthetic suite/browser scenarios | Вход→диалог→restart→повторная встреча→развитие→удаление→STOP; no secrets/external publication |
| M5-04 | M5-03 | Local real-model quality/usage/resource report | 20 сценариев × 3, doc22 rubric; recall и resource goals отдельно; budget/unknown usage reported |
| M5-05 | M5-04 | Operator acceptance + release notes/runbook | Нет открытых критичных scope/STOP/data failures; ограничения модели/железа названы; MVP text ready |

M5-01 начинается после M2, параллельно M3 и части M4. M5-03 зависит от всех
перечисленных задач M1–M4, не от названия этапа как условного события.

## Контрольные ворота

| Gate | Доказательство | До чего нельзя переходить |
| --- | --- | --- |
| G0 / M0 | Exact environment + successful probes + locks/digests | Нельзя объявлять стек воспроизводимым по документации upstream |
| G1 / M1 | Contracts/access/STOP/jobs fail scenarios | Нельзя давать реальные публичные/физические полномочия |
| G2 / M2–M3 | Source/social/multi-session/development isolation | Нельзя называть память/развитие устойчивыми по одному чату |
| G3 / M4–M5 | Full panel/recovery/quality/resource acceptance | Нельзя объявлять текстовый MVP готовым без интеграционного цикла |

Task report: изменённые файлы, принятые решения, команды/результаты проверок,
невыполненные проверки/причина, риск/следующий шаг. Правка плана допустима
при новых данных; крупная смена архитектуры получает запись D в doc07.
Само выполнение задачи не разрешает публикации, реальный эфир или новые права.

## Последующие эпики

Они запланированы, но не входят в приёмку первого текстового MVP.
Подробные границы/проверки — [интеграционная спецификация](26-future-integrations.md).

| ID | Зависит от | Результат | Проверка готовности |
| --- | --- | --- | --- |
| F1-01 | G3 | Audio capture/playback/router/watchdog Windows | Stable IDs, mute/STOP, no echo self-loop, VB-CABLE route |
| F1-02 | F1-01 | Local/API STT adapters | RU/noise/VAD, finite buffering, attribution/cancel, no raw audio retained by default |
| F1-03 | F1-01 | Local/API TTS adapters/voice profile | Voice rights, playback timing, pre-output moderation, all 4 STT/TTS combinations |
| F2-01 | G3, exact game versions | Fabric bridge observation/input/resource ownership | Local/private world first; client data provenance, disconnect/watchdog/manual takeover |
| F2-02 | F2-01 | Navigation/follow/exploration/stuck recovery | Obstacles/recovery cap/moving target, no per-tick LLM, not loop jumping |
| F2-03 | F2-02, F1-02, F1-03 | Minecraft chat/voice encounters | Stable server/world IDs, platform rules, topic dedup, voice/chat one utterance |
| F3-01 | F1-03, actual rig | VTS mapping/lip sync/emotion controller | Auth/reconnect, actual playback sync, defaults on cancel, expression arbitration |
| F4-01 | G3, channel facts | Twitch/YouTube inputs and social memory | Verified platform IDs, edits/deletes/dedup/rate limits, actor not answering every message |
| F4-02 | F1–F3, F4-01, public-release gate | OBS/stream coordinator/public safety path | Private rehearsal→explicit live; foreign audio/video policy, emergency scene/mute, two-platform route proven |
| F5-01 | F2-01, G3 | Screenshot/capture/catalog/post drafts | Interesting-place consent/rules, factual caption tied to source, privacy/media metadata |
| F5-02 | F5-01, public-release gate | Telegram manual release + comments | Exact draft/media moderation and owner approval; receipt/unknown result; same commenter memory |
| F6-01 | F5-01, recordings enabled | Media ingest/markers/EDL/project | Protected source references/timebases, no auto-edit biography, isolated render resources |
| F6-02 | F6-01 | Editing UI + FFmpeg export worker | Non-destructive trim/sequence/subtitles/audio levels/preview/export; cancel/error/quota; no public upload implied |
| F7-01 | G3, explicit need | Multiple active agents/resource arbitration/shared knowledge | Isolation, ownership leases/fairness; not confused with multiple developer agents |

Только координатор обновляет общий статус task list на integration branch.
Неперечисленные в таблице назначений задачи остаются `planned`.

M1-05 назначена по прямому поручению оператора после публикации M1-06.
База a614c36, отдельный checkout/ветка task/m1-05-access. Зона: apps/core/access,
tests/access и свой отчёт; узкая composition-правка core/bootstrap, auth binding
в adapters/configuration и включение тестов в scripts/check_combined.py.
Координатор выдаёт additive canonical auth/session и service claim/lease response
contracts (api/catalogue/export/generated + их tests): сначала контракт и проверки,
затем runtime. Исключены M2 business modules, migrations, dependencies/lockfiles,
Compose, UI, платные вызовы и публичный доступ. Зависимости M1-01/M0-06 приняты.

Уточнение зоны M1-05 координатором: optional RAM validity callback в принятом
ControlAuthority/проверке Supervisor для отзыва ожидающих команд при logout;
минимальный проброс auth bindings/host port в deploy/compose.yaml и описание
в .env.example. Формат команд, owner state machine, ресурсы и зависимости
не переопределяются. Проверки только synthetic bindings; реальные .env не читаются.

Транспортная часть M1-05 также владеет deploy/core.py только для bounded
WebSocket frame/queue/backlog/keepalive; worker/Redis listener и ресурсы не меняются.
Проверка optional ControlAuthority callback перенесена в tests/access,
чтобы исторический standalone M1-03 runner не зависел от новых test helpers.

Обнаруженный M1-05 integration blocker: uvicorn без optional WebSocket backend
не может обслужить canonical events endpoint. Координатор выдаёт минимальные
pyproject.toml/uv.lock/THIRD_PARTY_NOTICES изменения только для websockets,
без обновления остальных зависимостей; проверить реальный HTTP Upgrade и framing.

Итог M1-05: integrated62a8fb7, зависимые M2/M4 могут использовать auth/service
contracts и ports. Final source135 совпадает с проверенным checkout после
нормализации line endings; root smoke45 passed, включая настоящий WS Upgrade.
Windows510/Linux511 и core-only78/768 MiB passed; memory.peak264081408 bytes,
exit0/OOMfalse. uv lock --check --offline passed; только websockets17.2 добавлен.
Перед использованием обновить свой venv по frozen lock. Credentials локальны.
Функциональные границы и незапущенные части — implementation-reports/M1-05.md;
runtime не начинает работу без readiness/owner/grants. M2 принадлежит второму агенту.


M4-01 назначена оператором 2026-10-14 после M1. Отдельный checkout/ветка
task/m4-01-panel-shell, база 841b52c. Владелец apps/panel (shared shell/client,
manifest scripts при необходимости, без обновления dependency versions),
tests/panel и своего implementation report. Координатор владеет только
назначением/итогом doc24 и указателями README/doc25/doc27 при сдаче.
Зависимости M1-01/M1-05 integrated. Canonical generated contracts только
импортируются; contract schemas/exports, migrations, Python lock, Compose,
core/access и business M2–M5 не меняются. Приёмка UI-only до полного
backend; отдельная проверка реальных login/session/STOP/events M1-05.
Synthetic fixtures/локальный loopback browser допустимы; без настоящих
env/профилей/памяти, LLM API, публикации и новых прав.


Уточнение shared зоны M4-01 координатором: deploy/Dockerfile только panel
build stage/layout и COPY его dist; scripts/verify-linux.ps1 только allowlist/
panel recipe для canonical generated contracts и frontend tests. Runtime
Python stages/Compose/migrations/locks не меняются. Причина: импорт shared
contracts.ts требует прежнего repo layout в build stage, иначе Docker build
не воспроизводит проверенную native сборку. Один владелец — M4-01.


Для той же packaging-правки M4-01 назначена .dockerignore: добавить только
allowlist packages/contracts/generated/contracts.ts. Остальные исключения
секретов/данных и runtime context остаются прежними.


Итог M4-01 2026-10-14: foundation готова к публикации, приёмка UI-only/review.
Настоящие M1 login/session/STOP/WS и отдельные synthetic UI projections
проверены. Linux panel Docker stage/npm lock/lint/types/23 tests/build passed;
Edge11 cases, responsive/keyboard passed. Новые M2/M3 модули из3a5d690
сохранены, независимый review следует отдельно; их runtime не объявляется
проверенным этим task. До полной backend приёмки не переводить M4-01
в integrated и не считать M4-02–08 реализованными.


## Координаторская проверка M3 — 2026-10-14

По поручению оператора: M3-review-integration, база6755f43, отдельная ветка
codex/m3-review-integration. Зона: независимые tests/m3_review, application
extraction/gamemaster/development/laboratory и их acceptance; один владелец
узких storage guard/lifecycle ports и связанных тестов; scripts/check_combined.py
для включения новых принятых наборов. При подтверждённой необходимости:
additive metadata contracts/exports для GM и закрытая core composition
(регистрация/owner grants/Scope+Supervisor bindings), без автозапуска.
Никаких UI/M4 новых экранов, deps/locks/Compose, реальных API/моделей/каналов.
Узкие social changes только для закрытия обхода проверенного M3 relationship
development, без новой социальной feature/передачи владения БД.
Цель: воспроизвести M3 acceptance Windows/Linux, исправить найденные ошибки,
разобрать каждый coordinator request в своём отчёте M3-review-integration.md.
Наличие M3 модулей в main не означает принятого runtime wiring/качества модели.

Для M3-review назначен tests/conftest.py только для закрытия test SQLite pools
до остановки короткого asyncio.run в M3/independent review; остальные наборы
не затрагиваются. Базовая Windows проверка:117 passed, но15 thread warnings.


M3-review закрывает relationship gap расширением M3-03: доверенные aspect
limits, независимые подтверждённые эпизоды, история StateChange владельца
SOCIAL и atomic Relationship+history write; узкая зона social/service.py,
schemas/relationship policy и соответствующие fixtures/tests. Contract
StateChange.owner остаётся динамическим: UoW уже проверяет точного владельца.
Имена/лимиты тестового персонажа — только данные laboratory/profile.


Подтверждённый composition blocker: MemoryService.reader_access не
передавался создаваемым Episode. M3-review назначена узкая правка
memory/service.py: применять уже доверенно переданные ACL при записи
Episode; Claim сохраняет отдельный private ACL. Значения grants не расширяются
автоматически; public/API
purposes по-прежнему закрыты без своих RecordAccess flags.


M3-review проверяет также API-transfer purpose всех данных контекста M3:
узкий optional purpose в MemoryService.get_source/EvidenceReader, чтение
Source/Claim/Episode/Profile/CharacterState/StoryArc с api_transfer перед
моделью; apply/история остаются private. Trusted fixture grants/ACL
уточняются явно, production права автоматически не расширяются.

Координаторское уточнение проверок M3: закрытие test pools также для
dialogue/moderation; tests/llm — только portable synthetic SSE handshake.
Итоги и оставшиеся runtime gates: [review](implementation-reports/M3-review-integration.md).

Coordinator compatibility при поступлении M5-02 во время M3-review: узкая
правка backup/directory.py — fsync требует writable file handle на Windows.
POSIX mode не подтверждает Windows ACL; native backup до ACL-проверки не принят.

Та же узкая compatibility зона: tests/backup/backup_support.py закрывает
live recall index вместе с authority перед заменой файлов. Windows:
36 backup tests passed, 1 POSIX-mode/ACL test failed; native Windows backup не принят.

Итог review M3: Linux combined 948 passed/4 Redis skipped; ruff, mypy144,
imports и canonical exports passed. Windows M3/affected suites passed;
M5-02 native ACL test остаётся failure (36 passed/1 failed backup suite).
Runtime composition и реальные модели M3 отдельно не приняты; подробности
и ответы на все запросы — в [отчёте](implementation-reports/M3-review-integration.md).
