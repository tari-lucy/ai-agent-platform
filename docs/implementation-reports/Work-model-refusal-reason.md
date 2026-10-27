# Отказ рабочего хода до модели: точная причина

Claude Code, 2026-10-27, по поручению Codex. База main `d7b3d16`, ветка
`claude/prompt-budget-reason`. Окно: `m3_composition/service.py`, `activity.py`.

## Причина

`Execution.generate()` для live-привязки одним условием проверял пять разных вещей
(общий бюджет есть, то же подключение, роль разрешена, рабочая память, размер промпта)
и отвечал общим `CAPABILITY_UNAVAILABLE` без следа: LLM-попытка ещё не создана, а
`ExecutionModelGateway` превращал отказ в `not_admitted`, и пользователь видел общий код.

Воспроизведение на Linux (настоящий OpenCode 1.19.3, синтетическая модель): с домом и
интернетом OpenCode шлёт 8 инструментов на **15 666 символов** (только дом — 7 на 14 438);
текст среды платформы — 6 347 (дом+интернет+будильники; без дома 2 966). Вместе
~22 000 ещё до контекста личности, разговора, `/guide` и системного промпта OpenCode.
При `live.max_prompt_chars=24000` ход с домом/интернетом отказывается всегда, а
`activity-goal` (без инструментов OpenCode) проходит — это и наблюдалось.

## Исправление

- `live_refusal()` возвращает фиксированный код: `MODEL_BUDGET_MISSING`,
  `MODEL_CONNECTION_MISMATCH`, `MODEL_ROLE_NOT_ALLOWED`, `MODEL_TEST_MEMORY`,
  `MODEL_PROMPT_LIMIT` (с `prompt_chars=… limit=…`).
- `ModelRefused(ContractError)` — тот же `CAPABILITY_UNAVAILABLE`, права и лимиты прежние;
  `Execution.refusal` хранит код. Лог: `Model call refused before the provider: <код> role=…
  <размеры>` — без содержимого промпта и секретов.
- `ExecutionModelGateway.refused` и лог `Work model call not admitted: <код>`.
- Лимиты автоматически не повышаются, проверки не обходятся.

## Тесты

- `tests/local_host/test_live.py`: настоящий live-адаптер с подменным HTTP — запрос
  сверх лимита не доходит до транспорта, в логе `MODEL_PROMPT_LIMIT`/`limit=1000`, текста
  промпта нет; после восстановления бюджета следующий вызов проходит. 5 passed.
- `tests/workplace_sessions/test_model_refusal.py`: ход с реальным текстом среды
  дом+интернет и набором инструментов размера OpenCode — при 24 000 `MODEL_PROMPT_LIMIT`,
  при 40 000 допускается; каждый код отдельно; gateway сохраняет причину. 6 passed.
- Платных вызовов нет, полный suite не запускался.

## Решение оператора (не меняется кодом)

1. `PrivateAPIBudget.max_prompt_chars` ограничен 1000–40000, а `work_sessions.
   max_prompt_chars=100000`: всё сверх 40 000 недостижимо. Варианты: поднять верхнюю
   границу бюджета (и `live.max_prompt_chars`) или сверять эти настройки при включении.
2. Для хода с домом/интернетом нужен `live.max_prompt_chars` не меньше ~32 000–40 000.
3. Показ кода в UI: задания хранят только `ErrorCode`; чтобы `MODEL_PROMPT_LIMIT` был в
   прогрессе разговора, нужен новый `ErrorCode` или поле причины — окно contracts/session.
