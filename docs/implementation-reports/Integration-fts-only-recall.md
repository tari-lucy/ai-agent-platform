# Исправление FTS-only recall без optional vector dependency

Дата: 2026-10-15. Координатор, ветка `codex/host-recovery-integration`.

При Windows-приёмке двух переданных библиотек обнаружен общий дефект принятого
SQLiteRecallIndex: `_insert` безусловно импортировал sqlite_vec даже при
`dimension=None` и отсутствии векторных данных. MemoryService сохранял Source,
но отмечал неудачную индексацию; recall возвращал пустую выборку. Restore с
reindex завершался ModuleNotFoundError. На окружении с установленным optional
sqlite_vec эта ошибка не проявлялась. Устанавливать optional пакет для FTS не нужно.

Импорт теперь выполняется только в vector-пути. Условия включения векторов,
проверки scope/версий/TTL и поведение ошибок не ослаблены. Добавлена регрессия,
где optional импорт принудительно недоступен: put, поиск, rebuild и удаление
работают через настоящий FTS5. Векторы не объявляются доступными.

Проверки: регрессия FTS-only и по одному настоящему restore/lab smoke на
синтетических данных — 3 passed; ruff и strict mypy (206 файлов review-ветки)
чистые. Restore/lab smoke относятся к ещё проверяемым integration-библиотекам,
а не к заявлению готовности production recovery. Полного suite, установок,
моделей и платных API не было; рабочая БД не менялась.
