# M3 composition

Точка сборки: `app.create_m3_app(settings, store, registry, ...)`.
Это обычный FastAPI ASGI app; его lifespan открывает подготовленную SQLite,
восстанавливает Supervisor и обслуживает bounded очередь M3. Импорт ничего
не запускает. Один core и один внутренний executor на workspace.

## Подготовка хостом

- Создать registry через `new_registry()`; передать тот же объект SQLiteStore.
- Передать явные BootstrapSettings, scopes без run/session/agent, provider factory.
  Первый scope — активный профиль; workspace/persona/mode должны совпасть с settings.
- Миграции, Profile/CharacterState, Workspace, GmRuntimeState, Connection,
  LLMAssignment, Capability, Permission и storage grants/record ACL готовятся
  отдельно существующими административными/owner портами. Runtime их не создаёт.
- Principal IDs owners: `core-<owner>`; оператор `operator`.
  LLMAssignment принадлежит LLM, Connection/Capability/Permission — Workspace.
  `GmRuntimeState.id = gm-runtime`, `Workspace.id = scope.workspace_id`.
- У каждого используемого owner нужны свои grants. Для ссылки на чужой Source
  нужны явные read grant и RecordAccess; для передачи модели — также api_transfer.
  Общих разрешений оператору или всем owners по умолчанию нет.
- Primary assignments по ролям: extractor, consolidator, reflection, game-master,
  story-screen. Имена env settings передаются как ссылки; значения здесь не читаются.
  Runtime default allowlist содержит только adapter `mock`.
  Fallback bindings здесь отклоняются: цепочку нельзя запускать без закрепления
  всех её версий и бюджетов. Живой provider требует отдельной настройки/приёмки.

## Разрешения и ресурсы

Capability IDs: m3.extract/consolidate/reflect/post_session/gm/laboratory;
adapter/binding m3-core, input m3.job, output m3.result, local_write,
cancellation supported, idempotency supported, health ready.
Resource IDs: background + llm; laboratory — background. Каждый demand равен 1,
lane background. Резерв публичной модерации не занимается.
`required_permissions` содержит ID единственного соответствующего Permission.

Permission закрепляет principal operator, operation_type равный capability ID,
binding m3-core, level automatic, актуальные policy_version/expiry, отсутствие
revoked_at, conditions m3.empty и limits m3.limits. max_calls 1..8 на job,
включая retries; max_output_tokens 1..2048 на вызов. Runtime не повышает level.

JobOwner хранит canonical Job/Lease/ExecutionResult, Behavior — ExecutionCommand.
Каждая генерация заново проверяет lease, scheduler, права, источники и bindings.
Attempt записывается running до provider IO и завершается отдельно от domain
допуска. STOP/пауза не теряют позднюю техническую usage-квитанцию.
Owner commits повторно проверяют закреплённые версии в той же SQLite транзакции.

## Триггеры и управление

Доверенный владелец вызывает `Runtime.submit(authority, request_id, TaskInput)`.
Идентичный request_id возвращает известный job; другой payload конфликтует.
kind выбирает существующий Extractor/Consolidator/Reflector/GameMaster/LabRunner.
post_session означает закрытый логический разговор при ещё действующем run.
После Supervisor session.end/STOP старый job не исполняется; завершившийся
разговор можно обработать только по новому явному допуску.

HTTP использует прежние login/session/Origin/CSRF и /api/v1/commands:
gm.configure + m3.gm-configure; laboratory.start + m3.task с kind laboratory;
laboratory.transfer_settings + m3.transfer с reviewed digest.
Preview переноса доступен через существующий SettingsTransfer.preview;
панель M4 подключит его представление. Перенос разрешён при STOPPED, из явно
разрешённого TEST scope в WORK; биография и CharacterState не копируются.
Partial transfer оставляет durable reconciliation_required/planned, блокирует
start и не повторяется автоматически. Оператор подтверждает сверку отдельно.

ReflectionCheckpoint сохраняет вход, validated ответ и окончание предложений.
После сбоя READY change применяется через прежний контроллер с его CAS/пределами;
APPLIED не применяется повторно. Интервал рефлексии сохраняется между экземплярами.

GM использует сохранённые intensity/rules и монотонное активное время. Пауза,
STOP, сон и downtime его не расходуют. Checkpoint раз в 5 секунд; при аварии
потеря последней доли времени консервативно продлевает cooldown.

## Лаборатория и M5

Для laboratory заранее выдать свежий TEST lab_scope и доверенный scope_factory,
возвращающий именно его. Runtime не создаёт ему grants и отклоняет повторное
использование. Scope закреплён в durable JobArguments; seed и время симуляции
позволяют воспроизводить mock. Лабораторный LLM остаётся scripted mock. Добавленные M5-04 providers/resolve
отклоняются этим M3 entry до IO; их отдельный evaluation путь требует своего
допуска/бюджета.

Передать Reports с отдельным локальным каталогом и LabRunner. Полный JSON
сохраняется с checksum/size/TTL в Artifact; Source содержит только digest.
Чтение каждый раз проверяет текущие catalogue/source rights и TTL.
Files bounded: 2 MB/report, 32 MB total по умолчанию; не более 1000 файлов.
TTL sweep удаляет также файлы после частично завершённой записи.
`Reports.purge_sources` реализует существующий M5 DerivedPurger: хост M5 добавляет
его в RetentionService.purgers. DELETE/TTL источника сначала закрывают чтение
через каталог; purger удаляет файл и TTL sidecar.

## Recovery

Перезапуск оставляет STOPPED. Старые queued jobs закрываются, running jobs и
незавершённые LLM attempts становятся unknown; повторного provider IO нет.
Не подтверждённые ресурсы сохраняются до сверки исходным владельцем; lifespan
оставляет auth/STOP доступными, но start закрыт. Фактически закончившийся executor может передать metadata-only
settle_unknown без повторного применения payload. Lease expiry не считается
доказанным прекращением удалённого вычисления.

Проверки — tests/m3_runtime; combined runner включает их вместе с исходными
наборами. Реальные модели, качество ответов, публичные каналы и платные вызовы
не входят в mock-приёмку.
