# M3-runtime-integration — техническая mock-композиция

Дата: 2026-10-14. Исполнитель/координатор: Codex.
Ветка codex/m3-runtime-integration; отдельный checkout, исходная база 8c9796d.
Свежий origin/main cebbc92 с UI-only M4 и M5-03 включён merge 7fd091a.
**Технические критерии назначенной M3 mock-интеграции проверены.**
Рабочий приватный диалог, подключение всех экранов M4, эксплуатационная сборка
M5 и качество реальных моделей этим статусом не закрываются.

## Что соединено

- create_m3_app и bootstrap lifespan подключают один Runtime к существующим
  Supervisor, ResourceScheduler, SQLiteRunJournal, SQLiteJobs/JobOwner.
  Scope и provider allowlist явные. Новый owner БД/очередь/планировщик не создавались.
- Behavior сохраняет ExecutionCommand до submit, JobOwner выдаёт/обновляет lease,
  scheduler резервирует background/LLM; moderator reserve остаётся свободным.
  Runtime исполняет существующие Extractor, Consolidator, Reflector, GameMaster,
  LabRunner; post_session является явным durable trigger.
- До каждого provider IO и результата проверяются current lease/admission,
  permissions, профиль/политика, Source rights/версии, assignment/connection pins.
  Дополнительный SQLiteStore.authorize_writes повторяет proof внутри той же
  owner-транзакции под BEGIN IMMEDIATE. Отзыв прав между генерацией и commit
  закрывает запись; technical job/usage receipts этим допуском не блокируются.
- AttemptJournal + UsageLedger.begin_attempt записывают running до provider IO.
  Поздняя квитанция после STOP сохраняется. Crash оставляет unknown; цена/токены
  не превращаются в выдуманный ноль. IDs различают job attempts, max_calls
  учитывает durable записи через retries; общий бюджет не сбрасывается.
- ReflectionCheckpoint сохраняет episode IDs/input digest, проверенный ответ,
  base versions и окончание предложений. Повторный trigger не вызывает модель;
  crash после ответа или перед apply продолжает с checkpoint. READY проходит
  прежний bounded/CAS контроллер, APPLIED не применяется дважды.
  Интервал рефлексии сохраняется между экземплярами; смена входов конфликтует.
- GmRuntimeState хранит intensity, layered rules/rules_version, active_seconds.
  Часы монотонны: пауза GM/деятельности, STOP, downtime/сон их не расходуют.
  Последние до 5 секунд до аварии могут не сохраниться: это консервативно
  продлевает cooldown, а не снимает его. Новая сессия начинает с GM paused.
- Лаборатория закрепляет свежий TEST lab_scope, seed и начало симуляции в job.
  Сценарий использует scripted mock. Полный JSON хранится в scoped каталоге
  с ArtifactRef/checksum/size/TTL; Source содержит digest, не копию отчёта.
  Reads проверяют current source/catalogue rights и TTL. Sweep удаляет TTL files,
  Reports.purge_sources соответствует M5 DerivedPurger и физически удаляет файл.
- DurableSettings переносит выбранную intensity и сам проверенный ruleset,
  а не ссылку на отсутствующую версию. Проектные правила нельзя ослабить переносом.
  Profile идёт через PersonaService; биография и CharacterState не копируются.
  Preview fingerprint включает exact настройки/версии; CAS повторяется.
  SettingsTransferOperation сохраняет applied_keys/partial reconciliation;
  незавершённый перенос блокирует start, слепого повторения нет.

HTTP подключён через прежние session/Origin/CSRF/scope проверки и узкий mutation
port: gm.configure, лабораторный mock start, согласованный settings transfer.
Direct STOP остаётся синхронным, без обращения к БД/LLM/Redis. По умолчанию
внешнему worker не выдаются capabilities. Schema/migrations/grants/Permission
автоматически не создаются; отсутствующее разрешение означает отказ.

## Проверки и критерии

| Критерий | Реальное доказательство |
| --- | --- |
| Сквозной M3 job | SQLite + JobOwner + scheduler + gateway + owner apply; reflection меняет state один раз |
| Recovery/reflection | Новый Runtime над тем же хранилищем STOPPED; повторного model call нет; READY после сбоя применяется один раз |
| STOP / revoke / поздний ответ | Нет StateChange после закрытия допуска; terminal usage сохраняется; owner transaction с отозванной Permission не открывается |
| Ограниченный фон / retry | Второй job WAITING, пока background занят; второй provider не вызывается; безопасный retry не дублирует reflection |
| Post-session | Существующие extractor/consolidator/reflector в одном job; nothing-to-extract/keep-state являются допустимыми результатами |
| GM | Persisted rules/intensity, только зарегистрированная capability, active clock/пауза и сохранённый checkpoint |
| Лаборатория | Настоящий queued mock в свежем TEST scope, ArtifactRef и полный JSON; повторное использование scope отклоняется |
| TTL/delete | Истёкший report не читается; sweep и purger физически убирают файл; scoped catalogue остаётся authority |
| Settings | Реальные durable writes intensity/rules, CAS/replay; partial failure сохраняется и требует сверки |
| HTTP | Настоящие ASGI lifespan/login/CSRF/config/start/STOP; STOP работает при недоступном storage transaction port |
| Privacy | Только вымышленные fixtures; changed-file scan и diff проверены, секреты/env/runtime данные не добавлялись |

**Windows:** tests/m3_runtime — **27 passed**, 52.25 с.
Ruff и strict mypy — **155 source files passed**.

**Linux combined на 7fd091a:** один финальный запуск scripts/check_combined.py:
Ruff, strict mypy155, import boundaries, canonical exports — passed;
**979 passed, 4 skipped, 0 failed**, 245.03 с. Включены принятые backend suites,
tests/m3_runtime и новый tests/e2e M5-03 из origin/main.
6 предупреждений — deprecations websockets/uvicorn из существующего transport
набора. 4 skips — Redis host не передавался.

Контейнер: существующий pinned check image, Python 3.14.8; сеть none,
read-only исходники, временная synthetic data, 2 CPU/1536 MiB. Модели/пакеты
не скачивались; чужие контейнеры, рабочая память и volumes не затрагивались.
Полную suite на каждой правке не повторяли: affected tests по изменённым путям,
один Linux combined перед объединением. После gate менялись только документы.

## Не проверено / оставшиеся ограничения

- Живые LLM/агрегатор, фактический billing/качество персонажа/модерации,
  игра, каналы и эфир не запускались. Default adapter allowlist — mock.
- M3 runtime поддерживает primary bindings. Fallback chain не исполняется
  без отдельного закрепления всех assignments и общего бюджета.
- deploy/core.py всё ещё выбирает закрытый scaffold. Готовая explicit factory
  требует подготовленных owner данных и минимальных grants; рабочую память
  автоматически открывать оператору или всем owners нельзя.
- Полного настоящего HTTP цикла dialogue.message/PrivateStream/delivery нет.
  Public publisher остаётся закрыт. M4 всё ещё содержит mock-предположения
  схем/targets/CAS, перечисленные в отдельной сверке ниже.
- M5 host должен добавить Reports в RetentionService.purgers; контракт purger
  и физическое удаление проверены, общая эксплуатационная M5 сборка не объявлена
  готовой. Расписание backup/retention, native Windows backup ACL, kill -9/restore
  и незавершённые операции после restore — отдельный gate владельцев M5.
- Если прошлые ресурсы не подтверждены, restart сохраняет unknown и закрывает
  start до сверки владельцем. Lease expiry не является квитанцией прекращения.
  Auth/STOP остаются доступными; автоматического повторного эффекта нет.
- Browser suite M4 заново не запускалась: её код не менялся этой задачей,
  бизнес-путь пока mock. Нагрузку и реальные ограничения модели этот gate
  не измеряет.

## Передача следующему этапу

Инструкция сборки/портов:
[README M3 composition](../../apps/core/src/ai_agent_core/m3_composition/README.md).

По новому запросу оператора сверены отчёты M4-02..08/M5-03, исходники клиентов,
dispatcher и owner payloads. Актуальные несовместимости и ближайший порядок:
[M4-M5-integration-readiness](M4-M5-integration-readiness.md).
Сводка owner-requests актуализирована; никакие M4/M5 задачи не объявлены
закрытыми по одному наличию кода.

Ключевые commits до gate: 2fbbea0 (ownership), 45c3ba9/f943683 (contracts),
ce00126 (durable primitives), 251322d (core composition), 7fd091a (fresh main).
