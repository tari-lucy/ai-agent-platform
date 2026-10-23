# OC-01: ранний owner bridge для родного OpenCode

2026-10-23, Codex. База `main 4ae2627`, отдельные ветка/checkout
`codex/oc-01-native-bridge`. Зона: новый `opencode_ui`, узкий dispatch в access,
metadata intake неактивной области, условие wake в work_sessions.mutate,
тесты и документация. Текущая панель Claude,
generated contracts, провайдер и sandbox runner не менялись.

## Результат

Поставлен `aw-opencode/1`: авторизованный каталог, виртуальные directory,
SessionBook-списки/история, файлы и операции, команды с исходными request_id/CAS,
START и точный ACK. Direct STOP остаётся на старом независимом маршруте, а не
за каталогом или DB. Auth/Host/Origin/CSRF/request-limit, owners, release/retention,
Source и общий gateway сохранены. Собственной истории/прав/credentials нет.

Новая ветка вызывается внутри существующей HTTP boundary после аутентификации;
owner reads/commands проходят её прежние проверки, включая повторную проверку
сессии после await. Scope каталога берётся из разрешённых сервером областей,
directory только адресует их. В карточках нет канона, checkpoint или refs.
Неактивная подготовленная область может принять open/message/close без запуска
второго runtime; сообщение также не меняет next_check чужого активного контура.
Новые grants и права выполнения это не создаёт.

**Это ранняя поставка стыка, не полностью закрытый OC-01/native UI gate.**
Клод может использовать опубликованный контракт для OC-02/03. Принадлежность
всей пользовательской цепочки native UI ещё предстоит проверить в OC-03/04/06.
Пользовательский launcher и личный deployment не переключались.

## Проверки

- Windows, настоящий production local host, SQLite/Source/SessionBook/HTTP:
  STOPPED intake, replay, конфликт текста и CAS, две вымышленные личности,
  приём сообщения неактивной областью без START/inference, подмена scope/id,
  Origin/CSRF/отсутствие cookie. Итоговый адресный тест — 1 passed (14.93 s).
- Тот же host, синтетический harness и провайдер: START → один ответ → неверная
  revision отклонена → точный ACK/replay → character в истории → STOP → старые
  ACK/START отклонены. Ровно 1 activity model call, внешней сети/платной модели нет.
- Тест двух scope/личностей и отображения событий; negative HTTP-проверка всех
  методов из 162 native path templates, включая legacy, /api и experimental:
  прямой проход закрыт.
- Второй общий affected запуск: 39 passed, 1 failed (каталог). Ошибка исправлена:
  повторная валидация Profile должна использовать существующий ExtensionRegistry.
  После этого повторён только упавший тест с добавленной проверкой двух настоящих
  профилей (результат выше). Прошедшие сценарии повторно не гонялись. После уточнения wake для неактивной
  области ещё раз проверены только этот bridge intake и прежний активный intake:
  2 passed (29.74 s).
- Ruff новых файлов и затронутых access-модулей чистый. Strict mypy пяти source
  files чистый; после последнего исправления повторён на трёх новых модулях — чисто; после wake-условия на них и work_sessions.py
  (4 файла) — чисто. Import boundaries — чисто.
- Набор `tests/opencode_ui` добавлен в combined gate; полный suite не запускался.
  Зависимости/миграции/глобальные DTO/export не менялись.

На отдельной диагностике только синтетического каталога выявлен необходимый
ExtensionRegistry. При выходе из TemporaryDirectory Windows удерживал тестовый
SQLite файл; это ошибка cleanup этой диагностической команды, не работа с личной
БД. Финальный pytest-сценарий прошёл с обычным временным fixture.

## Передача Claude и открытые границы

Wire/команды/события/полный inventory:
[opencode-bridge-v1](../protocols/opencode-bridge-v1.md).

1. OC-03 адаптирует **родной** SDK/compatibility слой к этим owner API. Native
   shapes/session IDs/events не считаются совместимыми автоматически. Готового
   native Web UI, lazy assets, cache lifecycle и SSE-proxy эта поставка не даёт.
2. OC-04 держит model/tool broker и изоляцию/очистку server-кэша. Все native routes
   сейчас deny passthrough; добавлять generic proxy вместо adapters нельзя.
3. Отдельного owner API активации другого профиля пока нет. Можно смотреть и
   оставлять сообщение в разрешённой области; исполнение — один текущий runtime.
   UI должен честно показывать ожидание, без молчаливой смены адресата.
4. В Profile нет отдельного display_name: label пока persona_id, не извлечение
   имени из канона. При необходимости запросить минимальное metadata расширение.
5. Перед включением на личном host нужен узкий cross-review новой dispatch
   границы и intake неактивной области. Личный runtime этой поставкой не обновлён.
6. Новое пожелание оператора: разные настройки моделей рабочего OpenCode и
   автономного контура, одна личность/память. Доступ через аккаунт ChatGPT желателен,
   но его совместимость/credentials/budget/STOP ещё не проверены (стык OC-04).
   Текущие API-настройки не менялись. Игры/Telegram/стрим этим не подключены.

Следующий этап Codex — OC-04, после отдельного продолжения оператором. На этой
публикации остановиться; старые формы общения панели не отключать до OC-06/07.
