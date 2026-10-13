# Проверки M2-03 (social)

Только вымышленные люди и текст; реальная SQLite M1-02 и `MemoryService` для источников.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/social -q
python -m ruff check packages tests/social && python -m mypy && python scripts/check_imports.py
```

- `test_people.py` — раздельные platform ID, аноним, смена ника, изоляция persona/work-test, явные связи
  аккаунтов, узнавание после перезапуска, отношения (версии, идемпотентность, границы), права.
- `test_conversation.py` — резерв вопроса при гонке, переформулировка, ожидание/известный ответ/поправка,
  не доставленный черновик и unknown, истечение резерва, cooldown/reopen, «не обращайся», обещания только
  по доставленной реплике, закрытие с подтверждением, полнота обязательств (230 > страницы), срок
  исходного текста, сбой записи, контекст повторной встречи.

Не проверено: Windows, Docker/Linux-контейнер M1-02, runtime-wiring, несколько процессов.
