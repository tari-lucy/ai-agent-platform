# Проверки M2-06 (диалог/контекст/внимание)

Вымышленные люди и тексты; реальная SQLite (storage, memory/recall FTS, social memory), шлюз M2-04
с `ScriptedProvider`. Без сети и ключей.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/dialogue -q
python -m ruff check packages tests/dialogue && python -m mypy && python scripts/check_imports.py
```

- `test_pure.py` — внимание (бюджет болтовни, слияние, TTL, ёмкость, цель, оператор), извлечение `reply`
  из потока JSON при любом чанкинге, лимиты инициативы.
- `test_controller.py` — черновик и приватный стрим, частичный/некорректный JSON не исполняется, действия
  (разрешённые/запрещённые/наблюдение), STOP в потоке и между ответом и использованием, резерв вопросов,
  обещание только по DELIVERED, память между сессиями, права api_transfer, данные ≠ инструкции, режимы
  наблюдения/паузы/STOP, лимит вызовов при потоке сообщений.
- `test_initiative.py` — напоминания из точных обязательств, интервалы/лимиты, «не писать», перепроверка в
  очереди, полнота обязательств против обрезки контекста.

Не проверено: реальный провайдер/агрегатор, Redis/jobs-путь, runtime-wiring, Windows, несколько процессов.
