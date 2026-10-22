# Локальный host: резервные копии и восстановление

Тонкая composition существующего M5-02 (`BackupService`) для каталога local host.
Агент и модель не запускаются. Для подготовленного host используется существующая
привязка `enable-recovery`; daily callback подключён в host maintenance.
Восстановление рабочего места через staging:
[реализация, проверки и границы](implementation-reports/R4-staged-restore.md).

## Команды

```text
python -m ai_agent_core.local_backup create --root R --store S --ledger L [--kind manual|daily]
python -m ai_agent_core.local_backup list --root R --store S
python -m ai_agent_core.local_backup verify --root R --store S --backup-id ID
python -m ai_agent_core.local_backup preview-restore --root R --store S --ledger L --backup-id ID
python -m ai_agent_core.local_backup restore --root R --store S --ledger L --backup-id ID --decision-id D --digest H
```

Каждая команда — одна строка. Все пути абсолютные.

- `--store` (копии) и `--ledger` (независимый deletion ledger) выбираются явно. Они не могут
  лежать внутри каталога host, содержать его или пересекаться друг с другом; символическая
  ссылка отклоняется. Рядом с ledger создаётся `<ledger>.copies` (копия ledger на каждую копию).
- Ledger должен уже существовать и быть настоящим ledger. Отсутствующий, пустой, чужой, подменённый
  или откатанный ledger закрывает restore; пустой ledger взамен истории не создаётся.
- `create` делает снимок через backup API SQLite (согласован с WAL) и может идти рядом с `serve`.
  В копию входят `database.sqlite`, `host.json` и, при настроенных managed files,
  `workplace-blobs.zip` по ссылкам из этого SQLite-снимка. `.env`, ключи, `operator-password.*`, логи
  и внешние каталоги не добавляются; чужие файлы в манифесте блокируют restore.
- `preview-restore` и `restore` берут OS-блокировку host на всё время: работающий `serve`
  даёт `HOST_ALREADY_RUNNING`. Состояние `STOPPED` подтверждает только эта блокировка,
  а не старая запись RunSession.

## Порядок restore

1. `preview-restore` печатает проблемы (`issue: КОД blocking|warning`), число областей,
   открытых операций, событий outbox и `digest`.
2. `restore` требует явный `--decision-id` и тот же `digest`; изменившийся preview — отказ.
3. Страховочная копия `pre_restore` → staging → ledger удалений → сроки → пересборка индекса →
   проверка целостности → переключение файлов. Сбой до переключения не меняет рабочие файлы.
   Перед переключением создаётся `RESTORE-PENDING`; прерванное переключение закрывает serve,
   даже если сохранилась читаемая БД. Восстановление не обещает атомарность группы файлов при
   отключении питания.
4. Незавершённые задания не переисполняются и остаются неизвестными; события outbox переводятся
   в `failed`. Агент после restore не запускается: новый `serve` стартует `STOPPED`.
5. Заменённые файлы остаются в `<store>/.replaced` и содержат данные до восстановления.
   Старый blob-каталог остаётся рядом с настроенным blob root под именем `.replaced-<id>`;
   это частная страховочная копия, не активные файлы и не доступная агенту область.
   Оператор стирает их сам после проверки результата. При отказе могут остаться частные
   `.restore-<id>`; они также не становятся активным каталогом автоматически.
6. `local_host finish-restore --root R` печатает отдельный preview. Повторить с его `--digest H`
   и `--decision-id D`; если нужны `--drop-reports`, решение и preview должны включать этот флаг.
   Проверяются связь с выбранной БД, файлы, ledger, индексы и jobs inventory. Только после
   успеха marker снимается; новый serve начинает STOPPED. Не удалять marker вручную.

Контексты/checkpoint и недоставленные черновики рабочих разговоров сбрасываются через
SessionBook, generation увеличивается. Доставленные сообщения и processed_sequence сохраняются.
Это не ACK и не разрешение повторить неизвестный helper: старые попытки остаются у JobOwner.

## Коды отказа совместимости с host

`HOST_CONFIG_MISSING`, `HOST_CONFIG_INCOMPATIBLE` (профиль/области/live-пилот),
`SCOPES_INCOMPATIBLE`, `GRANTS_DIFFER`, `ROLE_BINDINGS_DIFFER` (назначения ролей и права),
`API_ATTEMPTS_WOULD_ROLL_BACK` (live-host: восстановление не должно сбрасывать счётчик попыток),
`RETENTION_NOT_INSTALLED`, `FILE_NOT_ALLOWED`, `HOST_STATE_UNREADABLE`, плюс коды M5-02
(`CHECKSUM_MISMATCH`, `LEDGER_UNAVAILABLE`, `LEDGER_OLDER_THAN_COPY`, `AGENT_NOT_STOPPED`, ...).

## Для координатора

`ai_agent_core.local_backup.create_backup_callback(layout, ledger=..., policy=...)` возвращает
async callback без аргументов: один снимок и ротация по `BackupPolicy`. Он не стартует Runtime/LLM
и не планирует расписание. Передайте ledger владельца retention, когда он будет привязан.

## Windows

Windows-проверки staging, ACL/private blobs, отказа переключения и STOPPED после restore —
в [отчёте R4](implementation-reports/R4-staged-restore.md). Для offline restore процесс serve
должен завершиться. В in-process ASGI-тесте отдельно закройте SQLiteStore: иначе открытый
Windows handle закономерно запретит замену БД. Обход OS lock или файловой защиты не нужен.
