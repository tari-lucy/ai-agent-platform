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
  не готовый host — `unavailable` с кодом, тикета нет; CLI отдаёт фиксированные коды.
- `tests/workplace_launcher/test_real_launcher.py` — **настоящий OpenCode/Docker на Linux**
  (`AW_OPENCODE_IMAGE_ID`): enable с реальной проверкой образа → сообщение → START →
  системный тикет → решение → ответ OpenCode → ACK → STOP → restart STOPPED. На VPS
  (Ubuntu 24.04, Docker 29.1.3, 2 CPU / 3 GB) — 1 passed (48.6 с), образ собран
  `build_opencode_sandbox.py`, синтетический провайдер, без сети.
- Наблюдение для runner (зона Codex, не менялась): при параллельной полной нагрузке на 2 CPU
  ход стабильно завершался `SANDBOX_UNKNOWN` без вызова модели — runner ограничивает каждую
  проверку допуска `wait_for(_verify, timeout=1)`, а TimeoutError считает неизвестным исходом.
  На свободной машине проходит. Для слабого host стоит решить, остаётся ли 1 с.
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

## Итог проверок (base main `1b36577`)

- `tests/workplace_launcher` (scripted) + `workplace_sessions` + `workplace_conversation` +
  `workplace_harness` + `local_host`: 104 passed, 11 skipped, 2 failed — 
  `test_import_has_no_effects` (stale venv этой машины, падает всегда) и
  `test_panel_and_api_on_one_address_smoke` (падает только в групповом прогоне, отдельно
  `test_panel.py` — 4 passed; так же вёл себя на main без этой ветки).
- `test_real_launcher.py` с настоящим OpenCode/Docker на Linux — 1 passed (48.6 с).
- Панель: tsc, eslint, 203 node tests, build; headless Chromium — разговор, ACK, работа,
  обращения — 6 passed. Пока агент STOPPED, «Чей ход» по-прежнему говорит про запуск;
  причина ожидания показывается после START, ссылка на обращение — когда запрос открыт.
- Ruff, strict mypy (290 source files), import boundaries, generated exports — чисто.
- Полный combined gate этой ветки не повторялся (gate R5 на предыдущей ревизии — в отчёте R5).

## Изменённые shared-файлы

- contracts: `ai_agent_contracts/panel.py` (`WorkSessionTranscript.waiting_reason`,
  `waiting_detail`, `access_ticket_id`) и `packages/contracts/generated/*`;
- composition: `m3_composition/service.py` (`Runtime.session_unavailable`),
  `work_session_runtime.py` (вызов запроса доступа в `select`), `work_sessions.py`
  (причина в расшифровке); новый `work_session_access.py`;
- local_host: `layout.py`, `serve.py`, `cli.py`, новый `work_sessions.py`;
- панель: `shared/validation.ts`, `ui/work/conversation.ts`, `ui/work/Conversation.tsx`;
- тесты/скрипты: `tests/workplace_sessions/session_support.py` (`synthetic()` вынесен из
  `install`, без копии), `scripts/check_combined.py` (+`tests/workplace_launcher`), новый
  `scripts/build_opencode_sandbox.py`; docs: `deploy/opencode/README.md`, `manual-mvp-check.md`.
