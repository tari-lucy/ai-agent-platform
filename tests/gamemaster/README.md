# Проверки M3-02 (game master)

Вымышленные люди и сюжеты; реальная SQLite, шлюз M2-04 с `ScriptedProvider`. Без сети и ключей.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/gamemaster -q
python -m ruff check packages tests/gamemaster && python -m mypy && python scripts/check_imports.py
```

`test_gamemaster.py`: основания линий, слои правил и экран (hard/soft/неуверенность/сбой), cooldown, окно и
интенсивность, отдых семейства, повторы, заблокированные предложения, план не виден персонажу, исход только
по независимому источнику, переходы и истечение, пауза/STOP, кубики (воспроизводимость, веса, диапазон),
`suggest`, изоляция scope.

Не проверено: реальная модель-гейммастер, качество предложений и сюжетных проверок, Windows, runtime-wiring.
