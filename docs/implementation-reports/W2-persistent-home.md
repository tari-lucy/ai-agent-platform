# W2: постоянная рабочая папка, /guide и shell

Claude Code, 2026-10-26. План: [doc31 §3, §9](../31-agent-workspace-v2.md), решение D70.
Ветка `claude/w2-persistent-workspace` от main `0657f0c`. Инструкция оператора:
[agent-workspace.md](../agent-workspace.md).

## Что сделано

- **Volume на личность.** `aw-home-<sha256 раздела>[:32]` — по workspace/persona/
  memory space/mode, work и test раздельно (`work_session_home.volume`). Runner
  создаёт его идемпотентно с меткой `ai-agent-platform.home=1` и монтирует в
  `/workspace` (`policy.create_args(..., home)`); контейнер по-прежнему один на ход,
  удаляется после хода, volume остаётся. Чужой путь хоста смонтировать нельзя:
  имя проверяется регулярным выражением.
- **/guide.** Tmpfs 1 МиБ; вход пишет `AGENTS.md` (правила оператора из
  `<root>/guide/AGENTS.md` или текст по умолчанию), `connections.md` (тот же текст
  устройства среды, что в системном промпте, плюс занятое место и порог),
  `workspace-readme.md` (первые 8000 байт README агента). Файлы 0444, каталог 0555,
  пишутся заново каждый ход, поэтому правка изнутри ничего не меняет. На пустом
  volume создаются `README.md` (стартовый), `inbox/`, `share/`.
- **Права OpenCode с домом.** `bash`, `glob`, `grep`, `list`, todo,
  `external_directory`; `read` везде, `edit` в `workspace/*` и `outputs/*`;
  `instructions` = `/guide/AGENTS.md`, `/guide/workspace-readme.md`. Без дома
  конфигурация прежняя. `webfetch`, `task` по-прежнему запрещены.
- **Окружение.** `PYTHONUSERBASE`, `NPM_CONFIG_PREFIX`, `PIP_CACHE_DIR` и начало `PATH`
  указывают в volume; XDG-каталоги и история OpenCode остаются в tmpfs `/work`.
- **Прогретый контейнер.** Volume монтируется при подготовке для активной личности;
  `claim` принимает только тот же volume, runner отвергает несовпадение. `/guide`
  пишется при активации из свежего payload, а не из подготовленного.
- **Образ.** `Dockerfile.harness-refresh` (и полный `Dockerfile`): `/workspace`
  (65532, 0700), `/guide`, метка `ai-agent-platform.workspace.protocol=1`.
  `refresh_opencode_harness.py` проверяет метку. Хост без метки: readiness
  `OPENCODE_WORKSPACE_IMAGE_UNEXPECTED`, runner — отказ до регистрации контейнера.
- **Настройка.** `WorkSessionsConfig.home_enabled` (по умолчанию выключено),
  `home_quota_mb` (порог-предупреждение, 4096); CLI `--home/--no-home`,
  `--home-quota-mb`. Права не выдаются, агент остаётся STOPPED.
- **Найдено и исправлено.** Описание инструмента `bash` OpenCode — 4633 символа,
  а `ToolDefinition.description` ограничивал 4000: любой ход с bash падал как
  `SANDBOX_UNKNOWN` до вызова модели. Лимит поднят до 16000 (общий размер промпта
  ограничен отдельно).

## Проверки (Linux VPS, Docker 29)

- ruff, mypy (311 файлов) — чисто.
- `tests/workplace_sandbox`, `workplace_harness`, `workplace_launcher`, `tests/llm`,
  `test_environment_guide`, `access/test_setup`: 211 passed, 36 skipped.
- Новые без Docker: `test_home.py` (имя volume, монтирование, границы текстов,
  правила оператора, права OpenCode), `test_home_wiring.py` (метка образа,
  `enable-work-sessions --home`, свежее чтение правил, CLI).
- **Настоящий Docker** (`AW_HOME_IMAGE_ID` — образ из `refresh_opencode_harness.py`
  поверх локального pinned `opencode-1.19.3`): `test_home_docker.py` — файл и
  исполняемая утилита в `.local/bin` переживают новый контейнер и новый runner,
  запись в `/`, `/usr/local/lib`, `/guide` отклонена, другой volume пуст, образ без
  метки отклонён; `test_real_home.py` — **настоящий OpenCode** с синтетической
  моделью: bash пишет в `/workspace`, `touch /planted` отклонён, правила `/guide`
  в системном промпте, следующий контейнер читает файл; прогретый путь монтирует
  volume при подготовке, отдаёт свежий `/guide` и не принимает чужой volume.
- Полный gate: см. итог ниже.

Не проверено: Windows/Docker Desktop (WSL2) — владелец volume и права на нём,
живая модель, поведение агента с реальным промптом. Это проверка Codex.

## Риски

- Docker не ограничивает размер volume; порог — предупреждение агенту.
- С bash агент управляет процессами контейнера и может исказить свой журнал хода;
  хост по-прежнему проверяет всё на границе (модель, файлы, тикеты, лимиты).
- Забывание разговора не удаляет volume (решение doc31 §3); удаление — действие
  оператора.

## Для проверки на Windows

1. `refresh_opencode_harness.py` поверх pinned-образа → новый ID.
2. `enable-work-sessions ... --image-id <ID> --home`, `serve`, START.
3. Попросить агента создать заметку в `/workspace/projects/test/` и показать `ls -la`;
   в следующем сообщении — прочитать её; перезапустить сервер — заметка на месте.
4. Opt-in тесты: `AW_HOME_IMAGE_ID=<ID>` для `tests/workplace_sandbox/test_home_docker.py`
   и `tests/workplace_harness/test_real_home.py` (PYTHONPATH с `tests/workplace_sandbox`).
