# Integration-development-controls — ручные решения и компенсирующий откат

Дата: 2026-10-15. Координатор Codex. Base 855144f; отдельная ветка
codex/integration-development-controls. Продолжение поручения оператора закрыть
стыки панели и затем проверить общий запуск. Linux host остаётся у второго агента.

## Реализованный результат

Canonical args для change.decision/interpretation/rollback, opportunity.cancel и
arc.pause, generated Schema/OpenAPI/TS; назад совместимое поле operator_reason
в StateChange. Target/CAS принадлежат объекту, GM использует numeric intensity
и GmRuntimeState.version. Панель читает настоящий набор правил; обязательная
причина решения сохраняется в истории.

Apply/reject/interpretation вызывают прежний DevelopmentController. Исправление
меняет только новую ревизию интерпретации: CharacterState/evidence/прошлая версия
сохраняются. Pending снова требует оператора; APPLIED сохраняет new_version.
Apply и invalidation устаревших pending теперь одна PERSONA транзакция.
GM pause arc и отмена её ещё неразыгранных возможностей — одна GM транзакция.

Rollback требует preview с последствиями и явное подтверждение панели. Private
proof пинит цепочку, версии Profile/CharacterState/GM/источников и текущие API
права оператора; срок пять минут. Execute создаёт компенсации/новое состояние и
помечает устаревшие pending CONFLICT атомарно. Никакие события/источники/биография
и отправленные реплики не удаляются. Commit повторно проверяет SQL ACL/current
source versions/config/control_epoch; поздний STOP/отзыв доступа не пропускает запись.

WORKSPACE OwnerOperation хранит только техническую квитанцию/fingerprint/result IDs.
Нет фиктивного Job и второй копии target state. При потерянной квитанции повтор
сверяет существующий результат по operation ID без второго эффекта; rollback
recovery работает и после истечения preview. Если результата нет, OUTCOME_UNKNOWN
остаётся явным, повторное применение не выполняется. Completed replay сохраняет
точную исходную квитанцию; другой payload того же request ID конфликтует.

## Проверки

- Windows, реальные ASGI/SQLite: tests/m3_runtime/test_development_controls.py —
  14 passed, 39.27 s. Принятие/отклонение/коррекция, сохранение прежней ревизии,
  компенсация/replay, stale proof/source/state, потеря квитанции apply/rollback,
  поздний epoch/source/API grant и GM resource CAS/cancel/pause.
- Реальный Edge без перехвата API: login → обязательная причина → apply →
  preview → подтверждение → compensating rollback → persisted reload пройден.
- Node client tests: 158 passed; TypeScript/ESLint/production build прошли.
- Linux offline affected gate: tests/development, tests/gamemaster,
  tests/contracts, tests/access, tests/m3_runtime — 302 passed, 189.69 s,
  exit 0, шесть прежних WebSocket deprecation warnings. Cached image,
  network=none, 2 CPU/1536 MiB; другие runtime/volumes не использовались.
- Browser affected: 17 перехваченных UI cases + один настоящий owner runtime
  case. Первый batch дал 17 passed/1 failed из-за устаревшей ожидаемой версии
  в fixture; исправлен только assertion, повтор одного case 1 passed, 3.15 s.
  Реальный owner case прошёл с первого запуска. Суммарно все 18 cases проверены.
- Ruff, mypy (176 sources), import boundaries, canonical export --check — exit 0.
  Перед commit diff --check и проверка новых/изменённых файлов на личные данные
  и секреты; generated exports соответствуют canonical Python schemas.

## Ограничения и интеграция host

Всё на вымышленных данных/без LLM и paid calls. Качество реального персонажа,
агрегаторы/API, ресурсы модели и полный MVP ещё не приняты. Старый suite целиком
не перезапускается: затронутый backend набор + один реальный browser путь.
Есть прежние warnings legacy WebSocket API и размера UI bundle >500 KB.

Ветка второго агента d216ee8 собрана от d2e473e и пока покрывает более узкий
mock dialogue/lab путь. Её minimal grants не включают WORKSPACE write/read API
для owner plans/receipts/settings transfer, PERSONA→MEMORY и GM→MEMORY для
развития/правил/рефлексии. Это честная граница её прежнего smoke; нужны явное
расширение матрицы init под новые пути и комбинированная приёмка. Не скрывать
недостающие grants внутри serve и не объявлять fixture доказательством host.

Ручные unknown records читаются через owner-operations, при отсутствии factual
result требуется разбор; автоматический повтор не добавлен. Новые зависимости,
миграции, Compose и live-provider fences этой задачей не меняются.
