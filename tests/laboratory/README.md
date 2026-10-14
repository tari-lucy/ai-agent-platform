# Проверки M3-04 (laboratory)

Вымышленные люди и сцены; реальная SQLite, mock-модель, виртуальное время. Без сети и ключей.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/laboratory -q
python -m ruff check packages tests/laboratory && python -m mypy && python scripts/check_imports.py
```

- `test_scenarios.py` — каждый из 12 сценариев проходит жёсткие инварианты; весь каталог не меняет рабочую область;
  известный пробел виден как мягкая находка.
- `test_runner.py` — виртуальные часы, отказ от не-test области, обнаружение записи в рабочую область, replay
  по seed (и расхождение между seed), кубики GM, отчёт mock/без секретов/тестовая запись, статус `error`,
  незаскриптованный вызов, STOP/секреты, мягкие и жёсткие проверки.
- `test_transfer.py` — предпросмотр, перенос только выбранного с решением оператора и digest, блокеры совместимости,
  направление и ключи, создание рабочего профиля, биография теста не переносится.

Не проверено: реальный провайдер, качество личности и диалога (оценка оператором 1–5), Windows, панель.
