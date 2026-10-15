# Локальный host: подготовка и запуск

Одна рабочая область, один активный персонаж, только loopback. Живые API здесь
не подключаются: режим `mock` — технический, он не доказывает качество ответов.

## Требования

- Python 3.14 и окружение из `uv.lock`: `uv sync --locked` (uv ровно `0.12.23`,
  как требует `pyproject.toml`). Системные Python и чужие окружения не менять.
- Каталог данных — **новый** и **абсолютный**. Относительный путь отклоняется.

## 1. Подготовка (один раз)

```text
python -m ai_agent_core.local_host init --root /abs/path/to/host
```

Параметры: `--profile-file` (Markdown профиля; по умолчанию вымышленный пример),
`--workspace-id`, `--persona-id`, `--port` (по умолчанию 8765), `--test-scopes`
(1..7, по умолчанию 4), `--permission-days` (1..90, по умолчанию 30),
`--migrations` (по умолчанию `migrations/` репозитория, не от cwd).

`init` применяет существующие миграции, создаёт профиль и состояние через
`PersonaService`, Workspace/GM/Connection/LLMAssignment/Capability/Permission,
права каталога и пул пустых TEST-областей. Ничего не перезаписывает: непустой
каталог отклоняется. Незавершённая подготовка оставляет файл `INIT-UNFINISHED`,
и `serve` её не откроет; для повторной попытки нужен новый каталог.

Создаётся `secrets/operator-password.txt` (пароль входа) и `.hash`. Пароль
не выводится в консоль: файл читает оператор. Каталог — `0700`, файлы — `0600`,
`umask 077` на время команд. `serve` отказывается работать, если секрет доступен
другим пользователям.

## 2. Запуск

```text
python -m ai_agent_core.local_host serve --root /abs/path/to/host --provider mock
```

- Слушает только `127.0.0.1:<port>`; порт можно переопределить `--port`.
- Не мигрирует, не создаёт grants, не запускает агента: после старта и после
  каждого перезапуска состояние `STOPPED`. Запуск агента — явная команда
  `agent.start` из панели/API после входа.
- `--panel-dir /abs/path/apps/panel/dist` (необязательно) раздаёт уже собранную панель
  с того же адреса, что и API. Без параметра режим только API (корень отвечает 404).
  Сборка (`npm run build` в `apps/panel`) вне этой команды.
  - Каталог должен быть абсолютным, содержать `index.html` с корнем `#root` и все
    локальные `src`/`href` из него; иначе отказ с кодом `PANEL_DIR_NOT_ABSOLUTE`,
    `PANEL_DIR_MISSING`, `PANEL_BUILD_INCOMPLETE`. Каталог, содержащий каталог host или
    лежащий внутри него, отклоняется (`PANEL_DIR_OVERLAPS_HOST`).
  - Раздаются только обычные файлы внутри сборки; точечные файлы, символические ссылки
    наружу, `..` и соседние каталоги дают 404. `/api/*` и `/internal/*` панель не
    перехватывает: неизвестный API-путь остаётся JSON-ошибкой. Пути без расширения
    (переходы по страницам) получают `index.html`.
  - Аутентификация, `STOPPED` после перезапуска и loopback не меняются.
- `--provider` обязателен. `live` отклоняется до открытия базы
  (`LIVE_PROVIDER_NOT_ENABLED`): M3 dialogue и laboratory пока принимают только
  adapter `mock`, это граница владельца composition.

## Что даёт mock

Ответы диалога и рефлексии детерминированы; роли extractor/consolidator/game-master/
story-screen mock не отвечают (`UNSUPPORTED`). Usage не сообщается и остаётся
unknown; тарифов нет.

## Права (см. `local_host/rights.py`)

Оператор только читает (с api_transfer) у PERSONA, MEMORY, GATEWAY, CHANNEL, JOBS,
LLM, GM; в TEST-области — MEMORY, CATALOGUE, LABORATORY. Запись в WORKSPACE
(Capability/Permission/Connection) доступна только во время `init`. После
`permission_expires_at` старт закрыт (`serve` пишет уведомление); продления
командой пока нет.

## Проверка

`tests/local_host` — сквозной smoke и guards (`python -m pytest tests/local_host`).
Native Windows приёмка координатора: init/serve, реальный HTTP mock-диалог,
лаборатория/отчёт, STOP/restart и браузер с собранной панелью проверены. На Windows
init назначает владельца и защищённый DACL через icacls. Serve только проверяет
SID владельца/allow rules у hash, каталога secrets и root через штатный Windows
PowerShell; при лишних правах или недоступности проверки — SECRET_PERMISSIONS.
Serve не исправляет ACL сам. В выводе используется относительное имя файла
пароля без локального имени пользователя. Подробно: [combined review](implementation-reports/Integration-local-host-review.md).

Текущая матрица host покрывает private mock dialogue/laboratory. Ручные development
plans/receipts и перенос настроек требуют отдельного явного provisioning под
новые owner paths; serve права не расширяет. Полный запуск с пользовательским
профилем и настоящим API этой проверкой не объявляется готовым.
