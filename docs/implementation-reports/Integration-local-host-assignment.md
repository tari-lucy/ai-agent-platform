# Поручение второму агенту: Integration-local-host

Подготовлено координатором 2026-10-15 для передачи оператором. Это поручение
вступает в работу после передачи второму агенту; сам документ не отправляет
сообщения и не запускает отдельного агента.

## Цель и проверенная база

Собрать воспроизводимый host bootstrap для AI Agent Platform на Linux/Windows,
используя уже принятые owners/runtime. Это один локальный workspace/один
активный персонаж. Main на момент подготовки: 855144f5611d453ba20f5caa3c9464b30bae0c4d.
Включает private mock dialogue, lab report/download/create/catalogue/start,
settings transfer и laboratory Job event/cancel. Перед началом fetch origin,
проверить реальный origin/main, root/status/worktrees; записать фактический SHA.
Работать от актуального main в отдельном checkout и codex/integration-local-host.
Незавершённые соседние ветки не являются принятыми зависимостями.

Пользователь хочет конечный запуск со своим описанием персонажа и моделью по
API. Описание и ключи подготовит пользователь; координатор преобразует профиль
и согласует live подключение. Вымышленный профиль/mock нужны для технических
проверок. Не подменять ими приёмку качества или готовность реального API.

## Файловая зона

Можно создавать/менять:

- apps/core/src/ai_agent_core/local_host/** — явный init/serve/config/provider wiring;
- tests/local_host/** — synthetic native Linux/Windows checks и smoke;
- scripts/local-host*.ps1 и scripts/local-host*.py — новые вспомогательные entrypoints;
- docs/local-host-setup.md и docs/implementation-reports/Integration-local-host.md.

Координатор владеет contracts/exports, M3 composition и manual controls,
общим UI/client, dependency/lockfiles, deploy/Compose, migrations, .env.example,
реестром решений/общими статусами. Не менять их параллельно. Не редактировать
другие feature screens/владельцев. Нужный shared change описать конкретно:
существующий файл/символ, фактическая сигнатура, какая часть отсутствует,
минимальный требуемый контракт и отрицательная проверка. Продолжить независимые
части; не писать дублирующий owner/schema/обходной endpoint.

## Обязательное чтение и реальные точки расширения

AGENTS.md, README.md, docs/25, docs/07, строка назначения в docs/24, docs/27;
для задачи docs/15,17–20,23 и новые integration reports. До кода сопоставить
потребность с настоящими сигнатурами:

- ai_agent_core.m3_composition.new_registry / app.create_m3_app;
- Runtime.scopes/reader_access/providers/dialogue_index/lab/reports;
- SQLiteStore.upgrade/provision, PersonaService.create, state.PRINCIPALS/ROLES;
- LabRunner, lab_grants, Reports и явные report/source/artifact ACL;
- ai_agent_adapters.configuration и llm.openai_compat.OpenAICompatibleProvider;
- принятые integration_dialogue/m3_runtime fixtures — свидетельства API,
  но production code не должен импортировать tests или копировать их окружение.

Известные ограничения main: deploy/core.py пока запускает scaffold.
M3 dialogue прямо отклоняет adapter != mock; laboratory entry также mock-only.
Не удалять эти fences самостоятельно. Живое подключение — изменение владельца
composition после проверки допуска/бюджета/usage. Нужен понятный fail-closed
отказ до готовности; технический mock допускается только явным режимом.

## Что разрешено сделать самостоятельно

Принимать обычные решения внутри зоны, чинить свои ошибки и минимальные
платформенные расхождения, не спрашивая разрешения на каждый файл. Работать в
своём venv из uv.lock (uv sync --locked); не менять системный Python/чужой venv,
зависимости или lockfile. Не снижать requires-python ради установленной версии.

Явный init нового каталога может применить существующие migrations и подготовить
профиль, Workspace/GM, role assignments/capabilities/permissions, scope pool,
минимальные grants и record ACL. Это доверенная настройка по команде оператора,
не выдача полномочий через HTTP и не автоматическое расширение прав при serve.
Матрицу прав обосновать конкретными owner reads/writes; не выдавать всем всё.
Использовать пример вымышленного профиля/fixture и локальные generated auth files,
без реальных пользовательских данных/секретов в Git или отчёте.

Init не перезаписывает существующие профиль/БД/секреты; incomplete init не
выдаётся за готовый. Serve не мигрирует/не создаёт grants и не запускает агента
автоматически. Повторный запуск/restart всегда STOPPED. Только loopback-доступ
к панели; публичный VPS listener/remote access не добавлять.

## Приёмка

1. Есть документированная явная команда init и serve без test helper imports,
   cwd-зависимых Windows путей, скрытого .env копирования и import-time effects.
2. Новый synthetic runtime создан штатными owners: work/test изолированы,
   lab pool и отдельные индексы/артефакты подготовлены; права ровно нужные.
3. Аутентификация и локальные secret files закрыты средствами своей ОС;
   секреты/endpoint/header/body не печатаются. Windows ACL не объявлять
   проверенным по одному chmod; при невозможности — точный blocker координатору.
4. Реальный HTTP login → STOPPED → явный start → private mock message/final →
   явное delivery acknowledgment → persisted TEST lab/report → STOP → restart
   снова STOPPED. Один сквозной smoke; реальные API пока не вызывать.
5. Missing config/binding/permission, использованный lab scope, stale request и
   denied source не обходятся ради зелёного запуска. Unknown и отсутствующие
   цены/usage не превращаются в успех/ноль.
6. Native Linux evidence отдельно от mock/перехваченных запросов; Windows
   переносимость по коду отдельно от реально выполненных проверок.

Не требуются новый scheduler/Job owner, игра/голос/публичные каналы, скачивание
моделей, live/платные API, изменение других контейнеров/данных/volumes. Для
подготовки Compose оформить запрос координатору с нужной командой/paths/ports.

## Экономные проверки и сдача

Сначала согласованный по коду перечень используемых портов/границ в task report,
затем самостоятельная реализация. Это не ожидание отдельного разрешения.
Один meaningful affected test набор после связного изменения и один настоящий
smoke. Не гонять весь backend/browser suite после каждого патча. При новой
ошибке повторять только затронутый case; итоговый полный прогон согласует координатор.
Не запускать второй полный runtime с общей памятью; только свой synthetic root.

Перед commit проверить diff/секреты/личные пути. Небольшие commits; push только
своей ветки. Main не merge/push самостоятельно: координатор интегрирует ветки
последовательно. В отчёте: что реально проверено, точные команды/exit codes,
mock/live/непроверенное отдельно, unresolved owner requests и SHA. В начале
README отчёта не писать integrated/готов MVP, пока current-main combined gate
не выполнен координатором. Необходимые правки shared интерфейсов не скрывать
под локальными assumptions и не расширять область без запроса.
