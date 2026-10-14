# Проверки M3-03 (development)

Вымышленный профиль и опыт; реальная SQLite (persona, memory), шлюз M2-04 с `ScriptedProvider`.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/development -q
python -m ruff check packages tests/development && python -m mypy && python scripts/check_imports.py
```

`test_development.py`: границы шага/окна/диапазона, независимое и непотраченное основание, маршрут оператора
(identity/не автономные/текст), решение оператора, конфликты версий и инвалидация, повторная доставка, пауза/STOP/
наблюдение, сюжетные правила, неопределённость, откат (предпросмотр, цепочка, digest, история), бюджет окна.
`test_reflection.py`: предложения без применения, «оставить состояние», один триггер/интервал, производные
триггеры, отсрочка, непоказанные основания, сбои, проверка идеи моделью как любого предложения.

Не проверено: реальная модель-рефлексия, качество интерпретаций, проявление изменений в поведении, Windows.
