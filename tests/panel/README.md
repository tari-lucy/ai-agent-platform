# M4-01: браузерные сценарии

Требуются синхронизированное Python3.14 dev-окружение проекта (Playwright),
построенная apps/panel/dist и установленный браузер. Тесты не скачивают браузер,
не читают .env, не используют рабочую БД/память, Redis или LLM.
Сервер — временный loopback Uvicorn на свободном порту, с вымышленным паролем;
запуск/остановка сервера и браузера принадлежат fixtures.

После npm run build --prefix apps/panel:

```text
uv run --locked --no-sync python -m pytest tests/panel --import-mode=importlib -q
```

По умолчанию используется установленный Playwright Chromium.
На Windows можно задать process env AI_PANEL_BROWSER_CHANNEL=msedge;
для отдельного установленного executable — AI_PANEL_BROWSER_EXECUTABLE.
Пользовательские browser profiles не используются.
Проверки браузера opt-in по каталогу; общий backend runner их не запускает.
Node tests API/cache/scope/WS: npm test --prefix apps/panel.

Настоящее ядро M1: login/cookie/CSRF/session reload/logout, direct STOP,
WS Upgrade, отказ чужого Host/Origin, STOP при заблокированном чтении.
Остальные проверки: deterministic projections с двумя разными scopes,
STOP активному owner при просмотре другого test-профиля, STOPPING/UNKNOWN,
семь маршрутов, узкое окно/клавиатура, reconnect без повторения команд,
HTML как текст, foreign projection и expiry/cache isolation.
Mock не подтверждает интеграцию dialogue/memory/GM/usage.

Снимки только синтетические, сохраняются в ignored runtime/panel-*.png.
Отчёт приёмки — docs/implementation-reports/M4-01.md.
