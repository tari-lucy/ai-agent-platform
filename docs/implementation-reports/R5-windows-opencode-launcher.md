# R5: Windows, production launcher и браузерный допуск OpenCode

2026-10-23, Codex, ветка `codex/r1-agent-session`; launcher Claude принят из main
`f1115da`. Личный host, его права и настройки OpenCode не менялись.

## Что проверено

Windows, Docker Desktop 29.4.0 (Linux engine), установленный Edge, Node 24.19,
OpenCode 1.19.3. Образ уже собран и закреплён по ID:
`sha256:9f6ac72a79a1e92aa7c26da8e3a8b4830dd43859d4af8f57a0cb41783326d08a`.
Новых загрузок/установок нет. Данные вымышленные; HTTP transport провайдера
синтетический, платных вызовов нет. Docker, OpenCode, SQLite, owners, HTTP и браузер
настоящие.

- `tests/workplace_launcher/test_real_launcher.py`: 1 passed, 61.15 с. Offline enable
  с настоящей проверкой Docker/image → штатный serve.build → сообщение в STOPPED →
  START → технический capability_request без модели → решение оператора → ответ
  OpenCode → ACK → STOP → restart STOPPED, без дополнительных модельных вызовов.
- Новый `tests/panel/test_opencode_launcher_browser.py`: 1 passed, 48.03 с. Offline
  configure и production serve.build подключают driver; test-only install и заранее
  выданного Permission нет. Настоящий loopback HTTP и Edge: сообщение в STOPPED →
  START → видимая причина ожидания → «Обращения», выбор подключения/автоматического
  права, preview и подтверждение → ответ без повторной отправки сообщения →
  автоматический ACK видимой вкладки → две реплики истории → reload без дубля →
  STOPPED. Ровно один синтетический вызов activity.
- Первоначальные четыре scripted launcher-теста прошли; после расширения проверки
  недоступности отдельно проверены обе причины: 2 passed, 51.80 с. В обычной
  composition нет harness, transcript сообщает unavailable с точным кодом,
  модель не вызывается и технический тикет не открывается.
- TypeScript/build и eslint изменённых файлов чистые. 6 тестов events прошли;
  ruff check/format изменённых Python-тестов чистые. Production Python не менялся,
  полный combined gate повторно не запускался.

## Найденный и исправленный стык

Первый браузерный прогон обнаружил: ticket.changed не инвалидировал расшифровку
рабочего разговора. Технический тикет уже существовал, но открытый экран продолжал
писать «запрос появится после запуска». В affectedResources добавлено обновление
work-session-transcript для ticket.changed и connection.changed: причина ожидания
зависит от тикета, подключения и Permission. После исправления браузерный путь
проходит без ручного обновления страницы.

Промежуточный прогон дошёл до успешного пользовательского сценария и упал на
неправильном чтении supervisor из потока теста; проверка перенесена в owner loop.
Playwright session держит свой event loop: async launcher-тесты и браузерный набор
запускались отдельными pytest-процессами, а не после запуска Playwright в одном.

## Docker unavailable и ограничения

До запуска Docker Desktop настоящий enable отказал с DOCKER_UNAVAILABLE. После
запуска engine реальный тест прошёл без изменения кода/лимитов. Ветвь serve и
код в transcript проверены с подменой readiness; настоящий serve при выключенном
Desktop в браузере отдельно не запускался. Работающий Desktop ради пробы не
выключался.

Секундный deadline каждой проверки допуска runner оставлен fail-closed. На этой
Windows-машине пробы прошли, но поведение на двух CPU под полной нагрузкой не
проверено. Наблюдение Claude про SANDBOX_UNKNOWN под нагрузкой остаётся ограничением;
это не основание пропускать проверку допуска или автоматически повторять исход
unknown. Отдельного решения по настраиваемому deadline здесь нет.

Helpers через launcher, личный host, реальная модель, стресс-тест и отдельный
CMD/subprocess-сценарий не проверялись. Проверен тот же production build, который
вызывает serve, под настоящим uvicorn/HTTP, без тестовой установки driver.

## Воспроизведение

Указать существующий проверенный образ в AW_OPENCODE_IMAGE_ID; для браузерной
пробы AI_PANEL_BROWSER_CHANNEL=msedge. Использовать Python текущего checkout и
helper paths из scripts/check_combined.py (для panel также tests/panel).
Отдельными процессами:

```text
python -m pytest tests/workplace_launcher/test_real_launcher.py -q
python -m pytest tests/panel/test_opencode_launcher_browser.py -q
```

Для этой машины helper paths передавались явным pytest `-o pythonpath=...`.
Использованы разные basetemp под игнорируемым runtime/test-runs.

Следующий пользовательский этап — обновить deployment и по отдельному поручению
подключить рабочий контур личного host штатными offline-командами из отчёта
opencode-launcher.md. Реальное Permission выдаёт оператор через панель.
