# Integration-local-host — явные init/serve локального host

Дата: 2026-10-15. Исполнитель: второй агент (Claude Code).
Base: d2e473e (origin/main на момент работы; в поручении указан 855144f — разница
только документ docs-коммита). Ветка: codex/integration-local-host, отдельный checkout.
Статус: ветка сдана на интеграцию. Не называть integrated/готовым MVP, пока
координатор не выполнил current-main combined gate.

## Порты и границы (согласовано по коду до реализации)

| Граница | Используется |
| --- | --- |
| Сборка | `m3_composition.new_registry`, `app.create_m3_app(settings, store, registry, providers=, scopes=, reader_access=, dialogue_index=, lab=, reports=)` |
| Хранилище | `SQLiteStore(path, extensions=)`, `.upgrade(dir)`, `.provision(scope, grants)`, `.check_schema()` (в Runtime.open) |
| Профиль | `PersonaService(storage, PRINCIPALS[PERSONA], registry, reader_access=).create(CreateCommand)` |
| Записи владельцев | `m3_composition.state.put/record/extension`, `PRINCIPALS`, `ROLES` |
| Лаборатория | `LabRunner(storage, scope_factory, index_factory, report_access=)`, `lab_grants()`, `Reports(root, storage, registry, readers=, api_transfer_allowed=)` |
| Доступ | `BootstrapSettings`, `access.password_hash`, scrypt-v1 |
| Сеть | один listener `127.0.0.1:<port>` (uvicorn, workers=1); исходящих соединений нет |
| Файлы | `<root>/{host.json, secrets/, data/platform.sqlite, data/dialogue-index.sqlite, data/lab-indexes/, data/reports/}` |

Production-код не импортирует `tests`, не читает `.env`/окружение, не использует cwd.

## Результат

- `apps/core/src/ai_agent_core/local_host/`: `init`, `serve` (`python -m ai_agent_core.local_host`),
  `layout` (каталог, секреты, права ОС), `rights` (матрица прав), `provider` (явный mock и
  fail-closed live), `cli`. Тонкие `scripts/local-host.py|.ps1`.
- `init` только в новый пустой каталог: миграции, профиль/состояние, Workspace/GM/Connection/
  LLMAssignment/Capability/Permission, grants, пул пустых TEST-областей, пароль оператора.
  Файл `INIT-UNFINISHED` убирается последним шагом; `serve` такой каталог не откроет.
- `serve` не мигрирует, не создаёт grants, не стартует агента; слушает только loopback.
  `--provider` обязателен; `live` отклоняется до открытия базы.
- Запись WORKSPACE (Permission/Capability/Connection) выдаётся только на время `init`;
  далее права переустанавливаются без неё: runtime не может сам расширить разрешения.
- Документация: `docs/local-host-setup.md`.

## Матрица прав (проверена вычитанием)

Оставлено только то, что при удалении ломало сквозной сценарий: SUPERVISOR (журнал запуска),
JOBS→BEHAVIOR, BEHAVIOR/JOBS/GATEWAY→MEMORY; каждое подтверждено падающим прогоном.
Не потребовались и убраны: SOCIAL, PERSONA→MEMORY, GM→MEMORY, LLM→WORKSPACE,
CATALOGUE→MEMORY (в рабочей области; в TEST-областях оно остаётся для Reports).
Первая попытка вычитания была ошибочной (шаблон замены не сработал, результат не
считался); перепроверено корректно. Оператор — только чтение; MODERATION и
public_disclosure не выдаются.

## Выполненные проверки (native Linux, Python 3.14.8, мой venv из uv.lock)

| Команда | Результат |
| --- | --- |
| `python -m pytest tests/local_host` | exit 0, 9 passed, ≈63 с |
| `ruff check` по local_host, tests/local_host, scripts/local-host.py | exit 0 |
| `python -m mypy` (конфиг проекта, 182 файла) | Success, 0 ошибок |

**Сквозной smoke** (`test_smoke.py`, реальный подпроцесс `serve` + HTTP по loopback, mock,
вымышленные данные, сгенерированные локальные секреты): без входа 401; неверный пароль 403
без токена; login → сессия `STOPPED` → `agent.start` → `ACTIVE` → `dialogue.message` →
приватный final (`draft`, не публичный) → `dialogue.acknowledge` → Delivery `DELIVERED` →
LLMAttempt `completed` с usage `unknown/unknown`, без токенов и тарифа → `laboratory.create`
(резервирование области пула) → `laboratory.start` → сохранённый TEST-отчёт и скачивание
JSON (`mock=true`, findings) → устаревший `expected_version` 409 → повтор использованной
лабораторной области 409 `VERSION_CONFLICT`, второго отчёта нет → STOP → `STOPPED` →
перезапуск процесса: login и снова `STOPPED`. Лог serve не содержит пароля и текста сообщения.

**Guards** (`test_guards.py`): права каталога 0700/0600 и владелец; пароль не в выводе init;
init не перезаписывает (непустой каталог, повторный запуск, относительный путь); неверные
опции ничего не создают; serve отказывает на несуществующем/незавершённом host, на `live`,
без `--provider`, на секрете с правами 0644; импорт без эффектов; матрица прав узкая.

Находка: библиотеки создают БД/WAL/индексы с 0644. Защищал только каталог 0700;
добавлен `umask 077` в командах (проверено тестом по всем файлам каталога).

## Дополнение: `serve --panel-dir`

Модуль `local_host/panel.py`, параметр необязателен, без него поведение прежнее.
Проверки (`tests/local_host/test_panel.py`, native Linux): отказы с кодом для
относительного/отсутствующего/пересекающегося с host каталога и неполной сборки (нет
`index.html` или файла из него); раздача `/`, страниц и ассетов; 12 путей выхода
(`..`, `%2e%2e`, `\\`, host.json, БД, секреты, dotfile, symlink наружу, соседний файл)
дают только 400/404 без маркеров содержимого; неизвестные `/api/*` и `/internal/*` без
HTML; без параметра корень 404; один smoke панель+API на одном адресе: login → `STOPPED`
→ start → STOP → `STOPPED` → рестарт: сессия не переживает (401), после входа `STOPPED`.
Дополнительно вручную открыта реальная сборка `apps/panel/dist` из соседнего checkout:
`/`, JS и CSS отданы с верными типами (в Git не входит). Браузерная работа панели против
этого host не проверялась (панель в основном UI-only, см. отчёты M4).

## Не проверено / ограничения

- **Live не вызывался**, сети не было. Качество ответов и готовность реального API не оценены.
- **Windows**: код с `icacls` не запускался; ACL и пути на Windows не объявляются проверенными.
- Не покрыто: WebSocket-чанки, `session.reflect/end`, extract/consolidate/gm (mock на эти
  роли отвечает `UNSUPPORTED`, прав на них нет), панель и статическая раздача UI,
  повторяющиеся negative «denied source / отсутствующий Permission» (остаются в
  `tests/integration_dialogue`, `tests/m3_runtime`; здесь не дублировались).
- Срок Permission ≤90 дней; после `permission_expires_at` старт закрыт. Команды продления
  нет (в моей зоне её можно добавить отдельным небольшим коммитом по запросу).
- Наблюдение: STOP сразу после появления отчёта вернул `STOPPING` с неподтверждёнными
  ресурсами (бюджет и job), затем `STOPPED` в пределах опроса. Поведение штатное, но
  причину задержки подтверждения я не исследовал.
- Установленный uv 0.12.9 ≠ требуемого 0.12.23: окружение собрано
  `uvx --from uv==0.12.23 uv sync --locked`, системный uv не менялся.

## Запросы координатору (owner requests)

1. **Live-подключение** (владелец composition). Файл/символ: `m3_composition/dialogue.py`
   (`binding.adapter_id != "mock"`, ≈стр.163) и `m3_composition/service.py` (лабораторный
   вход отклоняет `lab.providers/resolve/mock is not True`, ≈стр.750); `create_m3_app(
   allowed_adapters=)`. Нужно: после допуска/бюджета/usage снять fence для явно разрешённого
   адаптера и принять фабрику, использующую `OpenAICompatibleProvider(settings)`; отрицательные
   проверки: нет ключа/endpoint → отказ без IO, неизвестные usage/цена не становятся нулём.
   После этого в `local_host/provider.py` ветка `live` подключается в моей зоне.
2. **Compose/контейнер** (deploy). `deploy/core.py` — scaffold. Нужна команда
   `python -m ai_agent_core.local_host serve --root <volume> --provider mock`, том для
   `<root>`, порт. Host слушает только 127.0.0.1; для контейнера нужен решённый вариант
   публикации (без публичного listener) — решение за координатором, обходов не делал.
3. **Раздача панели**: путь собранного UI и способ его монтирования (общий UI/client).
4. (Информация) uv 0.12.23 нужен на машинах разработчиков, иначе `uv sync --locked` отказывает.

## Изменённые файлы

Только файловая зона поручения: `apps/core/src/ai_agent_core/local_host/**`,
`tests/local_host/**`, `scripts/local-host.py|.ps1`, `docs/local-host-setup.md`, этот отчёт.
Контракты, миграции, lock-файлы, Compose, `.env.example`, реестры не менялись.
