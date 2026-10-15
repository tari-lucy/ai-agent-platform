# Integration-local-backup — резервирование и восстановление local host

Исполнитель: Claude Code. Ветка `codex/local-backup-integration`, отдельный worktree.
База: `origin/main` 1bf0123 (проверенный 7cf69be + docs-коммит с поручением).
Статус: **код готов в ветке для review; production-стык retention/ledger не закрыт, реальный host
восстановить нельзя.** Тест на fixture не равен готовому recovery.

## Что сделано

- `apps/core/src/ai_agent_core/local_backup/`: layout (явные непересекающиеся пути), охрана ledger
  (не создаёт ledger), gate по OS-блокировке host, проверка совместимости копии с host, composition
  существующих `SQLiteSnapshotter`/`DirectoryBackupStore`/staging/`FileSwitcher`, CLI
  create/list/verify/preview-restore/restore, публичный `create_backup_callback`.
- Адресная правка в моей зоне: `BackupService` получил необязательный `restore_check` — проверка
  внутри preview, поэтому её проблемы входят в digest и перепроверяются в `restore`.
- Второго формата копий нет; local_host, m3_composition, контракты, зависимости не менялись.

## Приёмка (Linux, реальные файлы, вымышленные данные): `tests/local_backup` — 19 passed

Temporary host создан настоящим `initialise`; записи — через MemoryService/RetentionService/UsageLedger.

| Требование | Проверка |
| --- | --- |
| Копия с WAL, list/verify | коммиты только в `-wal` присутствуют в копии |
| Allowlist | в копии только database, host.json, manifest; `.env`, пароль, хэш, лог отсутствуют; чужой файл в манифесте блокирует |
| Права | каталоги 0700, файлы 0600, снимок не остаётся в work |
| Изменения после снимка | в репетиционной копии каталога host их нет, факт читается через recall, оригинал байт-в-байт цел |
| Удаление после снимка | не оживает: нет в БД/WAL/SHM/индексе, recall пуст, `reapplied_deletions=1` |
| Подмена копии / ledger | checksum, отсутствующий/пустой/чужой/откатанный ledger закрывают restore; ledger не создаётся |
| Активный serve | `HOST_ALREADY_RUNNING`, ничего не создано |
| Другой host config | `HOST_CONFIG_INCOMPATIBLE`, роли/области/права; откат счётчика API-попыток блокируется |
| Сбой staging / переключения | живые файлы байт-в-байт целы; ошибки без путей и текста |
| После restore | новый serve `STOPPED`; открытое задание остаётся `RUNNING`, старые outbox не переисполнены, попыток нет |

Также: `tests/backup` 37 passed; ruff и strict mypy (196 файлов) чисто.

## Не проверено

- Windows: ACL, права каталога копий, `os.replace` открытых файлов. Один короткий smoke для
  координатора: на Windows создать временный host `init`, `create`, остановить, `preview-restore`
  и `restore` с отдельными `--store`/`--ledger` вне каталога host, затем `serve` и убедиться в
  `STOPPED`.
- kill -9 между переключением файлов, большие БД, контейнерные тома, реальный FastEmbed (индекс host —
  только FTS, как в serve), копия на другом носителе.
- Ротация/очистка рабочих копий и `FileSwitcher.discard` на рабочих данных не запускались.

## Незакрытые production-стыки (запросы координатору)

1. **Retention/ledger binding в host.** Ни init, ни serve не создают `SQLiteDeletionLedger`,
   `RetentionService`, не вызывают `SQLitePhysicalStore.install()`. Нужны: владелец ledger-файла вне
   каталога host (файл, который retention пишет при удалениях), установка таблицы
   `retention_usage_aggregates` при init, передача того же ledger в `create_backup_callback`.
   Проверка: `preview-restore` реального host перестаёт давать `LEDGER_UNAVAILABLE` и
   `RETENTION_NOT_INSTALLED`. Сейчас композиция проверена на fixture, который эти шаги имитирует.
2. **Суточный цикл.** Подключить `create_backup_callback` к циклу первого агента; цикл и расписание
   мои файлы не трогают.
3. **Производные хранилища вне БД и индекса.** `data/lab-indexes` и `data/reports` restore не
   переписывает и не очищает от текста удалённых источников; нужен `DerivedPurger` владельца.
4. **Открытые задания** после restore остаются как были; сверку исхода (`OUTCOME_UNKNOWN`) должен
   дать владелец jobs (запрос M5-02 остаётся в силе).
5. **Публичный способ закрытия хранилища/чтения HEAD** по-прежнему в composition: HEAD берётся из
   `ai_agent_adapters.storage.migrations`.

## Ответ координатора (учтён)

Принято: единый ledger и RetentionService, расписание, очистка lab-индексов/reports и сверка jobs
подключаются координатором; production recovery закрыт до общей проверки. Backup не хранит
собственный список допустимых live-ролей: `_assignments_fit` удалён, `HostCompatibility` принимает
необязательное правило `assignments_fit(config, bindings)` от координатора, а пока сравнивает
назначения с текущей БД (`ROLE_BINDINGS_DIFFER`). Актуальный main 707fca8 (лимит API
настраиваемый) файлов этой ветки не затрагивает; ветка на 1bf0123, слияния не было.

## Решения

- Restore всегда offline под `exclusive_host`; create может идти рядом с serve (backup API).
- Копия допускается только к тому же host: identity host.json, области, права (`storage_grants`),
  назначения ролей (равенство текущей БД); для live-host число `LLMAttempt` в копии не меньше текущего (лимит платного API
  не сбрасывается откатом).
- Живая БД читается проверкой совместимости без следов: при отсутствии `-wal` — immutable-чтение.
