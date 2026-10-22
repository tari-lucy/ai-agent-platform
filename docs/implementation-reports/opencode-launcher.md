# Launcher рабочего контура: OpenCode при обычном serve

Исполнитель: Claude Code. Ветка `claude/opencode-launcher`, base main `a4351ab` (+ R5 Linux
restore). Поручение оператора/Codex 2026-10-22; вариант первичного допуска (системный тикет)
согласован Codex.

## Результат

- **Offline-конфигурация** `enable-work-sessions` (`local_host/work_sessions.py`, раздел
  `work_sessions` в `host.json`): неизменяемый image ID, области и область вывода, потолки
  runner (память, pids, CPU/wall, tmpfs, вывод, вход/файлы, вызовы модели/токены, вызовы MCP) —
  данные, без привязки к персоне. Требует files + tickets + включённую деятельность. Проверяет
  образ (Docker отвечает, ID совпадает, Linux, label версии OpenCode 1.19.3), пишет manifest и
  ready binding `opencode.<capability>`, по `--ceiling-days` — потолок решения оператора.
  Ничего не скачивает и не собирает, Permission не пишет, START не делает. Повтор безопасен;
  смена областей отклоняется (`WORK_SESSIONS_AREAS_FIXED`), смена образа — только конфигурация.
- **Подключение при serve** (`serve._build`): при наличии раздела — проверка готовности и
  существующий `OpenCodeSessions(...).install()` с шаблоном из конфигурации. Выключено по
  умолчанию. Не готово (нет Docker/образа, нет деятельности) — разговоры ждут, причина кодом:
  stderr `NOTICE: work conversations unavailable: <CODE>` и в панели. `check-work-sessions` —
  отдельная проверка готовности.
- **Первичный допуск** (`m3_composition/work_session_access.py`, одна строка в
  `work_session_runtime.select`): после START, если ждёт сообщение оператора, capability и binding
  готовы, а права нет, — владелец тикетов открывает один `capability_request` с автором
  `service` («Для рабочего разговора требуется разрешение»; явно технический запрос платформы,
  не решение персонажа). Без вызова модели и без выдачи права. Один тикет на версию capability
  и binding: повторные tick/сообщения/разговоры/restart не создают дублей. Отказ оператора
  (закрытие тикета) не переоткрывается; новый запрос — только после явного «решено» оператором
  или смены capability/binding. Право — на рабочий контур в пределах областей, срока и общего
  бюджета: новые разговоры и перезапуски его переиспользуют. После выдачи ждущие сообщения
  исполняются без повторной отправки (обычные проверки session driver). Блокируется только
  разговор; остальные занятия идут. STOPPED — ничего; restart — STOPPED, нужен START.
- **Панель**: `WorkSessionTranscript.waiting_reason/waiting_detail/access_ticket_id`
  (`unavailable`, `access_required`, `access_requested`, `access_refused`); «Чей ход» называет
  причину, ссылка «открыть обращения».
- `scripts/build_opencode_sandbox.py` — Linux-аналог `.ps1` (тот же архив, SHA-256, состав,
  контекст; сборка без сети, базовый образ по digest — явным `--pull-base`).

## Проверки

- `tests/workplace_launcher/test_session_launcher.py` (production build, scripted harness над
  общим gateway, готовность образа подменена): системный тикет → одно решение → оба ждущих
  сообщения отвечены, ACK; новый разговор без нового тикета; повторные tick и второй разговор
  без дублей; STOP → новое сообщение ждёт → restart STOPPED → START отвечает; отказ не
  переоткрывается и после restart+START; STOP во время ожидания ничего не исполняет;
  не готовый host — `unavailable` с кодом, тикета нет. 3 passed.
- `tests/workplace_launcher/test_real_launcher.py` — **настоящий OpenCode/Docker на Linux**
  (`AW_OPENCODE_IMAGE_ID`): enable с реальной проверкой образа → сообщение → START →
  системный тикет → решение → ответ OpenCode → ACK → STOP → restart STOPPED.
- Панель: tsc, eslint, node tests.

## Команды (синтетический host, не личный)

```sh
# 1. Образ (один раз; скачивает архив OpenCode и базовый python по digest)
python scripts/build_opencode_sandbox.py --pull-base      # печатает sha256:<image id>
# 2. Host (cli = python -m ai_agent_core.local_host, PYTHONPATH на sources checkout)
cli init --root R --operator-controls --live-model <model>
cli enable-recovery --root R --store /abs/copies --ledger /abs/ledger/events.sqlite
cli enable-tickets --root R
cli configure-activity --root R --enable
cli enable-workplace-files --root R --ceiling-days 7
cli enable-work-sessions --root R --image-id sha256:<id> --area projects --area inbox \
    --output-area projects --ceiling-days 7
cli check-work-sessions --root R
# 3. Запуск
cli serve --root R --panel-dir apps/panel/dist --provider live --env-file /abs/.env
```

В панели: «Работа» → написать → START → «Обращения»: технический запрос → решение «выдать»
(области, срок) → ответ в разговоре → получение. Право действует до срока/отзыва.

## Оставшиеся Windows-проверки (Codex)

- Тот же путь обычным `serve` на Windows/Docker Desktop: `enable-work-sessions` с image ID из
  `.ps1`, `check-work-sessions`, недоступный Docker Desktop → `DOCKER_UNAVAILABLE` в панели.
- `test_real_launcher.py` на Windows; браузерный путь через панель с этим launcher (вместо
  test-only install в `test_opencode_skills_browser.py`).
- Helpers (`max_helper_calls`) launcher не включает — отдельное решение.
- Подключение личного host — отдельно, после проверки штатного запуска.
