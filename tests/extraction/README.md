# Проверки M3-01 (extraction)

Вымышленные люди и тексты; реальная SQLite (memory), шлюз M2-04 с `ScriptedProvider`. Без сети и ключей.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/extraction -q
python -m ruff check packages tests/extraction && python -m mypy && python scripts/check_imports.py
```

`test_extraction.py`: чужое сообщение/наблюдение/вывод/канон разделены; self-made материал не независим;
независимость по эпизодам и потолки уверенности; дословная цитата и права источников; противоречия;
отсутствие дублей; сбои/обрывы/отмена/STOP; simulation; данные ≠ инструкции; консолидация.

Не проверено: реальная модель-экстрактор и качество извлечения, смысловая связь утверждения с цитатой,
runtime-wiring, Windows.
