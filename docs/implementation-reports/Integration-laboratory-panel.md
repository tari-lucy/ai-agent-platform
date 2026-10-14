# Integration-laboratory-panel

Дата: 2026-10-15. Codex по прямому поручению оператора. Base 40290e3,
отдельная codex/integration-laboratory-panel. Частичная техническая private/mock
приёмка лаборатории; весь M4/M5 не объявляется integrated.

## Что соединено

- Кнопка «Скачать полный JSON» получает Blob через принятый PanelApi:
  точные bytes/checksum/размер/ETag, фиксированное имя laboratory-report.json.
  Без artifact кнопка отключена; revoke/logout/ошибка закрывают получение.
  При уходе со страницы/смене scope/revision pending request отменяется,
  object URLs и timers очищаются. Ошибочное содержимое не предлагается как файл.
- Найден и исправлен report ACL gap: LabRunner сохранял отчёт без operator ACL,
  даже если Source/Artifact были разрешены. Добавлен явный report_access,
  default пуст, применяемый тем же LABORATORY UoW. Новых grants нет.
- Форма читает backend catalogue, без локального fallback при отказе.
  Использует shared startLaboratory с RunSession.scope/version, отдельной
  scenario.version и выбранным TEST scope; старая дублирующая сборка start
  удалена. Версия старого отчёта больше не используется для управления.
  Неподключённый paid API control убран из этой mock формы.
- laboratory.create с canonical laboratory.create-args резервирует пустую
  зарегистрированную TEST область той же workspace/persona из уже разрешённого
  runtime/session пула. Проверяет m3.laboratory Permission/Capability и owner
  write grant, явные LabRunner/Reports. Не вызывает provision, scope_factory,
  модели, миграции и не копирует профиль.
- Новая immutable LaboratoryScopeReservation принадлежит LABORATORY, хранится
  в существующих generic SQLite records. Новых таблиц/миграций нет.
  Одна UoW сохраняет reservation + receipt; request replay и конкурентные
  запросы не занимают slot повторно. Scope metadata учитывает скрытые/удалённые
  records. Родительский transaction BEGIN IMMEDIATE и runtime lock защищают
  выбор/запись. Лимит — максимум 8 зарегистрированных auth scopes.
- Receipt resource_id = memory_space_id/ID reservation, scope = scope команды,
  version=1. UI показывает конкретную область для выбора в верхнем списке.
  При исчерпании пула — RESOURCE_BUSY; abandoned/unknown reservations не
  освобождаются автоматически. Reserved start использует точный pinned scope
  без повторной фабрики; legacy explicit prepared-scope путь сохранён.

## Проверки и границы доказательств

- Windows ASGI/SQLite: 6 passed, 18.66s (первые reservation cases и существующий
  настоящий lab HTTP test, теперь create → start → mock scenario → metadata/
  full JSON). Отдельный повторный лабораторный сценарий для download не добавлен.
  Последующие negative/concurrency cases включены в финальный Linux набор.
- Linux final affected: storage/contracts/laboratory/M3 runtime — 218 passed,
  115.31s. Cached Python3.14 image, network none, CPU2/RAM1536MiB, readonly source;
  tmp SQLite/индексы/отчёты изолированы. Полный backend не повторялся.
- Edge affected batch: 13 passed/1 failed (reload fixture сбрасывала выбранный
  TEST scope); исправлен выбор. Узкий retry двух UI cases passed.
- Настоящий Edge/HTTP/SQLite journey после финальных fixture исправлений:
  1 passed, 6.84s — login → durable prepare → metadata → exact JSON download →
  current Source API revoke → refusal/no download, без intercepted проекций.
  В том же journey отсутствие artifact отключает кнопку.
- Этот браузерный case скачивает заранее синтетически сохранённый отчёт.
  Настоящий lab execution проверен отдельным ASGI case выше; эти доказательства
  не выдаются за единый browser create/start/execution/download cycle.
- Pure TS152 passed; ESLint/typecheck/build passed; mypy172, Ruff,
  import boundaries и canonical exports passed.
- Повтор request ID, разные/конкурентные requests, исчерпанный pool, отсутствие
  session scope, tombstoned/hidden data, revoked Permission, отсутствие Reports/
  runner и сохранение reservation при закрытии/reopen SQLite проверены.
- Diff/состав файлов и секреты/личные данные проверены перед commit.

Первые ошибки были исправлены без ослабления условий: Scope import новой
схемы, receipt обязан ссылаться на реально записанный reservation ID; synthetic
Provenance должен быть enum. Browser clock/Permission fixture сначала ссылалась
на старые expires_at; test setup теперь читает свои Permission до сдвига времени
и явно обновляет synthetic TTL. Production TTL/lifecycle не менялись.
Первый Linux вызов остановился при collection из-за одинаковых test_contracts.py;
применён уже принятый --import-mode=importlib, после чего выполнен один affected
набор. Failed checks не удалялись ради зелёного результата.
4 известных legacy WS warnings в настоящем browser case; прежний Vite bundle
>500kB warning. Transport/bundle не менялись.

## Что осталось

- Host заранее готовит пул/register scopes, lab_grants, role/config readiness,
  отдельные индексы и явные operator report/source/artifact ACL. Endpoint только
  резервирует этот пул, не создаёт новые полномочия или scopes на лету.
  Default deploy по-прежнему закрыт; пользовательские env/БД не читались.
- Полный browser execution cycle, per-job progress/cancel и laboratory.event
  ещё не приняты; generic прямой STOP уже остаётся независимым.
- Settings transfer preview/apply UI, GM/development decisions/rollback,
  memory plans/delete/export, usage aggregates и operations — следующие стыки.
- Реальные модели/платные API/публичные каналы не использовались; нет
  installs/model downloads, dependency/lock/Compose/migration changes.
- Windows backup ACL blocker остаётся без изменений.

Второй агент должен обновиться от main и использовать canonical contracts/client.
Остальные persona/development feature zones этой задачей не менялись.
