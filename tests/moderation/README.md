# Проверки M2-07 (модерация)

Вымышленные тексты и политика; реальная SQLite, шлюз M2-04 с `ScriptedProvider` как модель-модератор.
Без сети и ключей.

```bash
export PYTHONPATH=apps/core/src:apps/worker/src:packages/contracts/src:packages/domain/src:packages/application/src:packages/adapters/src
python -m pytest tests/moderation -q
python -m ruff check packages tests/moderation && python -m mypy && python scripts/check_imports.py
```

`test_gate.py`: одобрение точной редакции и привязка проверки; минимальный контекст и данные ≠ инструкции;
недоступность/сбой/обрыв/невалидные ответы — не «да»; отказ с правилами и фрагментами; revise → новая
редакция проверяется заново; жёсткое правило; review и лимит редакций; локальная блокировка секретов
без отправки; независимость модели (в т.ч. резервы и сообщённая модель); изменение текста/аудитории/
каналов/редакции/контекста/политики/срока/STOP/допуска закрывает выпуск; подделки; отмена.

Не проверено: реальная модель-модератор и её качество (пропуски/ложные блокировки/задержка), аудио/медиа,
Windows, runtime-wiring.
