# Промежуточный review recovery-библиотек на Windows

Дата: 2026-10-15. Координатор, `codex/host-recovery-integration`.
В review-ветке сведены backup a253c7e и derived-artifacts a06b9a0;
это ещё не принятие production recovery в main.

Первый affected Windows-прогон: 18 passed, 10 failed, 1 POSIX skip.
Общая причина recall/restore отказов — FTS-only индекс импортировал optional
sqlite_vec. Исправление выделено отдельно, принято в main b8428b7; пакеты
и модели не устанавливались. Новый regression + restore/lab smoke: 3 passed.

После исправления единственный повтор затронутого набора:
`tests/local_backup tests/local_artifacts`: 28 passed, 1 skipped (POSIX mode).
Ruff и strict mypy: чисто (206 файлов). Полный suite не запускался.
Все host/ledger/копии/удаления — временные и синтетические. Рабочие данные,
ключи и live API не использовались; рабочий host не перенастраивался.

## Что ещё не закрыто

- Production init/serve не имеют принятого retention/ledger binding и расписания.
  Недостающий ledger нельзя заменять пустым при восстановлении.
- Reports.holds и полное происхождение report-текста отсутствуют; purger честно
  оставляет blocked_pending_purge. Большая очистка области требует явного решения.
  Lineage-задача отложена оператором в пользу рабочего диалога и памяти.
- Инвентарь открытых lab-индексов и согласованный порядок restore→derived cleanup
  →serve нужно подключить координатору.
- Тест «старые jobs после restore» вставляет неполный JSON Job без jobs schedule.
  Его RUNNING не является доказательством приёмки настоящего JobOwner recovery.
  Нужен настоящий queued/running job через владельца, без исполнения старого
  эффекта после restore. Некорректные/осиротевшие job records должны закрывать gate.
- POSIX chmod не доказывает Windows ACL для ledger/копий. Нужна отдельная проверка
  фактической ограниченности этих каталогов при production-подготовке.

Готовность библиотек и прохождение fixture отдельно от готовности восстановления.
Ни main, ни этот отчёт не объявляют production recovery завершённым.
