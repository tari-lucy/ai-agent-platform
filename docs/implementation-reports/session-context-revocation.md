# Рабочий разговор при недоступном контексте

Исполнитель: Claude Code. Ветка `claude/session-context-revocation`, base main `c966401`,
слито с `382af58`. Окно: `m3_composition/work_session_runtime.py`, методы
`agent_activity/sessions.py`, проекция разговора и её UI; совместимые поля контракта.

## Причина

После отзыва основания `select()` каждый тик отправлял тот же trigger (повтор упавшего job),
попытка не записывалась, разговор оставался `open` без объяснения и занимал очередь
(наблюдение в [session-skills](session-skills.md)).

## Поведение

- **Недоступна только часть контекста.** Закрытая обработанная история отбрасывается при
  сборке (`OpenHistory` поверх существующего `ReleasedSessionContext`); черновик, чьё основание
  закрыто, сбрасывается владельцем (`SessionBook.reset_context`: история, задачи и прогресс
  сохраняются, generation +1) — контекст пересобирается, разговор продолжается. Успешная
  попытка старой generation не блокирует новый ход.
- **Недоступно само необработанное сообщение оператора** (отзыв `api_transfer`, удаление,
  TTL/ledger, скрытая строка). Сообщение не исполняется: `SessionBook.invalidate` —
  терминальное состояние `invalidated` с `invalidated_reason="message_unavailable"`, задачи
  сохраняются, черновик/checkpoint/контекст сброшены, новые сообщения в этот разговор не
  принимаются. Панель ещё до START показывает «…не будет исполнено; начните новый разговор»,
  после остановки — причину и путь через существующую форму «Новый разговор о работе».
- **Без бесконечных попыток и расхода модели.** Основания проверяются в начале `select` до job,
  доступа и модели. Отказ сборки контекста записывает failed-попытку (лимит/охлаждение
  действуют, тот же trigger не повторяется). Незавершённые попытки инвалидированного разговора
  сверяются с job на следующих тиках и закрываются (`cancelled`/`failed`), а не висят
  `submitted`.
- **Отзыв во время модельного вызова**: поздний результат не применяется (существующие проверки
  release после хода), разговор останавливается, повтора нет. STOP и restart — прежние
  гарантии: после restart STOPPED, ничего не запускается само.
- `SessionBook.entries`: строка, скрытая lifecycle источника, — явный пропуск (`None`), не
  конец истории; `messages` на пропуске по-прежнему отказывает.

## Проверки

- `tests/workplace_sessions/test_context_revocation.py` (production build, SQLite, ledger,
  синтетический провайдер): закрытая история отброшена и контекст пересобран; закрытый черновик
  сброшен с новой generation; отзыв необработанного сообщения → остановка без вызова модели и
  без попытки, ввод в остановленный разговор отклоняется, новый разговор работает; истечение
  TTL → остановка; отзыв во время ответа → поздний результат закрыт, попытка `cancelled`, без
  повтора; STOP/restart STOPPED. 2 passed (второй — 3/3 повтора).
- `test_sessions.py` (+1): invalidate сохраняет задачи, повтор — тот же receipt, повторная и
  устаревшая инвалидация отклонены, ввод после остановки отклонён, причина только у
  `invalidated`; `entries` даёт пропуск, `messages` отказывает.
- `workplace_sessions` + `workplace_conversation` + `workplace_launcher` +
  `test_session_skills.py`: 40 passed, 1 skipped (настоящий OpenCode без образа). Ruff, strict
  mypy, import boundaries — чисто.
- Панель: node tests 205 passed (+1 подписи/проверка; исправлен устаревший
  `tickets.test.ts` после `c966401`), eslint. Headless Chromium
  `test_work_conversation_ack_browser.py` (+1: отзыв в STOPPED → видимая причина → START →
  остановка с объяснением, модель не вызвана → новый разговор) и
  `test_work_conversation_browser.py` — 3 passed.
- Полный suite не повторялся (по поручению).

## Контракт

Совместимые необязательные поля: `WorkSession.invalidated_reason`,
`WorkSessionTranscript.invalidated_reason` (`"message_unavailable"`), значение
`waiting_reason="message_unavailable"`. Экспорт generated и `tsc` — после окна Codex
(`api.py`/`catalogue.py`/generated).
