# Integration-derived-artifacts — очистка производных лабораторных файлов

Исполнитель: Claude Code. Ветка `codex/derived-artifacts-purger`, отдельный worktree.
База: `origin/main` bb3d207. Backup-ветка `codex/local-backup-integration` (a253c7e) не использована
и не смешана.
Статус: **код и узкая приёмка в ветке для review. Production gate закрыт НЕ полностью:**
связь «источник → отчёт» и порт проверки наличия тела отчёта в owner-коде отсутствуют (ниже).

## Что обнаружено (форматы и владельцы)

- **Lab-индексы.** Каждый запуск `LabRunner` получает свежий `SQLiteRecallIndex` в
  `<host>/data/lab-indexes/<uuid>.sqlite`; host держит handle открытым до выключения `serve`
  (список `opened`, без привязки к имени файла). После перезапуска файлы остаются на диске без
  владельца-handle. Связь файл ↔ область есть только внутри самой БД индекса (`sources.scope_key`).
- **Отчёты.** `Reports` пишет тело `<sha256(scope+source_id)>.json` и маркер `.ttl`; `Reports.purge`
  удаляет по точной паре (scope, source_id) **собственного источника отчёта** (`labsource`).
- **Пробел.** Тело отчёта содержит текст лабораторных источников (`Lab.episode` пишет в лог
  `text=…`, `say` — реплики), но ни тело, ни `Artifact.provenance_source` не называют эти источники.
  Удаление источника памяти по его `source_id` не находит зависимый отчёт, а удаление «отчётного»
  файла по id источника ничего не доказывает. Фиктивное соответствие не создавалось.

## Что сделано

`apps/core/src/ai_agent_core/local_artifacts/`:

- `derived_purger(roots, open_indexes, reports=ReportBinding(files, catalogue))` — фабрика
  существующего порта `DerivedPurger.purge_sources(scope, source_ids)` для
  `RetentionService(..., purgers=(…,))`. Второго реестра удалений нет.
  - **Индексы:** открытые handle владельца (`open_indexes() -> {путь: RecallIndex}`) чистятся
    через публичные `inventory`/`remove_source`/`compact` с проверкой после. Закрытые файлы
    каталога распознаются (таблицы индекса; иначе отказ `UNRECOGNISED_INDEX_FILE`, чужой файл не
    получает таблиц), открываются адаптером на время операции и чистятся на месте.
    Файл индекса не удаляется, открытый handle не закрывается.
  - **Отчёты:** точное `Reports.purge` для каждого id; затем, если в области остались тела
    отчётов (`holds`), purger бросает `PURGE_INCOMPLETE` (причина `REPORT_BODIES_UNLINKED`):
    источник остаётся `blocked_pending_purge` (проверено: шаг `derived`, текст в БД не стёрт,
    `resume` идемпотентно повторяет отказ).
  - **Широкая инвалидация** — единственный честный способ снять эту блокировку:
    `preview_scope_reports(scope, catalogue)` показывает, какие отчёты области станут недоступны,
    `invalidate_scope_reports(preview, decision_id, …)` выполняется только по решению и неизменному
    preview. Записи каталога остаются; чтение отчёта затем даёт ошибку (потеря доступности явная).
  - Ошибки идут владельцу как `ArtifactsError(code, reasons)` — только фиксированные коды/типы
    исключений, без путей и текста. Повтор безопасен.
- Offline-подготовка при restore: `plan_restore` / `apply_restore` и CLI
  `python -m ai_agent_core.local_artifacts preview|apply --root R --ledger L [--drop-reports]`.
  Под OS-блокировкой host (работающий `serve` → `HOST_ALREADY_RUNNING`), существующий ledger
  (пустой/чужой/отсутствующий → `LEDGER_UNAVAILABLE`, ledger не создаётся). Источники берутся из
  **независимого ledger**; из индексов удаляются только они; чужая область и соседние файлы целы.
  `--drop-reports` явно делает недоступными все тела отчётов (число и предупреждение в preview,
  решение и digest обязательны). Без него отчёты, которые нельзя подтвердить чистыми, показаны как
  `unproven_reports` и остаются. Старые производные файлы не восстанавливаются, LLM не вызывается,
  живая БД не открывается и не меняется (проверено побайтово).
- Файловые операции только в проверенных абсолютных корнях: ссылки/junction внутри корня,
  относительные пути, совпадающие/вложенные корни закрыты.

## Приёмка (Linux, реальные файлы): `tests/local_artifacts` — 10 passed

Host подготовлен настоящим `initialise`; lab-запуск — настоящий `LabRunner` с `Reports`;
удаление — настоящий `RetentionService` и независимый ledger; маркер вымышленный, не печатается.

| Требование | Результат |
| --- | --- |
| marker до удаления в источнике, индексе, теле отчёта | подтверждено чтением байтов и recall/download |
| после ledger→purge: нет в recall/download и в файлах (включая WAL/SHM) | индексы очищены; тело отчёта **остаётся** до решённой инвалидации, источник честно `blocked_pending_purge`; после инвалидации и `resume` — `purged`, байтов нет, download закрыт |
| другая область и соседние файлы | индекс и отчёт другой области, `keep.txt`, `secrets/` не изменились |
| повтор, отсутствующие id, закрытый/занятый индекс, отказ файловой операции | идемпотентно; закрытый handle, блокировка файла, `PermissionError` дают `PURGE_INCOMPLETE` без ложного purged; повтор завершает очистку |
| restore-подготовка | индексы очищены по ledger, отчёты — только по решению; БД и secrets побайтово прежние; повторный apply ничего не меняет; устаревший preview, открытый индекс, чужой sqlite — отказ |
| symlink/path | ссылка в каталоге индексов → `DERIVED_PATH_UNSAFE`, файл вне корня не тронут |

ruff и strict mypy (199 файлов) чисто. Полный suite не гонялся.

## Запросы координатору (общие интерфейсы — я их не менял)

1. **`Reports.holds(scope, source_id) -> bool`** (`m3_composition/artifacts.py`): публичная обёртка
   `self._path(scope, source_id).is_file()`. Нужна, чтобы purger и restore знали, что тело
   существует, не дублируя приватный `_path`. Сейчас `ReportFiles.holds` — порт; в тестах его
   заменяет `FileHolds` (использует `_path` только внутри fixture). Без `holds` подключить
   `ReportBinding` к `Reports` нельзя (mypy/Protocol не пропустит) — это намеренно.
   Проверка: `tests/local_artifacts/test_purger.py`.
2. **Связь источник → отчёт** (закрывает пробел окончательно, снимает необходимость в широкой
   инвалидации): `Lab.source()/episode()` должны писать `source_id` в лог, `ScenarioReport` — нести
   `source_ids: tuple[str, ...]` всех источников запуска, а `Reports` — сохранять их (поле
   `Artifact`/sidecar) и дать `Reports.sources_of(scope, artifact_id)` или `purge_for_source(scope,
   source_id)`. Тогда purger удаляет ровно связанные отчёты и может доказать чистоту.
3. **Инвентарь открытых lab-индексов в host.** `local_host/serve.py::lab_index()` сейчас
   только добавляет индекс в список `opened`. Нужен словарь `{путь файла: индекс}` (путь известен
   в `lab_index()`), его `.copy()` — аргумент `open_indexes`.
4. **Подключение (координатор):**
   `RetentionService(..., purgers=(derived_purger(verified_roots(host.lab_indexes, host.reports),
   lambda: opened_by_path, ReportBinding(reports, artifact_catalogue(store, PRINCIPALS[Owner.CATALOGUE]))),))`.
   При `REPORT_BODIES_UNLINKED` оператору показывается `preview_scope_reports`, затем
   `invalidate_scope_reports` по решению и `RetentionService.resume(scope)`.
5. **Restore:** после `BackupService.restore` и до первого `serve` — `local_artifacts preview`,
   затем `apply` (с `--drop-reports`, если оператор принимает потерю доступности отчётов). Встроить
   в переключение должен координатор.

## Ограничения и непроверенное

- Windows не проверялся (блокировка, `os.replace`, junction). Короткий smoke: на Windows создать
  temp host (`init`), запустить lab-сценарий, удалить источник через RetentionService, затем
  `python -m ai_agent_core.local_artifacts preview --root R --ledger L` и `apply` при остановленном host;
  убедиться, что при запущенном `serve` команда отвечает `HOST_ALREADY_RUNNING`.
- Индексы с таблицей `vectors` (создаются только с `dimension`) закрытыми файлами не чистятся:
  `UNRECOGNISED_INDEX_FILE`; lab-индексы host создаются без `dimension`.
- Стирание тел отчётов при `--drop-reports` — перезапись нулями и unlink (best effort; на SSD/CoW
  физическое стирание не гарантируется).
- Основной dialogue-индекс и БД чистит `RetentionService`, не этот пакет. Backup/replaced каталоги,
  Source/Episode/Claim, usage, секреты и ledger не затрагиваются.
- Рабочие каталоги не чистились; всё — во временных fixture.
