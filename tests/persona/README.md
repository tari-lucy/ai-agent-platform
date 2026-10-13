# Проверки M2-01 (persona)

Только вымышленные профили и отдельные SQLite-файлы (`tmp_path`, либо каталог из
`M2_01_DB_DIR`). Рабочая память, ключи и LLM не используются.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/persona -q
python -m ruff check packages tests/persona && python -m ruff format --check packages tests/persona
python -m mypy
python scripts/check_imports.py
```

- `test_markdown.py` — формат, схема, межполевые правила, секреты, нормализация, round-trip.
- `test_service.py` — на реальной SQLite: создание, принятие и снимки, конфликты версий,
  параллельные правки, откат при сбое между записями, состояние, copy без памяти, изоляция
  personas и work/test, идемпотентность, рестарт.
- `test_files.py` — адаптер файлов: границы каталога, symlink/FIFO/каталог, размер, кодировка,
  экспорт без перезаписи.

Не проверено: Windows, Docker/Linux-контейнер изоляции M1-02, runtime-wiring, HTTP, live LLM.
