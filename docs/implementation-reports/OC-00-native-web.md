# OC-00: родной Web OpenCode, версия и граница подключения

2026-10-23, Codex. База: main `39d56f6`. Отдельные checkout/ветка
`codex/oc-00-native-web`. Зона: opt-in probe, отчёт и документационный стык;
контракты, личный deployment и файлы текущей задачи Claude не менялись.

## Вывод

**Продолжаем с родным Web OpenCode 1.19.3.** Проверенный установленный binary
содержит Web assets: основной HTML, JS и CSS отдаются при network=none.
Скачивание интерфейса с внешнего сервиса для этой поставки не потребовалось.
Технического основания переходить на TUI пока нет. По последнему поручению
оператора TUI допустим как запасной путь, если дальнейший bridge/Web gate
обнаружит существенное препятствие. Его отдельное подключение здесь не выполнено.

Это проверка native surface, **не готовый пользовательский вход в платформу**:
родные сессии и API ещё не связаны с owners личности/памяти/разрешений.
Не направлять пользователя на отдельный OpenCode server с собственным ключом.

## Реальная проба

Образ уже находился на машине:
`sha256:9f6ac72a79a1e92aa7c26da8e3a8b4830dd43859d4af8f57a0cb41783326d08a`.
Его закрепление/checksum binary описаны в deploy/opencode/Dockerfile.

Проба запускает только свой временный контейнер: pull=never, network=none,
read-only root, uid/gid 65534, cap-drop ALL, no-new-privileges, 1 GiB RAM,
2 CPU, 256 pids; tmpfs /tmp 128 MiB и /work 16 MiB. Нет mounts, host env,
публикации портов, Docker socket, личных данных и ключей провайдера.
После проверки контейнер удаляется по собственному случайному имени; другие
контейнеры и тома не затрагиваются.

Внутри запущен настоящий `opencode serve --hostname 127.0.0.1 --port 19090`.
Настройки взяты из существующего trusted worker: один фиктивный platform/bound,
отключены обновления, models fetch, project config, plugins, фоновые title/
summary/compaction и внешние MCP. Model URL ведёт на счётчик-заглушку контейнерного
loopback; число обращений к нему — **0**. Prompt не отправлялся.

| Проверка | Результат |
| --- | --- |
| /global/health | 200, healthy=true, version=1.19.3 |
| /doc | 200, OpenAPI 3.1.0, 162 различных path templates |
| Главная страница | 200, text/html, 2884 байта |
| Основной JS | 200, text/javascript, 2738111 байт |
| Основной CSS | 200, text/css, 482489 байт |
| Остальные ссылки из HTML | Все 7 локальных assets суммарно вернули 200 |
| Создать → прочитать → удалить вымышленную native session | 200 → 200 → 200 |
| /event и /global/event | 200, text/event-stream; первое событие server.connected |
| Без Basic credentials: /, /doc, health, sessions, provider, config, path | 401 |
| GET с чужим Origin и правильными credentials | 200, без Access-Control-Allow-Origin |

Последний результат означает: CORS ограничивает чтение браузером, но сам по себе
не является проверкой полномочий команды. Native Basic auth также не заменяет
Scope, операторскую сессию и CSRF платформы.

SHA-256 полученного /doc:
`eebb56042880e07c354e0ad0a31f70aab45f536adf5b40b6b1fd5abf4663417b`.
Основной JS: `f50056f89aaa6d96b12c2532734b7b3142a68a37d9a96bc58ef1dfb2ead7b7d8`.
Основной CSS: `deca26a5c1d635fd592671e2cade5dafa63b1047693e01be4923721a0e62a6b4`.

Даже без диалога сервер создал собственные SQLite DB/WAL/SHM и log в tmpfs
(около 0.51 MiB суммарно). Это технический кэш, не память персонажа.
Его нельзя резервировать/восстанавливать или выдавать в обход owners.
После завершения контейнера эти файлы исчезают вместе с tmpfs.

## Точный протокол для OC-01/03/04

У **этого же 1.19.3** /doc содержит legacy routes и новые /api/* routes.
Нельзя считать все /api/* чужой версией или пропускать их общей proxy-веткой.
Закреплённый клиент сначала проверяет /global/health и при healthy=true выбирает
v1 compatibility; затем использует legacy global event stream. Некоторые методы
совместимости продолжают использовать новые API: недостаточно реализовать три
legacy session endpoint и считать весь родной клиент совместимым.
Основания: [server-protocol.ts](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/app/src/utils/server-protocol.ts),
[server-sdk.tsx](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/app/src/context/server-sdk.tsx),
[server-compat.ts](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/app/src/utils/server-compat.ts).

| Native поверхность | Что требуется от bridge |
| --- | --- |
| /global/health; bootstrap project/path/config/provider/agent | Проверенная проекция настроенной платформы; без ключей, физических путей host и чужих профилей |
| /session, /session/{id}, /message, /prompt_async, /session/status | SessionBook/Source и разрешённая история; новые сообщения — штатные команды с request_id/CAS, не прямой native prompt |
| /event, /global/event | SSE-адаптация owner events; фильтр Scope и проверка авторизации после ожидания, повторное чтение после разрыва |
| /file, /find, /skill и их /api аналоги | Только ManagedFiles/SkillLibrary с release; никакого произвольного host path |
| /permission, /question и их /api аналоги | Адаптация обращений и структурированных решений существующих owners; native allow не создаёт Permission платформы |
| Native abort | Может отменять соответствующий текущий ход; глобальный STOP платформы остаётся отдельным явно видимым действием |
| /auth, provider OAuth, credential/integration writes, config PATCH | По умолчанию отказ; ключи и изменение провайдера из native UI не допускаются |
| /pty, shell, command, worktree/git mutations, share, sync, upgrade | По умолчанию отказ, включая aliases под /api и experimental; скрытая кнопка не является защитой |

Это матрица требований к следующей реализации, а не уже действующий allowlist.
Полная машинная инвентаризация с operationId получается воспроизводимым probe.
В production нельзя проксировать «всё остальное» в native catch-all: он умеет
обслуживать UI, а при отсутствии embedded assets — обращаться к внешнему upstream.
Поведение exact source: [shared/ui.ts](https://github.com/anomalyco/opencode/blob/v1.19.3/packages/opencode/src/server/shared/ui.ts).

## Рекомендуемая реализация в рамках принятого плана

1. OC-01 публикует версионированный адаптер native wire → существующие owner
   команды/чтения. Авторизация платформы, CSRF для изменений, scope/cursor/limits,
   request replay, source release и точный ACK не делегируются native серверу.
   Directory/session ID клиента — только адрес проекции; право проверяется заново.
2. OC-03 адаптирует **родной upstream клиент закреплённой версии**, используя его
   SDK/compatibility стык. Не писать новый основной чат. Список персонажей идёт
   от Workspace/Profile, разговор — от SessionBook; Build/Plan не личности.
   Нельзя сохранять private transcript/контекст/credentials в browser persistence.
3. Браузер получает assets и API через один локальный вход платформы.
   Нативные endpoints публикуются по явному списку операций; /api/v1 платформы
   не перехватывается native catch-all. Смена native base path должна проверяться:
   клиент использует абсолютные URL, один reverse-proxy prefix сам всё не решает.
4. OC-04 сохраняет inference в уже ограниченном harness/model gateway.
   Предпочтителен прежний network=none и host ↔ container stdio; server остаётся
   внутри контейнерного loopback. UI не получает native password/gateway token.
   Точный двунаправленный SSE/stdio transport, отмена и backpressure — отдельная
   проверка OC-04; данная проба проверила только вызовы внутри контейнера и вывод
   метаданных в Windows. LAN/listener и Docker host mounts не добавлять.
5. Долговечны только записи owners. Native cache ограничен tmpfs/lifetime;
   release/revoke/restore очищают технические проекции. UI работает при STOPPED,
   native prompt не может обойти START, JobOwner, lease, epoch или общий бюджет.

Минимальная адаптация выглядит осуществимой: assets есть, у клиента выделены
SDK/compatibility точки. **Это вывод из проб и кода, не измеренный объём патча**.
Полная bootstrap-совместимость, ограничения browser cache, перенос draft/ACK и
выбор двух профилей должны быть доказаны OC-03/06; голый server их не предоставляет.

## Воспроизведение и границы

```powershell
python scripts/probes/opencode_native_surface.py --image-id sha256:9f6ac72a79a1e92aa7c26da8e3a8b4830dd43859d4af8f57a0cb41783326d08a --output runtime/probes/oc-00/native-surface.json
```

Использовать рабочий Python проекта и доступный Docker Desktop. Скрипт ничего
не устанавливает и не получает образы. JSON содержит только вымышленные
метаданные/route inventory; runtime игнорируется Git.

Проверки: реальная offline Docker-проба, ruff и strict mypy только нового скрипта,
отдельная компиляция встроенного container source. Полный suite не запускался:
production-код не менялся. Реальные API, личный host и его START не использовались.

Не проверено: полное выполнение JS в браузере, все lazy assets, бесконечный SSE
и reconnect, двунаправленный bridge, model/tool enforcement через native routes,
две личности, delivery/ACK в родном UI, TUI attach. Это gates следующих OC-задач,
а не доказанные свойства данной HTTP-пробы. Существующая панель и личный launcher
продолжают работать по прежнему пути.
