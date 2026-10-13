# M1-05 acceptance

Из корня с установленным Python3.14:
`python scripts/check_combined.py`.
Runner задаёт актуальные source/test helper paths и importlib mode.
78 access cases, полностью synthetic credentials/identities; real SQLite и
ephemeral loopback Uvicorn HTTP/WS пробы.
После обновления lock требуется uv sync --locked; общий recall набор использует --extra recall. Нет .env рабочего оператора,
моделей, платных API или подключения внешних каналов.

Scope/CSRF/Origin/session/service purpose, expiry/revocation, finite buffers,
STOP при блокировке DB/входа и actual lease owner проверяются поведением.
WS использует same-loop ASGI harness: обычный TestClient в другом потоке
нарушил бы thread ownership Supervisor. Private setup fixture создаёт только
новый temp binding и проверяет exclusive creation; не заменяет локальный .env.

Результаты: [results.json](results.json), [отчёт](../../docs/implementation-reports/M1-05.md).
