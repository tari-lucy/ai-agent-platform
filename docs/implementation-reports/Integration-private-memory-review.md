# Приёмка приватной памяти на актуальной композиции

Дата: 2026-10-15. Координатор. Ветка исполнителя
codex/private-dialogue-memory-quality, HEAD 46000dd, база 125974c.
Review выполнен в codex/memory-quality-review от main 9b076c4, включая FTS-only
исправление, host recovery и native Windows ACL. Слияние без конфликтов.

Просмотрены prompts/wire, extraction diagnostics, ранжирование и повторная
проверка оснований производной памяти. Валидатор цитат, типы/атрибуция,
API-transfer/TTL/source versions и token budget не ослаблены.

Windows affected acceptance: 9 passed (134.84 с): private_pipeline,
memory_rights_and_control и probe. Сквозной production-host сценарий проверяет
деталь вне последних восьми реплик, память и следующий запрос после restart;
отдельно revoke/late result, WORK/TEST/STOP/pause, bounded reflection и HTTP ceiling
probe на synthetic provider. Полный набор исполнителя повторно не запускался.
Ruff и strict mypy (213 файлов) чистые. Группу memory_quality добавил в combined
runner; полный combined не запускался.

Малая coordinator-правка: opt-in probe восстанавливает исходный provider class
даже при исключении. Добавлено утверждение в существующую проверку probe; ранее
подмена оставалась в процессе после завершения (тестовая monkeypatch fixture это
скрывала). Платных вызовов этим acceptance не делалось.

Ограничение больших эпизодов подтверждено в общем случае. Текущий подтверждённый
private-dialogue путь передаёт в pairs ровно входную и собственную реплики:
4000 + 2000 символов, два первичных источника — меньше 8000/20. Все grounds
эпизода отдельно проверяются. Разбиение будущих больших эпизодов не объявлено
реализованным и не служит причиной ослаблять provenance. Диагностика extraction
пока не durable-поля MemoryCycle; это отдельное расширение контракта.

Приёмка synthetic-пути разрешает начать отдельную задачу веб-диалога от свежего
main после объединения. Качество свободного ответа live-модели этой проверкой
не доказано; bounded real probe остаётся отдельной локальной пробой координатора.
