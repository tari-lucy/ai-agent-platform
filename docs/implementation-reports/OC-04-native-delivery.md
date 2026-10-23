# OC-04: локальная поставка клиента и поток уведомлений

2026-10-23, Codex. База `main d4ef924`, отдельная ветка/checkout
`codex/oc-04-native-delivery`. Это завершённый инфраструктурный этап OC-04,
**не полная приёмка native OpenCode Web или ChatGPT account transport**.

## Сделано

- `serve --opencode-ui-dir` подключает проверенный dist на `/opencode/` одного
  loopback origin. Панель остаётся `/`, owners API не перехватываются. Без сборки
  адрес честно закрыт CAPABILITY_UNAVAILABLE вместо старой панели под новым URL.
- Packaging уже собранного native adapter через `scripts/package_opencode_ui.py`:
  manifest/version/base, bounded files/size, хеши, отказ symlink/junction/traversal,
  запрет overlap с host. Serve загружает только проверенные неизменяемые bytes,
  не читает личную файловую систему по URL. Unknown native API не получает SPA HTML.
- Статическая поставка не запускает native server/его DB/cache, модель или агента.
  Сохраняется прежний bounded per-turn harness/owner MCP/gateway. Нет новых Docker
  mounts/портов/образов/моделей или credential stores. CSP ограничивает соединения
  self, скрипты self, запрещает workers/frames/eval; совместимость native bundle
  с этими границами проверяет клиентская поставка OC-03.
- SSE `/api/v1/opencode/agents/{directory}/events` использует ту же cookie,
  Host/Origin/Scope/срок сессии и существующий Events. Отдельного архива нет:
  подключение/reconnect явно требует refetch, Last-Event-ID не имитирует replay.
  Только owner notifications, без текста модели/памяти и без автоматического ACK.
- Очереди/подписчики конечны и общие с WS. В Events добавлен optional
  notifications_only (для прежних клиентов default=false). Model chunks не
  помещаются в SSE buffer. Logout/expiry/disconnect освобождают подписку;
  ограничена отправка медленному клиенту. HTTP slots/LLM lease/direct STOP не заняты.
- Старый Windows launcher совместим с пятью полями, принимает дополнительный
  opencode_ui_dir. Если указана готовая сборка, открывает `/opencode/`; пустая строка
  сохраняет прежний вход. Локальный launcher/личный deployment не менялись.

## Проверки

- Исходный affected запуск: 25 passed, 1 failed (слишком широкий SPA fallback
  принимал `/opencode/api/session`). Исправлено: страницы только для virtual
  directory/его base64url alias, неизвестные native routes остаются 404.
  Повторён только упавший тест — passed.
- Browser transport: реальный CLI init/serve на временном host, Edge через
  Playwright, синтетический dist. Загрузка JS, same-origin EventSource,
  reload/reconnect, direct STOP → owner event/refetch, restart/start не выполнялись.
  Чтение RunSession осталось STOPPED, LLMAttempt journal пуст. Это не native UI.
- Chromium Playwright не был скачан; первая проба отказала по отсутствию binary.
  Использован установленный Edge, без установки. Следующая проба выявила, что
  string wait_for_function теста требует eval под CSP; заменён DOM locator.
  Политика безопасности не ослаблялась. Browser + Windows launcher — 2 passed.
- Финальный affected gate после защиты отсутствующей сборки/expiry и фильтра
  брокера: 9 passed (25.01 s). Включены все новые static/SSE tests, настоящий
  browser transport, Windows launcher, существующая WS scope/logout проверка.
  Ранее прошедшие полный access-events/process наборы повторно не гонялись.
- Ruff чистый; strict mypy 11 изменённых source files — чистый. Diff и локальные
  ссылки проверены. Полный suite, контейнеры, реальные/платные LLM не запускались.
- Браузерный тест opt-in `AW_OPENCODE_BROWSER=1`, использует стандартные
  `AI_PANEL_BROWSER_CHANNEL`/`AI_PANEL_BROWSER_EXECUTABLE`; проверен channel=msedge.
  Он включён существующей папкой tests/opencode_ui в combined gate, по умолчанию
  пропускается без явного browser environment, не вызывает download.

## Что требуется для пользовательского теста

1. Настоящий upstream adapter dist от Claude OC-03: base `/opencode/`, SDK к bridge,
   login, выбор профиля/сессии, message/progress/ответ/точный ACK и ссылки на тикеты.
   Пакетировать тестовую страницу как замену native UI нельзя.
2. OC-02: проверенный контекст той же личности/памяти. Проверка private browser
   persistence upstream, native SDK shapes и общего пути — OC-03/06.
3. Узкий cross-review новых SSE auth/scope и static boundary до личного включения.
   Затем OC-07 — обновление deployment с backup и конфигурация готовой сборки.
4. ChatGPT account access ещё не проверен/подключён. Не выдавать его за уже
   работающий второй транспорт; API bindings и бюджеты в этой поставке не менялись.
5. Прежний runner всё ещё ограничивает одну `_verify` проверку 1 s. Под нагрузкой
   это ранее отмечалось Claude как риск unknown. Лимит не менялся и нагрузочная
   Docker-проба здесь не повторялась; это отдельная проверка полного OC-04 gate.

Передача Claude: [полный стык сборки/событий](../protocols/opencode-delivery-v1.md).
После публикации остановиться по указанию оператора. Пустой или standalone native
OpenCode не предлагать как готовую среду персонажа.
