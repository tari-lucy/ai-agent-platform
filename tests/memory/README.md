# Проверки M2-02 (memory / recall)

Только вымышленный текст; реальные SQLite (основное хранилище M1-02) и отдельный файл индекса
с настоящими FTS5 и sqlite-vec 0.1.9. Embedder — детерминированная имитация (стемы по 4 буквы),
она проверяет логику, а не качество MiniLM.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/memory -q       # нужен пакет sqlite-vec==0.1.9 (extra recall из uv.lock)
python -m ruff check packages tests/memory && python -m mypy && python scripts/check_imports.py
```

- `test_chunking.py` — дословные детерминированные куски и хеши.
- `test_index.py` — индекс отдельно: scope, подпись/размерность векторов, TTL, атомарная пересборка, группы, безопасный запрос.
- `test_lifecycle.py` — регистрация, дубликаты, TTL, права/expire/delete, эпизоды, конфликтные утверждения, сбой индекса.
- `test_recall.py` — права по целям, устаревший индекс, удалённое/просроченное, изоляция scope/persona/workspace/work-test, ACL, дедуп опыта, FTS fallback, смена модели, `unavailable`, конфликты, reconcile.

Не проверено: реальный FastEmbed/MiniLM в этой ветке, Docker/Linux-контейнер M1-02, Windows, runtime.
