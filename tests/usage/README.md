# Проверки M2-05 (usage/ledger)

Вымышленные операции и цены; реальная SQLite M1-02 (миграции до head), без сети и ключей.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/usage -q
python -m ruff check packages tests/usage && python -m mypy && python scripts/check_imports.py
```

- `test_ledger.py` — повтор квитанции ≠ новая попытка, гонка записей, реальный повтор, поздний usage
  (замена, не сумма), конфликты, статусы, изоляция scope/work-test, права владельца, перезапуск.
- `test_cost.py` — cached/reasoning без двойного счёта, нет тарифа/usage/ставки ≠ 0, версии тарифа
  и неизменность прошлого, явный пересчёт и repin, сообщённая модель, валюты, сводки и история,
  путь шлюз M2-04 → журнал при отмене и позднем usage.

Не проверено: Windows, runtime-wiring, несколько процессов, сверка с реальным счётом агрегатора.
