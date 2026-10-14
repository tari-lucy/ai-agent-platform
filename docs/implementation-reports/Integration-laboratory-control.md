# Integration-laboratory-control — наблюдения и отмена одного задания

Дата: 2026-10-15. Координатор: Codex. Base c94f1b3.
Ветка codex/integration-laboratory-control, отдельный checkout.
Поручение: связать лабораторные controls с реальными владельцами; последующее
поручение оператора — затем завершить остальные стыки, host запуск и общий smoke.

## Принятый результат

- Canonical laboratory.stop-args/event-args и LaboratoryRun/LaboratoryRuns,
  generated Schema/OpenAPI/TS. GET laboratory-runs читает настоящий Job state
  с operator read+api_transfer grant/ACL, bounded pagination. Полные Job scope,
  operation_id/version и pinned TEST lab_scope не выводятся из report.version.
  Live clock/readiness/count после завершения отсутствуют (null), не выдумываются.
- JobOwner.cancel проверяет operator/run/Job CAS и canonical request replay.
  Текущий SQL Job ACL проверяется в Jobs UoW до синхронного retire только его
  admission. QUEUED/WAITING отменяются; RUNNING — OUTCOME_UNKNOWN до настоящего
  factual executor result. Async task cancellation сама по себе не ACK.
  Поздние domain записи закрыты существующими lease/admission/epoch fences.
  Другие задания/общий run не останавливаются; прямой STOP не менялся.
- Runner lifecycle публикует live TEST context и закрывает приём до формирования
  отчёта; finally покрывает ошибки/отмену. Повтор execute не перезаписывает живой
  context. Один TEST scope не принимается двумя pending laboratory Jobs.
- Событие привязано к точному RUNNING ready job, версии, parent run и TEST
  partition. Лимиты 32/job и 2000 chars, пустой/secret-like текст закрыт.
  Обычный лабораторный MEMORY owner сохраняет simulation Source в TEST;
  api_transfer/public_disclosure=false, канон/CharacterState не меняются.
  Это наблюдение, а не самостоятельный вызов модели или пережитый эпизод.
- Job API ACL/CAS/config/Permission/lease/admission перепроверяются внутри MEMORY
  commit. Retry identity/receipt принадлежит MEMORY, отмена — JOBS. Request
  replay не создаёт второй Source; after-cancel новое событие закрыто.
- Report хранит source refs/sequence, не копирует сырой текст наблюдения.
  Summary operator_events помечает интерактивный прогон. Панель не объявляет
  его seed-only воспроизведением: нужны также порядок/время исходных событий.
- Панель показывает задания/virtual time/live count, canonical controls и
  persisted status после reload. Известный version conflict отличается от
  unknown/network; неизвестная mutation автоматически не повторяется.
  Старые stop/event helpers и report-version CAS убраны; прежние browser
  проверки обновлены под канонический путь.

## Проверки

| Проверка | Результат |
| --- | --- |
| Linux affected jobs/control/access/contracts/laboratory/m3_runtime | 348 passed, 4 skipped, 185.61s; один офлайн контейнерный gate |
| Новые actual ASGI/SQLite cases (включены в Linux) | 5: event/replay/private TEST source+running cancel factual receipt, queued cancel/replay/wrong CAS, revoked Job API, 32-event limit+реальный report без текста, foreign TEST/stale run |
| Клиентские pure tests | 158 passed; canonical Job CAS/TEST identity, malformed projections, interactive report comparison |
| Настоящий Edge + HTTP/SQLite/runner | 1 passed, 11.30s; UI start→event→cancel→CANCELLED, общий run остаётся ACTIVE, reload сохраняет job status |
| Затронутый прежний laboratory browser file | 13 passed, 8.95s; intercepted projection UI cases, не доказательство backend |
| Static/build | Ruff, mypy 174 files, ESLint, TypeScript/build, import boundaries, canonical export check passed |

Четыре Linux skips — Redis integration cases: отдельный Redis host не задан.
Общий STOP/reconciliation и SQLite owner paths проверены; реальная брокерная
топология здесь не перезапускалась. Warnings: legacy WS (6 Linux/real browser,
2 intercepted UI), прежний Vite chunk >500 kB. Full backend/browser suites
не повторялись; Linux network=none, read-only mounts, synthetic /tmp,
2 CPU/1536 MiB, без общей рабочей памяти.

Во время реализации исправлены: replay ошибочно проверял MEMORY receipt через
JOBS helper (теперь проверяется правильный owner/principal/fingerprint/TTL);
закрытый runner возвращал общее busy вместо stale; fixture не задала нужный
CATALOGUE reader на report Source; browser assertion обращалась к Supervisor
не на его потоке. Проверки не ослаблялись, default grants не расширялись.

## Границы и дальнейшие действия

Scoped acceptance выполнен. Секреты/.env/реальные профили не читались;
модели/платные API, игра/каналы/эфир не подключались. Новых dependencies,
lockfile/Compose/миграций/решений нет. Diff/whitespace/секреты/личные пути
проверены перед commit. Host должен явно подготовить operator Job API grants
и readers; отсутствие прав закрывает controls/чтение. Общая память не подключена.

Событие не заставляет scripted mock отвечать на произвольный текст: для такой
проверки есть приватный dialogue путь. Это приёмка входа наблюдений/изоляции,
а не качества LLM. Для интерактивного replay нужен исходный набор событий;
автоматический replay произвольного interactive transcript не добавлялся.

Дальше по текущему поручению: canonical GM/development decisions/rollback,
memory plans/correction/delete/export, usage/dashboard и необходимые operations;
затем воспроизводимый host bootstrap и один общий first-start/restart/STOP smoke.
Прежний Windows backup ACL blocker остаётся отдельной проверкой. Этот этап
не объявляет весь MVP готовым и не снимает все статусы UI-only M4/M5.
