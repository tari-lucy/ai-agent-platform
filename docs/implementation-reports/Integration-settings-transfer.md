# Integration-settings-transfer — перенос выбранных настроек

Дата: 2026-10-15. Исполнитель/координатор: Codex.
Base: fb163f8081d4954fa845e22de5398176be0a0d7d.
Ветка: codex/integration-settings-transfer, отдельный checkout.
Поручение: подключить preview/confirmation/apply TEST→WORK и видимый исход,
продолжая закрывать согласованные стыки лаборатории. Без live API/новых grants.

## Результат

- Canonical schema laboratory.transfer-settings-args, DTO preview/status,
  SettingsTransferOperation.operation_id (nullable для старых records),
  bounded settings-transfers history и generated OpenAPI/JSON Schema/TS.
- POST laboratory/transfer-preview: разрешённые source TEST/target WORK
  одного workspace; конкретный набор profile/gm_intensity/story_rules_version.
  Before/after (Markdown либо настройки), изменения, блокировки, digest,
  target GM version, actor и срок пять минут. Proof сохраняет metadata, но
  before/after=null: полный профиль не дублируется в БД плана.
- Apply через существующий command route требует точный сохранённый plan
  ID/version/operator/source/keys/digest, актуальные версии и STOPPED.
  Реализация использует прежних владельцев PERSONA/GM/WORKSPACE, generic
  SQLite records и TransferJournal; новых owner/table/migration нет.
- Source API-transfer grant/ACL/version проверяются в owner transaction.
  Target Profile/CharacterState versions проверяются на входе PERSONA UoW,
  затем текущие ACL продолжают проверяться после собственных записей.
  GM обновляется через прежний CAS. Изменение/отзыв прав закрывает запись.
  Недопустимый downgrade правил/удаление project rule видны до применения.
- Профиль применяется PersonaService.apply с проверкой совместимости;
  рабочие накопленные значения CharacterState сохраняются. Источники,
  эпизоды, люди/отношения, обещания, расходы и тестовая биография не копируются.
- Между разными owners нет обещания общей атомарности. Durable planned/
  reconciliation_required хранит подтверждённые applied_keys; при сбое
  записи прогресса список — нижняя граница известных изменений. Start
  заблокирован до сверки. Повтор незавершённого operation возвращает
  OUTCOME_UNKNOWN; completed возвращается без повторных owner writes.
- Status GET/history позволяют восстановить результат после reload/ошибки.
  null означает неизвестно/недоступно, а не отсутствие эффектов. Acknowledge
  требует progress.version и отдельное подтверждение ручной сверки, не откат.
- Панель показывает выбор ключей/назначения, различия, блокировки и TTL;
  требует отдельный checkbox. Смена выбора сбрасывает preview. Истёкший план
  и неизвестный исход блокируют повтор применения; доступно чтение состояния.
  История сохраняется после reload. Старым progress без operation_id UI не
  выдумывает новую идентичность. Default deployment остаётся закрытым.

## Проверки

| Проверка | Результат | Что доказано |
| --- | --- | --- |
| Windows initial owner/laboratory | 45 passed, 39.41s | Существующие owner transfer/settings paths |
| Windows transfer HTTP final | 11 passed, 27.87s | ASGI/SQLite, полный профиль+GM+rules, replay/status/history, metadata-only proof, stale digest/selection/TTL/source, закрытый source, отзыв grant внутри owner записи, rules downgrade, partial failure/ack, гонка рабочего CharacterState |
| Linux affected | 345 passed, 162.04s | access/contracts/laboratory/persona/m3_runtime, включая 10 новых HTTP cases; последний CharacterState race дополнительно проверен Windows |
| Client pure tests | 155 passed | Точный план/CAS/selection, foreign source/blockers, impossible completed, nullable unknown, legacy identity, прежний client |
| Реальный Edge + HTTP/SQLite | 1 passed, 7.01s | Login, TEST выбор, before/after .5→.9, disabled до checkbox, apply, фактическое рабочее значение, history после reload; запросы не перехватывались |
| Static/build | passed | TypeScript, ESLint, Ruff, mypy 173 files, import boundaries, canonical exports, production build |

Linux: один cached image, network=none, read-only source mounts, synthetic
/tmp storage, 2 CPU/1536 MiB; без рабочей памяти/других контейнеров. После
финального review транзакционной защиты повторён только узкий Windows
transfer набор; full backend/browser suites не запускались.
Ошибки lint (Date.now при render), типизации SQL predicate и форматирования
исправлены без suppressions/ослабления проверок. Таймер TTL теперь effect.
Известные предупреждения: Vite chunk >500 kB; legacy WS deprecations (6).

## Приёмка и оставшиеся стыки

Scoped acceptance выполнен: реальный UI→canonical API→owners путь выбранных
настроек, явное подтверждение, stale/denied/partial поведение, recovery чтением.
Проверки используют только вымышленные данные и mock provider; платные вызовы,
секреты/.env, реальные профили, игра/каналы/эфир не использовались.
Diff/whitespace/состав файлов и отсутствие личных данных/секретов проверены.
Dependencies/lockfiles/Compose/миграции/decision registry не менялись.

Host обязан явно подготовить разрешённые scopes и минимальные operator
read+api_transfer grants/record ACL для читаемых PERSONA/GM/WORKSPACE records,
а также разрешённую видимость journal через Runtime.readers. Полномочия
автоматически не выдаются; legacy setup без ACL получит ACCESS_DENIED.
API требует существующие target Profile/CharacterState/GM; создание пустой
рабочей области — отдельный host lifecycle. Реальные operator grants/БД и
сроки реальной среды этой проверкой не принимались.

Остаются laboratory events/per-job cancel, полноценный browser lab execution
cycle, GM/development decisions/rollback, memory plans/delete/export, usage
aggregates/operations и прежний Windows backup ACL blocker. Этот отчёт не
означает полного acceptance M4/M5. Следующий связный стык — events/per-job
cancel с точными Job/Run CAS/epoch; общий STOP уже существует. Общие contracts
и main интегрирует координатор; остальные feature zones не переписывались.
