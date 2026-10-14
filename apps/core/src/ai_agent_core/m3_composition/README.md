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
gm.configure + gm.configure-args; laboratory.start + laboratory.start-args;
laboratory.transfer_settings + laboratory.transfer-settings-args с reviewed digest.
POST /api/v1/laboratory/transfer-preview возвращает canonical plan на пять минут;
панель показывает diff/blockers и требует отдельное подтверждение.
Apply несёт plan ID/version, точные source/keys/digest; target CAS = GM version.
GET settings-transfer-status/{operation_id} и settings-transfers восстанавливают
progress после ошибки/reload. Acknowledge использует CAS progress и не делает откат. Перенос разрешён при STOPPED, из явно
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

## Private mock dialogue (Integration-dialogue)

Опциональный create_m3_app(..., dialogue_index=...) подключает существующий
DialogueController/ContextBuilder/MemoryService/RecallService/SocialService.
Индекс предоставляет host; без него dialogue закрыт. Factory не создаёт grants,
профили, bindings или миграции; deploy/core.py остаётся закрытым scaffold.

Дополнительно нужны role character и capability/Permission m3.dialogue
по тем же правилам, что выше. Demand background+llm, lane foreground.
Отдельный trusted WorkKind dialogue допускает ответ оператору при observation,
но требует ACTIVE/OPEN и отзывается pause/STOP. Инструменты здесь не исполняются.
Character binding дополнительно ограничен adapter mock до provider IO.

Operator ACL/grants для private context и JSON-чтений задаются явно в своей
области: Profile/CharacterState, Source/Episode, Utterance/Delivery, jobs/usage.
Gateway также требует собственного чтения Source, чтобы сохранить ссылки
на основания. Core owner grants и RecordAccess не заменяют друг друга.
api_transfer здесь проверяется и для loopback JSON; это не разрешение
вызвать внешний provider. Никакого автоматического расширения прав нет.

dialogue.message адресуется текущему session_id. Сначала Memory owner
сохраняет Source с conversation TTL; ExecutionCommand содержит только SourceRef
и ID, без копии текста. Затем создаётся durable job, HTTP не ожидает модель.
Если между двумя owners случился отказ, повтор с тем же ID находит Source
и продолжает постановку; существующее содержимое не перезаписывается.

WS chunks предварительные; generation.terminal с saved_result_id означает
сохранённый приватный final, а не публичную модерацию или доставку.
Utterance остаётся draft/audience operator-panel; Delivery — QUEUED.
dialogue.acknowledge с ID/version/revision подтверждает получение панелью.
Только тогда создаётся Source сказанного, Episode и подтверждённая Delivery.
Это не доказательство, что человек прочитал текст. Повтор ACK не удваивает опыт.
ACK после смены run или закрытия допуска запрещён.

session.reflect перед session.end создаёт конечный job по явным episode_ids.
Рефлексия не запускается после STOP и может сохранить состояние без изменений.
Чтения/DTO/CAS и public TS builders: docs/implementation-reports/Integration-contracts.md.
Это минимальный private operator путь; публичные каналы, реальные модели,
все M4 формы и M5 эксплуатационный bootstrap требуют отдельных gates.

## Полный mock lab report через HTTP

GET /api/v1/laboratory-reports/{report_id}/content с четырьмя partition query
полями возвращает точные сохранённые JSON bytes как attachment
laboratory-report.json; ETag — quoted SHA-256. Максимум 2 000 000 bytes,
no-store/nosniff, без pathname/URL от клиента, без redirects/range/cache replay.
Требуется обычная operator session/Host/Origin и разрешённая TEST partition.
Чтение после STOP допустимо при сохранённых правах; нового запуска нет.

Downloads проверяет api_transfer для LaboratoryReport, Artifact и Source,
версию полной ArtifactRef, TTL/доступность/размер/checksum. После file IO
повторяются проверки Artifact/Source, отчёта и сессии. Нет записи/LLM/job IO.
Если Reports не передан или artifact ещё не создан — CAPABILITY_UNAVAILABLE;
неизвестный/недоступный отчёт — ACCESS_DENIED, повреждение/смена reference —
STALE_CONTEXT. Ошибки не содержат пути, тела отчёта или raw исключений.

Reports по умолчанию сохраняет Source.api_transfer_allowed=False.
Доверенный host может явно выбрать api_transfer_allowed=True для новых
mock reports и минимальные grants operator read+api_transfer к owners
laboratory/catalogue/memory, отдельные RecordAccess для отчёта/artifact/source.
Это независимые условия; endpoint не вызывает provision. Существующие
закрытые Source не открываются сменой настройки или повторным save.
Default deploy остаётся закрытым, миграции/содержимое локальной БД не меняются.

Shared PanelApi.laboratoryReportContent(scope, report, signal) возвращает Blob
после сверки TEST partition, ArtifactRef, byte limit, ETag, SHA-256,
mock/scenario ID/version. Session clear/abort удаляет ожидающий результат;
redirects/автоповторов нет. Feature owner подключает кнопку скачивания,
освобождая созданный object URL; остальные M4-06 действия пока UI-only.

## Подготовленный TEST pool и кнопка скачивания

laboratory.create / laboratory.create-args {persona_id} адресуется laboratory
из текущей partition персонажа. Нужны существующие m3.laboratory
Capability/Permission, owner LABORATORY write grant в родительской partition,
явные LabRunner/Reports и заранее включённые в runtime.scopes/session TEST
области той же workspace/persona (максимум 8 scopes вместе с рабочей).

Endpoint выбирает незарезервированную, зарегистрированную и полностью пустую
область. SQL metadata check учитывает также скрытые/удалённые записи. Он
не читает их содержимое. LaboratoryScopeReservation и receipt сохраняются
одной LABORATORY UoW; record ID/resource_id — memory_space_id ребёнка,
receipt.version=1, scope receipt — родительский scope команды. Повтор request_id
при сохранённых допусках возвращает прежнюю receipt. Конкурентные команды
не делят slot; пустой пул -> RESOURCE_BUSY. История reservation сохраняется
после закрытия SQLite connections; автоматического освобождения/повторного
использования, в том числе после неизвестного исхода, нет.

Это резервирование подготовленного пула. provision/миграции/копирование
профиля/фабрики/LLM в нём отсутствуют; новые host scopes/grants через API
не добавляются. Host заранее задаёт lab_grants и отдельный fresh index factory.
Создание профиля в зарезервированной области выполняет обычный LabRunner
только после допуска laboratory.start. Reserved execution использует точный
pinned scope без второго scope_factory; прежний явный prepared-scope путь
с проверкой factory остаётся совместим.

Для JSON/download host отдельно задаёт LabRunner.report_access,
Reports.readers/api_transfer_allowed и operator grants к laboratory/catalogue/
memory. report_access default пуст; это передача заданного ACL записи отчёта,
не автоматическое расширение прав. Без неё metadata может быть недоступна.

Панель получает catalogue у backend, laboratory.start-args формируется общим
builder из текущего RunSession и собственной версии сценария; report.version
не используется как управляющая версия. Форма только mock. Кнопка скачивания
использует проверенный PanelApi Blob, фиксированный filename; pending read
отменяется при unmount/scope/revision change, object URLs освобождаются.
Settings transfer preview/apply/status/reconciliation подключены отдельной приёмкой
Integration-settings-transfer. Подача наблюдений и per-job cancel подключены через Integration-laboratory-control.


## Laboratory control

GET laboratory-runs — operator Job API-transfer projection, с pagination и
точными parent run + lab_scope. Допуск записи наблюдения требует живого runner;
limit=32/job, text<=2000. Используется существующий lab MEMORY Source owner,
без LLM-вызова/канона; text не дублируется в artifact. Parent Job API grant/ACL,
config и lease/admission проверяются в SQL owner commit. Host должен явно
разрешить operator read+api_transfer для JOBS и видимость Jobs через readers;
runtime не меняет grants. Clock/progress после окончания могут быть null.

Stop canonical laboratory.stop-args адресует Job CAS и не использует report
version/derived lab-report operation ID. JobOwner отзывает только его token,
RUNNING требует factual executor receipt, прочие admissions остаются действующими.
Запросы чужого lab_scope/run/Job не принимаются. Одна TEST область не резервируется
двумя pending laboratory Job. Повтор execute одного job не закрывает чужой live
context. Прямой STOP/технические receipts после отзыва работают по прежней модели.
Интерактивные отчёты отмечены operator_events и не обещают seed-only replay.
