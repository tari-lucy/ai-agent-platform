# Справка после блока M2 (M2-01…M2-07)

Дата: 2026-10-13. Автор: Claude Code (второй агент). Состояние: `main` = 065a71a на момент справки.
Модули M2 проверены на изолированной SQLite и моках; в runtime не подключены, реальный провайдер
не вызывался. Детали — `docs/implementation-reports/M2-0X.md`.

## 1. Окружение

- Выполнить `uv sync --locked --extra recall` и `scripts/check_combined.py` на Linux/VPS. У второго агента
  пакеты ставились вручную по lock, без метаданных пакета.
- В его venv падают 4 теста (`PackageNotFoundError: ai-agent-platform`): bootstrap, supervisor, 2× telemetry.
  Ещё 2 теста `tests/access` с реальным uvicorn не стартовали (`Server.started` False) — перепроверить все 6.
- `pytest tests` целиком не собирается: `tests/access/test_service.py` и `tests/persona/test_service.py`
  совпадают по basename. Нужны уникальные имена.
- В `tests/access` есть кросс-импорты между тестовыми папками (`operations_support`, `test_storage`,
  `test_jobs`); без доп. путей в `PYTHONPATH` сбор падает.

## 2. Подключение через composition (core bootstrap)

- Расширения: `register_persona_extensions`, `register_social_extensions`, `register_llm_extensions`
  (схема `llm.generation-parameters`).
- Grants и `RecordAccess`: memory-owner, social (SOCIAL, MEMORY read, CHANNEL read), `Owner.LLM` read/write,
  `Owner.MODERATION`, persona, `reader_access` оператору; `RecordAccess` social на Source и Delivery.
- Диалог: `ControlView` поверх Supervisor, `ProposalSink` в `JobOwner.submit`, `PrivateStream` в панель.
- LLM-шлюз: `Admission` = `JobOwner.verify` + `scheduler.verify`; `BudgetLease` вокруг `generate`;
  `attempt_id` из lease JobOwner; при `acquired=False` вычисление не повторять.
- `AttemptSink` → очередь → `UsageLedger.record_attempt`.
- Настройки `AI_PLATFORM_*` — в `OpenAICompatibleProvider`; `.env` читает только composition.
- Роль `moderator` назначить на другую модель; `Publisher` связать с каналовым шлюзом. Все публичные пути —
  только через `ModerationGate.release`.

## 3. Storage и владельцы (общие изменения)

- Запрос по семантическим ключам и полям: аккаунт→человек, человек→встречи, тема→вопрос,
  период/роль/модель для сводок. Сейчас сканирование с кэшем (валидно для одного процесса).
- API чтения и очистки просроченных (TTL) Source.
- В Topic хранится только хеш ключа темы; читаемый `topic_key` — решение social/storage.
- Gateway-владелец пишет `Utterance.moderation_status` и `moderation_check_id` по результату
  `ModerationGate.check`; журнал инцидентов — тоже за ним.

## 4. Отдельные назначения

- Хук «перед вызовом» в шлюзе M2-04 и `begin_attempt` в журнале: запись `running`
  (сейчас падение процесса посреди вызова оставляет попытку без записи).
- FastEmbed-адаптер и упаковка extra `recall` в образы core/worker; синхронизация индекса через outbox.
- Подтверждённое слияние и разделение людей.
- `persona.edit` с серверными черновиками: новый тип записи и миграция после 0003.

## 5. Проверки на реальном окружении (нужна авторизация оператора)

- Агрегатор: совместимость, usage при обрыве и отмене, параметр длины (`max_tokens`/`max_completion_tokens`).
- Сверка расходов с биллингом агрегатора.
- Классификатор модерации: набор RU/EN, обходы, ложные блокировки, задержка; реальные политики до
  подключения любого внешнего канала.
- Несколько процессов: очередь внимания, цель, лимитер инициативы и кэши social живут в памяти процесса.
- Windows, Redis (`tests/jobs/test_redis.py` пропущен без изолированного хоста), нагрузка выше сотен
  людей и десятков тысяч попыток.
- Blind-оценка качества диалога: 20 сценариев × 3 повтора по doc22.

## 6. Известные компромиссы

- Recall дедуплицирует по эпизоду: реплика персонажа и входящее сообщение — один эпизод, реплика отдельно
  всплывает не всегда.
- Результат модели принимается только как целый строгий JSON без fenced-блоков; на реальной модели
  возможны лишние отказы.
- «Другая модель» модератора проверяется по ID модели; разные ID одного upstream-агрегатора независимость
  провайдера не доказывают.
