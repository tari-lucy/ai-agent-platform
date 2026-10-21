# R5 — Linux/headless часть общего запуска

Исполнитель: Claude Code. Ветка `codex/r5-linux-journey`, base main `03fb572`
(R1 первая поставка, R2, R3). Статус: **Linux/headless gate того, что есть в main** —
выполнен. Итоговый Windows/Docker gate с настоящим OpenCode — Codex (R5/AW-11).

## Что проверено одним сценарием

`tests/workplace_journey/test_linux_journey.py` — production composition (`build`/ASGI),
SQLite, владельцы memory/persona/tickets/activity/files, deletion ledger (recovery),
закрытый каталог blobs, **синтетический** транспорт модели (маршрут, не качество):

1. STOPPED: оператор открывает рабочий разговор и пишет — сообщение сохранено владельцем
   памяти, ждёт исполнения (`processed_sequence` 0); повтор запроса — тот же ответ;
   поручение сохранено; ни одного вызова модели, run STOPPED.
2. START: личность ищет метод, права нет → тикет `capability_request`; ответ оператора в
   тикете и структурное решение (grant skills) → find → save навыка → read → complete
   («Собственный вывод»).
3. Пусто в очереди → собственная цель другого вида, выбранная данными (не if/else):
   знания → отдельный тикет → grant knowledge → заметка → complete.
4. Опыт: два `workplace-result` (observation) и два эпизода `work-…` (по записанным
   файлам), два `workplace-conclusion` (interpretation); библиотека показывает навык и
   заметку.
5. Бюджет: ровно 9 вызовов модели; доля деятельности в `private-api-status` = 9.
6. Отзыв `api_transfer` источника сообщения разговора: текст в расшифровке `withheld`;
   контекст, не построенный на нём, остаётся валидным.
7. STOP: новое поручение сохраняется, вызовов нет.
8. Restart → STOPPED и тишина; START → сохранённая задача завершается одним вызовом.

**Не покрыто на Linux (Windows/Docker gate, Codex):** настоящий OpenCode harness, sandbox
runner/helper-скрипты, ответы личности в разговоре (session driver R1), delivery/ACK,
staged restore. Отдельные узкие пробы revoke/TTL/ledger/cross-scope — в
`tests/workplace_tickets`, `workplace_experience`, `workplace_conversation`,
`workplace_files`.

## Runbook (Linux, синтетика, без платных вызовов)

Окружение: Python 3.14 venv проекта, Node 22 для сборки панели, headless Chromium для
браузерных тестов (`AI_PANEL_BROWSER_EXECUTABLE`). PYTHONPATH — из
`scripts/check_combined.py` (SOURCES + HELPERS), `--import-mode=importlib`.

```sh
# Сценарий R5 и затронутые наборы
python -m pytest tests/workplace_journey tests/workplace_conversation \
  tests/workplace_activity tests/workplace_experience tests/workplace_tickets \
  --import-mode=importlib -q -p no:cacheprovider
# Браузерные (нужна собранная панель apps/panel/dist)
python -m pytest tests/panel/test_work_conversation_browser.py \
  tests/panel/test_work_browser.py tests/panel/test_library_browser.py \
  tests/panel/test_tickets_browser.py --import-mode=importlib -q -p no:cacheprovider
# Полный gate
python scripts/check_combined.py
```

Ручной прогон на отдельном синтетическом хосте (не на личном):

```sh
# cli = python -m ai_agent_core.local_host (с PYTHONPATH на sources этого checkout)
cli init --root R --operator-controls                  # live/budget — как в quality_support
cli enable-recovery --root R --store /abs/new/copies --ledger /abs/new/ledger/events.sqlite
cli enable-tickets --root R
cli configure-activity --root R --enable
cli enable-workplace-files --root R --ceiling-days 7
cli serve --root R --panel-dir apps/panel/dist --provider live --env-file /abs/synthetic.env
```

Агент стартует STOPPED; START — только из панели. Права на области выдаются решением по
тикету. С `.invalid`-привязкой после START модель недоступна — это проверка маршрута, не
запуск личности.

## Известное

- Под параллельной нагрузкой тесты со временем бывают ложными (повтор отдельно);
  `test_guards::test_import_has_no_effects` на этой машине падает всегда (stale venv).
