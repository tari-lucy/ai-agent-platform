# Постоянный собеседник и точные обещания (этап 2 пула памяти)

Дата: 2026-10-19. Исполнитель: Claude Code. Ветка `codex/private-social-continuity`,
отдельный worktree от `origin/main` `638162c`. Этап 1 (`codex/long-term-memory-retention`)
ещё не принят и **не** использован: эта ветка от него не зависит. В `main` не вливалось,
рабочий host не обновлялся, платных вызовов не было.

Расширение прав SOCIAL авторизовано оператором в сессии 2026-10-19 (вариант «А»).

## Что было (проверено кодом и тестом)

1. Входной `Signal` приватного диалога не нёс ни `platform_id`, ни `account_id`. Поэтому
   `person_id = None`, а предложение обещания контроллер отбрасывал
   (`PROMISE:NO_REPLY_OR_PERSON`).
2. В приватном пути `DialogueController.settle()` не вызывается вообще. ACK пишет
   Delivery и Episode, но обещание доставленного ответа никуда не попадало, даже если бы
   `person_id` был.
3. Матрица прав хоста (`local_host/rights.py`) не давала SOCIAL ничего, а
   `record_readers()` не включал social-принципала. Поэтому `observe_account` без
   новых прав отменяет сообщение (`ACCESS_DENIED`), это воспроизведено.
4. **Дефект social-владельца:** запись хранит scope с `agent/run/session` того
   запуска, где создана, а `SocialService._write` не переносил ревизию в scope текущей
   команды. Закрыть обещание или обновить Person в **следующем** запуске было
   невозможно (`ACCESS_DENIED`). Memory- и development-владельцы делают перенос
   (`correct`, `_revised(scope)`), social — нет. Воспроизведено тестом, мутационная
   проверка: без исправления тест падает.

## Что изменено

- `m3_composition/dialogue.py`: собеседник приватного диалога — аутентифицированный
  вход панели, аккаунт `private-panel/operator`, а не имя из текста. Дальше работает
  существующий путь контроллера: `observe_account` → `record_encounter` → `person_id` →
  точные обязательства и `<person returning=…>` в контексте. Обещание ответа ждёт
  ACK этой же редакции; после `DELIVERED` его записывает `social.record_promise`
  (идемпотентно по utterance + key, в том числе при повторе ACK).
- `m3_composition/service.py`: `Runtime.pending_promises` — черновики обещаний до ACK, в
  памяти процесса, не больше 64, как `_pending` самого контроллера. Флаг
  `private_social`.
- `social/service.py`: `_write` переносит запись того же partition в scope команды.
  Чужой partition по-прежнему отклоняет storage.
- `local_host/rights.py`: SOCIAL пишет свои записи (Person/Encounter/Promise), читает
  MEMORY (основания) и CHANNEL (`DELIVERED` для обещания). В `record_readers()` добавлен
  social-принципал только на чтение. API transfer и доступа оператора к SOCIAL нет.
- `local_host/layout.py`, `init.py`, `serve.py`, `social.py`, `cli.py`: флаг
  `HostConfig.private_social`. Новый `init` выдаёт права и включает флаг. Для старого
  хоста — офлайн `enable-social`: переприменяет текущую матрицу work scope (других
  грантов там нет: их выдаёт только `init`), затем ставит флаг. Пока флаг выключен,
  приватный диалог идёт анонимно, как раньше. Обновление кода не ломает рабочий хост.

## Приёмка

Production-host (`build`, публичный профиль, HTTP owners, live-адаптер поверх
синтетического транспорта), `tests/memory_quality/test_private_social_continuity.py`:

- обещание появляется только после ACK. Ответ с обещанием без подтверждения ничего не
  записывает и не объявляется данным;
- имя «Ника» в тексте не создаёт и не объединяет людей. Другой аккаунт с тем же
  отображаемым именем — другой Person;
- после 6 нерелевантных реплик и после **restart** запрос модели содержит ровно это
  обязательство и `<person name="оператор" returning="true" meetings="2">`; Person один;
- отмена (`withdrawn`) в следующем запуске — новая версия, версия 1 (`open`) сохранена;
  следующий запрос без обязательства;
- если у основания обещания отозвана передача в API, обязательство в запрос не попадает;
- в TEST scope рабочих обещаний нет;
- старый хост без SOCIAL: ответ работает, Person/Promise не создаются. После офлайн
  `enable-social` тот же вход становится собеседником, обещание записывается.

Unit: `tests/social/test_conversation.py` — обещание из одного запуска закрывается в
следующем, история версий сохранена, другая персона его не видит.

## Проверки

Linux, `../venv` с `PYTHONPATH` на src этого checkout. Ничего не устанавливалось.

- `tests/social` — 22 passed; новый production-host файл — 2 passed.
- Целевой регресс (memory_quality, social, dialogue, integration history/http/memory_cycles,
  local_host guards/controls/memory/live/recovery): 144 passed, 3 skipped, 1 failed за
  1362 с. Failed — `test_guards.py::test_import_has_no_effects`: тест запускает
  `python -I`, а `-I` игнорирует `PYTHONPATH`, и venv этой машины указывает на старый
  чужой worktree без `local_host` (`ModuleNotFoundError`). Это окружение. То же
  утверждение проверено вручную на коде ветки: импорт `cli`, `serve`, `social` из пустого
  каталога — код 0, файлов не создано.
- Strict mypy — 221 файл без ошибок; ruff check/format по изменённым файлам чисто.
- Нестабильность: длинный сценарий `test_same_person…` (≈3,5 мин, restart) один раз из
  четырёх прогонов упал `CANCELLED` на поздней реплике; три повтора прошли. Похожий
  эффект был в этапе 1 на длинных сценариях (фоновое изменение версии контекста,
  `STALE_CONTEXT`). Связь с этой правкой не найдена, но и не исключена — отмечаю
  открыто.

## Ограничения и запросы владельцам

- **Обещание до ACK живёт в памяти процесса.** Restart между ответом и
  подтверждением его теряет, ложно данным оно не становится. Запрос владельцу
  contracts: необязательное поле `Utterance.promise: PromiseIntent | None` (тот же
  закрытый тип). Тогда ACK берёт обещание из сохранённой редакции и переживает restart.
  Проверка: restart между draft и ACK → после ACK Promise `open`.
- **Закрыть или исправить обещание из панели/модели нельзя.** Есть только метод
  владельца `close_promise`; ни команды API, ни намерения в `DialogueDecision` нет.
  Нужен shared change: команда `social.promise.close` (`withdrawn|fulfilled|unknown`,
  `closure_result`, evidence для `fulfilled`) или поле решения модели.
- Старые сообщения (до `enable-social`) не имеют ACL для SOCIAL и не могут быть
  основанием обещания; новые могут.
- `MemoryService._transition` (`set_rights`/`expire`/`delete`) имеет тот же межзапусковый
  эффект scope, что был у social. Это не моя зона: в тесте отзыв выполнен в scope
  текущего запуска. Владельцу memory — та же однострочная правка.
- Один оператор на хост: идентичность — логин `operator`. Для нескольких операторов
  принципал нужно передавать в `TaskInput`.

## Команды координатору

```text
git fetch origin
git log --oneline origin/main..origin/codex/private-social-continuity
# после интеграции на рабочем хосте (сервер остановлен):
python -m ai_agent_core.local_host enable-social --root <host root>
```

Следующий этап — 3 «Связная биография» (`codex/grounded-character-biography`) от свежего
`origin/main` после принятия этапов 1–2.
