# Host recovery: координаторская интеграция

Дата: 2026-10-15. Ветка codex/host-recovery-integration, база main b8428b7
(через review f492ea4); библиотеки backup a253c7e и purger a06b9a0.

## Реализовано

- Явный offline enable-recovery: новые отдельные приватные каталоги,
  SQLitePhysicalStore.install, закреплённые ledger ID и sequence floor.
  Начатая подготовка закрывает serve до завершения. Старые удаления без истории
  нельзя легализовать новым пустым ledger.
- HostLedger резервирует floor до записи удаления; потеря/подмена/откат ledger
  закрывают запуск. Ошибка во время записи отзывает runtime через supervisor.
  Config не восстанавливается поверх текущего из snapshot; floor не откатывается.
- RetentionService получает тот же ledger, основной индекс и derived purger с
  inventory открытых lab-индексов. Reports.holds — публичный безопасный inventory.
  Происхождение тел отчётов не выдумано: pending сохраняется честно.
- Lifecycle обслуживания отдельно от watchdog/LLM: retention каждые 300 секунд,
  daily snapshot один раз за текущие UTC-сутки, без replay пропущенных суток и
  START. Полный callback хэширования/копирования в отдельном потоке/event loop,
  отдельное соединение к тому же ledger. Ручные и фоновые копии сериализованы.
- Настраиваемые keep_daily=7 / admission quota=1 GiB / interval=300 в host.json.
  Код/счётчики maintenance.json не содержат текстов/секретов/endpoint.
- Restore: preview и STOPPED host-lock → staging/ledger/TTL/reindex/outbox →
  marker перед switch → отдельный preview/apply derived cleanup → повторная
  retention/reindex проверка → следующий serve только STOPPED. При ошибке
  marker остаётся. Непроверенные тела отчётов требуют явного drop-reports решения.
- Job inventory проверяет контракт/версию/scope/schedule до owner reconcile;
  malformed/orphan Job закрывает запуск. Неисполняемые остатки schedules после
  штатного retention terminal records не считаются живыми jobs.
  Старые RUNNING переходят в OUTCOME_UNKNOWN, QUEUED в CANCELLED через JobOwner.
  Новая незавершённая операция, отсутствующая в копии, блокирует её restore.
- Убрана недоказательная приёмка с неполным raw JSON Job: это теперь отрицательный
  тест отказа, положительный сценарий создаёт jobs через owners.

## Проверки

Windows, временные host/ledger/копии, только вымышленные данные, mock.
Один affected-набор после реализации: 35 passed, 1 skipped (POSIX chmod):
local_backup, local_artifacts, local_host/test_recovery, настоящий HTTP host smoke,
existing HTTP dialogue→reflection→restart. Полный combined suite не запускался.
Новые группы подключены к штатному combined runner с importlib, без его запуска.
После последнего guard — повтор только одного затронутого сквозного сценария:
1 passed; он дополнительно проверяет запрет потери новой операции после snapshot
и штатную отмену через JobOwner перед restore. Полный affected-набор не повторялся.
Ruff, strict mypy (210 файлов), import boundaries — чисто.

Проверены daily-copy после restart, потеря и rollback того же ledger ID, torn
reservation/ошибка записи, незавершённая подготовка. Сквозной owner-сценарий:
RUNNING и QUEUED → snapshot → удаление источника → restore → отказ serve до
finish → owner reconciliation, ноль повторных LLM-attempts, удалённый текст
не возвращается. Windows owner-only DACL проверяется кодом для host, ledger,
его каталога, каталога ledger copies и store; POSIX skip не выдаётся за ACL-тест.

## Ограничения

Это не полное disaster recovery после потери всего host/текущей БД. Сохраняются
config, независимый актуальный ledger, secrets; restore требует читаемого
совместимого текущего host и не откатывает API-attempts. Нет автоматического
START, внешних инструментов, миграций/пакетов/новых permissions или paid API.
Свойства SSD/CoW/шифрования не подтверждены. Replaced-файлы не удалялись.
Полное происхождение report bodies отложено; строгая pending-граница сохранена.
Рабочий host до отдельной deployment-проверки не изменялся этим acceptance.


## Дополнительная native Windows приёмка

На первой рабочей копии проверка фактических дочерних ACL выявила явные широкие
ACE и владельца Administrators, хотя root DACL был закрыт. Это не было обнаружено
POSIX skip и проверкой только корней. Созданные recovery-деревья немедленно
ограничены; host-данные и содержимое копии не удалялись.

Исправление: единый native restrict_private заменяет весь DACL owner-only,
включая явные ACE, выставляет владельца; применяется до записи в каталоги
копирования и к готовым файлам/ledger copies. Windows staging наследует уже
защищённый work-каталог; POSIX сохраняет mode 0700/0600. Ledger WAL/SHM и lock
получают ту же защиту. Копирование остаётся вне потока control watchdog.

Узкая дополнительная приёмка Windows: удаление заведомо широкой explicit ACE,
файлы настоящей daily-копии после restart, native HTTP smoke — 3 passed.
Restore owner-сценарий — 1 passed после добавления production control watchdog
в ручной fixture (sleep/lease guards не менялись). Повтор всего набора не делался.
Ruff, strict mypy (211 файлов), import boundaries — чисто.
