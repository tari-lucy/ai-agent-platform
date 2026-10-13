# SQLite adapter M1-02

Порты находятся в `ai_agent_application.storage`; реализация —
`SQLiteStore` / `Repository[T]` / `SQLiteUnitOfWork`. Domain не зависит от SQLite.
Один transaction принадлежит одному core owner. Роль worker отвергается;
worker также не получает core-data в Compose.

## Порядок подключения

1. Доверенный composition root задаёт путь БД, registry расширений и clock.
2. Отдельная административная процедура явно вызывает `upgrade(migrations_path)`.
   Открытие/чтение/transaction только проверяют head 0002; не создают схему.
3. Администратор задаёт Scope и Grant через `provision`. Это внутренний API:
   его нельзя экспортировать как клиентский CRUD или доверять Principal из JSON.
   Новый набор grants целиком заменяет предыдущий набор данной области.
4. Владелец использует `async with store.transaction(principal, scope, command)`.
   При `unit.replay is not None` возвращает сохранённую квитанцию.
   Иначе записывает DTO через `put` и завершает через `complete(Receipt)`.
5. Перед остановкой соединений вызывает `close`.

Для новой изменяемой записи нужна version=1 без expected_version; следующая
редакция требует expected_version=current и version=current+1. Record без
версии неизменяем. Ошибка put помечает весь unit of work как failed:
перехват исключения не разрешает commit частично записанных данных.
Без complete, при исключении или cancellation весь transaction откатывается.
Изменение, редакция, точные ключи, ACL, links, metadata outbox и receipt
сохраняются одной транзакцией BEGIN IMMEDIATE.

## Чтение и границы

Scope key включает workspace/persona/memory space/work-or-test. Новый run
не создаёт новую биографию; full scope команды остаётся частью fingerprint.
Current grant проверяет role, owner и expiry; чужому читателю дополнительно
нужна ACL конкретной записи. Private/API/public проверяются отдельно, в SQL
до LIMIT. Историческая редакция не обходит текущие права или удаление источника.
Available SourceRef, включая typed refs зарегистрированного расширения,
проверяется в транзакции записи по области/версии/правам/TTL.

При истечении сырого текста private lookup Person/Topic/Promise/Relationship
сохраняет точное состояние, если текущие права разрешают доступ. Истёкшая
SourceRef в представлении помечается expired. API/public context и прочие
производные записи требуют доступных источников. Explicit deleted Source
блокирует чтение зависимых записей, удаляет свои старые SQL payloads и
сохраняет ключи запрета повторного импорта. Полное удаление производных данных,
файлов, индексов, WAL/free pages и backup ledger — отдельный lifecycle M2/M5.

Квитанция действует receipt_retention (по умолчанию 30 суток, настраиваемо).
Истёкший известный request возвращает OUTCOME_UNKNOWN и не исполняется повторно.
Очистка ledger/outbox и dispatcher пока не реализованы. Outbox содержит только
ID/type/version/scope/time, без текста, prompt или secret bindings.

Repository не предоставляет бизнес-допусков, moderation, STOP или lease admission.
Их проверяют владельцы следующих задач. Python Principal/Grant не являются
изоляцией от произвольного вредоносного кода внутри процесса core.
