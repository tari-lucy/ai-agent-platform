# R5 Windows: OpenCode, навыки и browser ACK

База main `2f98540`, ветка/checkout исполнителя `codex/r1-agent-session`.
Оператор передал production launcher/local_host wiring Claude; здесь только
независимая Windows-приёмка, без изменений его файлов и без переноса личного host.

## Что проверено

`tests/panel/test_opencode_skills_browser.py` использует существующий production
build/HTTP/WS/owners и браузерный server fixture Claude. В отдельном тесте его
синтетический harness заменён настоящим OpenCodeSessions/DockerTaskRunner.
Это явный test-only installer с отдельными synthetic manifest/binding/Permission,
не реализация production configure/serve и не предоставление прав личному агенту.

Одна logical session, три настоящих OpenCode turn:

1. Пока STOPPED оператор пишет рабочее сообщение; модель не вызывается.
   START → MCP save_skill → experimental версия 1 → ответ персонажа.
2. Оператор просит найти метод → MCP find_skills → read_skill. Проверено,
   что настоящий запрос модели получил сохранённый метод, а не выдуманный ответ.
3. MCP save_skill с expected_version=1 → experimental версия 2. Через owner
   released_content прочитан точный новый текст. Старый метод присутствует во
   входах попытки, новый — в её результатах; статус verified не выставляется.

Каждый ответ подтверждается автоматически видимой панелью, оказывается в
истории как реплика персонажа; «сохранено, ждёт исполнения» снимается. После
reload ровно шесть сообщений, каждый ответ один раз. STOP возвращает кнопку
START, новых вызовов нет. Все три Job SUCCEEDED; ровно семь LLMAttempt,
каждый completed с ролью activity. После ходов нет runner resources/handles,
после теста нет контейнеров с нашим runner label.

## Среда и проверка

Windows, Docker Desktop Linux engine/client 29.4.0, реальный Edge через Playwright
1.63.0 (headless), OpenCode 1.19.3. Python/venv и готовая сборка панели — своего
checkout. Docker VM сообщает 12 CPU и 8171765760 bytes RAM; это доступный лимит
VM, не измеренный peak всей платформы. Проба запускает только один тяжёлый стенд.

Новый образ собран существующим scripts/build_opencode_sandbox.ps1 из ранее
проверенного локального архива с новым workspace MCP навыков; бинарник и base
digest прежние. Новых пакетов/браузеров/OpenCode на Windows не устанавливалось.

Локальный tag: `ai-agent-platform:opencode-skills-r1`.
Проверенный image ID:
`sha256:9f6ac72a79a1e92aa7c26da8e3a8b4830dd43859d4af8f57a0cb41783326d08a`.
Digest локальной сборки может отличаться на другой машине: template принимает
проверенный полный image ID, а не изменяемый tag.

Потолки: один контейнер, 1024 MiB RAM, 128 pids, 20 s CPU, 55 s wall,
32 MiB tmpfs, 64 KiB выхода, 512 output tokens; до трёх model/workspace RPC
на turn, helper MCP выключен. Параметры пользователя и права не расширялись.

Итог: **1 passed (126.05 с)**; ruff/check/format чистые. Полный suite, повторная
проверка типов неизменённого production-кода и новая сборка панели не запускались.
Предупреждения uvicorn/websockets — deprecation, зависимости не менялись.
Первый прогон упал из-за неверного распознавания роли в synthetic provider:
OpenCode формирует свой system prompt. Ошибка пробы исправлена; роль проверяется
по доверенному LLMAttempt, без изменения production gateway.

Для повторения из своего checkout (изолированный basetemp, существующий image):

```powershell
$env:AW_SKILLS_IMAGE_ID = docker image inspect --format '{{.Id}}' ai-agent-platform:opencode-skills-r1
$env:AI_PANEL_BROWSER_CHANNEL = 'msedge'
$Support = 'pythonpath=' + ((Get-ChildItem tests -Directory | ForEach-Object { 'tests/' + $_.Name }) -join ' ')
$Base = 'runtime/test-runs/opencode-skills-' + [guid]::NewGuid().ToString('N')
.venv/Scripts/python.exe -m pytest tests/panel/test_opencode_skills_browser.py -o $Support --basetemp $Base -q
```

Без AW_SKILLS_IMAGE_ID тест явно skipped и Docker не запускается. На Linux
browser channel можно не задавать, используя уже установленный Chromium.
Никакого скачивания browser/image из этого теста нет.

## Что ещё требуется для пользовательского запуска

Claude реализует offline configure/readiness/serve wiring и общий launcher.
После его поставки Codex проверяет этот путь на Windows без test-only install,
включая неполную настройку, image/config mismatch, STOP/restart и понятные ошибки.
Лишь затем — отдельно согласованный перенос личного host и его permissions.

Эта проба не открывает отдельный OpenCode TUI/Web, не проверяет качество
настоящей модели, crash/restore во время выполнения, полный выбор task goals
или отсутствие невидимой причины при отозванном основании разговора. Helpers
и staged restore проверены предыдущими отдельными поставками. Полный R5 этой
одной пробой не объявляется закрытым. Платных API и чтения личного .env не было.
