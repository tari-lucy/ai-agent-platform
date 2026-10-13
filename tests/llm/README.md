# Проверки M2-04 (LLM)

Только вымышленные тексты и ID; без сети: `ScriptedProvider`, `httpx.MockTransport` и
loopback-сервер 127.0.0.1 на asyncio. Реальные ключи и платные вызовы не используются.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/llm -q
python -m ruff check packages tests/llm && python -m mypy && python scripts/check_imports.py
```

- `test_gateway.py` — полный/оборванный поток, конечный повтор, резерв, отмена (event, задача,
  допуск, поздний результат), timeout, finish reasons, usage, некорректные результаты, привязка роли.
- `test_openai_adapter.py` — форма запроса Chat Completions, usage, статусы HTTP, редиректы,
  секреты, endpoint-валидация, конфигурируемые модель/endpoint, реальный loopback: обрыв
  соединения, отмена и закрытие сокета.

Не проверено: реальный агрегатор (совместимость, usage при обрыве), Windows, Docker.
