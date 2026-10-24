# Исправление первого входа в OpenCode

2026-10-24, Codex. Ветка `codex/opencode-login-redirect`, база main `ea20350`.
Симптом оператора: native UI без cookie перенаправлял на `/?next=%2Fopencode%2F`,
а общая browser-граница сервера запрещает query у страниц и возвращает
`400 INVALID_ARGUMENTS`. Предыдущий cutover проверял сначала login через чистую
панель, поэтому этот путь не был покрыт.

## Исправление и проверка

Патч 10 меняет только два login-redirect в native `bridge.ts`: теперь `/` без
query. Сервер, allowlist, permissions, credentials и протокол не меняются.
После login оператор открывает родное окно кнопкой панели; скрытое хранение
return-path и автоматическое перенаправление на произвольный URL не вводятся.

Серия 1–10 накладывается в отдельном Git index на точный v1.19.3 `e3c4eb2`;
дерево совпало с собранным. Bun 1.3.14/Vite build — exit 0; упаковка/load —
**938 файлов / 34 352 199 байт** без manifest, прежние limits/CSP сохранены.

Новый `tests/panel/test_native_opencode_login.py`: **1 passed, 18.02 с**, Windows
Edge, production owners временного host и synthetic harness. Покрыто: native
без сессии → чистая панель → пароль → STOPPED → ссылка OpenCode → поле ввода;
снятие cookie → повторный native-вход → чистая панель. Исходный query по-прежнему
400. Agent.start и модель не вызываются. Ruff и diff проверяются перед сдачей.
Полный suite, Docker journey и Linux typecheck повторно не запускались:
изменены только два строковых назначения URL, не типы или transport.

Исправление активировано вместе с патчем 11 в том же личном запуске. Предыдущие
bundle и backup сохранены. Реальный fresh login в Edge, переход из панели к native
prompt и STOPPED после перезапуска — passed; preflight полностью OK.
Agent.start, реальные сообщения, платные запросы и новые права не выполнялись.
