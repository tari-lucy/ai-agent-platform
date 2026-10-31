# D88: короткая повторная Windows-проверка c73a287

Дата: 2026-10-31. Кандидат: `claude/resume-after-restart`, `c73a287`,
база `70cbe94`. Проверка: `codex/review-resume-windows-v3` от этого кандидата.

**Итог: три замечания предыдущей проверки закрыты в проверенном объёме.**
Блокирующих замечаний по этим сценариям не обнаружено. Это не приёмка
загрузки Windows или настоящего serve с Docker: они здесь не запускались.
Личный host, его процессы, настройки и VPS не менялись. Платных вызовов нет.

## Эквиваленты трёх прежних красных регрессий

Новый файл: `tests/control/test_resume_v3_windows_review.py`.
Используются настоящий Supervisor, production SQLiteRunJournal/SQLiteStore,
отдельная тестовая БД и временные файлы. Никакого старого маркера разрешения
или подмены ожидаемого результата на ACTIVE нет.

1. **STOP при заблокированном resume.json, затем shutdown и новая загрузка.**
   Файл теперь только блок восстановления. Записан старый блок, который сам
   по себе разрешает более новый ACTIVE-запуск: положительное предусловие
   проверяется явно. Настоящий CreateFileW разрешает читать, но запрещает
   удаление/замену файла. STOP фиксируется в SQLite; последующий shutdown
   сохраняет ended_at. После закрытия и повторного открытия БД Resume оставляет
   агента STOPPED. Отмену больше не нужно записывать в отдельный файл.
2. **STOP между Resume.close() и Runtime/Supervisor.shutdown().**
   Наблюдатель уже отключён, но STOP всё равно получает ended_at, shutdown не
   стирает его, следующий Supervisor через Journal.latest видит это решение.
   Resume остаётся STOPPED; файл разрешения не создаётся. Асинхронной записи
   старого ACTIVE-маркера в новой схеме нет.
3. **Нечитаемый блок восстановления.**
   Настоящий CreateFileW с share=0 блокирует чтение resume.json. read_block
   выдаёт ResumeError(RESUME_BLOCK_UNREADABLE); Resume пишет диагностический
   код и не запускает агента, хотя в журнале есть прежний ACTIVE. Результат
   STOPPED. Повреждённое содержимое дополнительно покрывает набор кандидата.

Все три теста требуют STOPPED и **прошли: 3 passed, 2,58 с**.
Два используют Win32 file sharing и пропускаются на других ОС; второй
сценарий пригоден и для Linux.

## Другие проверки

Наборы кандидата:

- `tests/local_host/test_resume.py`;
- `tests/control/test_resume_real_supervisor.py`;
- `tests/control/test_resume_sqlite_journal.py`.

Результат: **29 passed, 1 skipped, 3,17 с**. Пропущен только POSIX mode-bit
тест: stat.mode не доказывает NTFS ACL. Полный control/gate не повторялся.
Ruff check и ruff format --check нового тестового файла проходят.

Просмотрены изменения Supervisor.previous/Journal.latest и схема ended_at:
последняя запись области берётся и для завершённого запуска; shutdown
не стирает установленный STOP-ом ended_at. Это закрывает воспроизведённые
провалы прежней схемы с файлом-маркером.

## ACL блока на Windows

Get-Acl проверен у обоих resume.json, созданных в этой проверке. Унаследованы
FullAccess для SYSTEM, Administrators и OWNER RIGHTS; Everyone/Users нет.
Владелец в этом тестовом окружении — BUILTIN/Administrators.

DACL обоих файлов (без идентификаторов компьютера/пользователя):

```text
D:(A;ID;FA;;;SY)(A;ID;FA;;;BA)(A;ID;FA;;;OW)
```

Это подтверждение ACL этих временных каталогов, не гарантия защиты произвольного
host_root. os.chmod(0600) на Windows сам по себе не задаёт частный ACL: защита
здесь наследуется от родительского каталога. ACL личного host не менялся.

## Граница результата и передача

Проверка на c73a287 завершена. Можно передать Claude результаты для интеграции
его пакета. В ветке проверки только тесты и отчёт; production-код не менялся,
в main ветка не вливалась, личный автозапуск не включался.

Отдельно остаётся настоящая процессная приёмка на тестовом host:
нештатное завершение ACTIVE-процесса, штатное закрытие, явный STOP, затем
перезапуск serve; после этого загрузка ОС и задержанный запуск Docker.
Эти результаты нельзя выводить из быстрых тестов журнала.

Для повторения из checkout, с PYTHONPATH на его apps/core/src, apps/worker/src,
packages/{contracts,application,domain,adapters}/src и tests/control:

```powershell
python -m pytest --import-mode=importlib --basetemp=C:/ai-pers2/runtime/checks/resume-v3-new `
  tests/control/test_resume_v3_windows_review.py `
  tests/control/test_resume_real_supervisor.py tests/control/test_resume_sqlite_journal.py `
  tests/local_host/test_resume.py -q --tb=short
```

basetemp должен быть новым каталогом только для тестовых данных.
