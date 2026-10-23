# OC-04: поставка Web UI и событий на одном origin

2026-10-23. Ранняя инфраструктурная поставка после OC-01. Это стык для OC-03/06,
не объявление подключённого native UI или готового аккаунтного доступа ChatGPT.
Родной клиент/его адаптацию собирает Claude; Codex предоставляет local_host,
статическую поставку и notifications. [Базовый API](opencode-bridge-v1.md).

## Для Claude: какой артефакт нужен

1. Собрать адаптированный **родной OpenCode Web 1.19.3** с public base `/opencode/`.
   Не создавать вместо него новый чат. Главная страница — `index.html`, локальные
   script/stylesheet/icon/modulepreload URLs в HTML начинаются `/opencode/` и
   указывают на файлы внутри сборки. Корневые `/assets/*` и внешние UI/CDN не подходят.
2. SDK обращается к `aw-opencode/1` на этом же origin, использует платформенные
   login/session, cookie/CSRF. Полноценные обращения/решения — в панели `/`.
   Не обращаться к native auth/provider/config/prompt/terminal API напрямую.
3. Frontend URL разговора: `/opencode/{virtual_directory}/session/{session_id}`,
   также поддержан проект без id после `/session`. `virtual_directory` — ключ
   `aw_<64 hex>` из каталога, либо его base64url-представление, как у upstream
   маршрутизатора. Это адрес проекции, не физический каталог. Право видеть данные
   каждый раз проверяет owner API; статическая HTML-страница его не выдаёт.
4. Убрать private browser persistence upstream: история/контекст/credentials не
   должны попасть в localStorage, IndexedDB, service workers или native DB.
   Наличие hash manifest этого не доказывает — нужна проверка OC-03/06.
5. Проверить под CSP: scripts только self (без unsafe-eval/inline), connect только
   self, нет внешних шрифтов/ресурсов, worker-src none, base-uri none, запрет frames.
   Если нужный upstream worker не работает — запросить конкретный минимальный
   стык; не ослаблять CSP самостоятельно и не включать CDN/auth bypass.
6. Из чистой проверенной поставки Python, где `ai_agent_core` соответствует этому
   коммиту, выполнить `python scripts/package_opencode_ui.py --build-dir <absolute-dist>`.
   Команда не собирает код/зависимости, не скачивает UI, не перезаписывает manifest.
   Она создаёт `platform-ui-manifest.json` с хешами уже подготовленных файлов.
   Повторный выпуск делается в новом build directory.

Manifest declares upstream_version=1.19.3, bridge_protocol=aw-opencode/1,
base_path=/opencode и files={relative_path:{size,sha256}}. Это описание проверяемого
артефакта, **не доказательство подлинности upstream, интеграции SDK или выдача прав**.
Source commit/патч и проверка происхождения остаются в отчёте OC-03.

Лимиты поставки: 512 файлов, 8 MiB на файл, 32 MiB суммарно; manifest до 256 KiB.
Не включать `.env`, node_modules, git, source maps, БД и private runtime.
Symlink/junction, выход из корня, размер/хеш не совпали — отказ. Нельзя использовать
каталог host, его подкаталог или родителя как dist. После валидации serve держит
только неизменяемые bytes allowlist; изменение файлов на диске не меняет текущую
поставку. Нет файлового API/catch-all proxy или внешнего fallback.

## Запуск после OC-03 и общего gate

```text
python -m ai_agent_core.local_host serve --root <absolute-host> --provider mock
  --panel-dir <absolute-panel-dist> --opencode-ui-dir <absolute-opencode-dist>
  --open-browser
```

Это одна команда в одну строку. Для уже разрешённого live host сохраняются его
прежние `--provider live --env-file <local-path>`; ключи не передавать в UI или
аргументами. Первую общую приёмку выполнять на временном host с synthetic/mock.

Сервер по-прежнему только loopback. OpenCode — `/opencode/`, панель — `/`, API —
прежние `/api/v1/*`. Кнопки/ссылки выбора агента делает OC-03, startup остаётся
STOPPED. UI не стартует native server, новый процесс агента или LLM lease.
Контейнер остаётся прежним bounded per-turn worker с gateway/owner MCP: закрытие
страницы освобождает подписку, не останавливает автономную жизнь. STOP — прямой
owner endpoint, независимо от static UI/notifications.

Windows `Start-Panel.cmd` поддерживает необязательную строку `opencode_ui_dir`
в `config/local.launcher.json`. Пустая/отсутствующая строка сохраняет старый вход;
путь к проверенной сборке переключит открываемый URL на `/opencode/`. Прочие поля
и старые конфиги совместимы. Сам launcher не обновляет deployment/сборки и не
читает значения `.env`. На личном host это поле пока не заполнено этой поставкой.

Статические UI-файлы доступны до login (как файлы существующей панели), но не
содержат private bootstrap. Данные API/события требуют обычной авторизации.
Без настроенного bundle `/opencode/` возвращает CAPABILITY_UNAVAILABLE, а не
старую панель под новым адресом. Вход ChatGPT в будущем — доступ к модели;
он не выдаёт автоматически операторские права на платформу/память.
Неизвестный asset — 404, encoded/traversal path — отказ, unknown native API не
маскируется HTML fallback. SPA fallback только для указанной формы virtual project
route; он не допускает `/opencode/api/session` или `/opencode/pty`.

## SSE для адаптера SDK

`GET /api/v1/opencode/agents/{directory}/events`, обычный same-origin EventSource
с операторской cookie. Токены, scopes и cursor в query запрещены. Добавлено в
`GET /api/v1/opencode/manifest` как events_sse; прежний WS остаётся совместимым.

- `event: connected`: JSON protocol=aw-opencode/1, directory, replay=refetch.
  При каждом подключении клиент перечитывает owner views. Last-Event-ID не даёт
  replay: долговечного event store нет, прошлые события не синтезируются.
- `event: refetch`, `id: <owner-event-id>`: прежняя Invalidation проекция из OC-01.
  Только идентификаторы/версии/тип owner event; нет текста модели/памяти.
- `event: closed`: фиксированный code при истечении/отзыве auth или заполнении
  очереди; закрытие соединения. До заголовков — обычные HTTP 401/403/429.
- Каждые 15 s возможен comment keepalive. Отправка ограничена 250 ms; потерянный
  клиент не держит ресурс бесконечно. Auth/TTL проверяются во время ожидания и
  перед выдачей каждого события, disconnect/overflow освобождают слот/очередь.
- Лимит подписчиков и конечная очередь общие с существующим WS (`AccessLimits`).
  SSE не занимает слот обычного HTTP command и не удерживает аренду модели.
  Scope нельзя сменить сообщением внутри потока; нужен новый авторизованный URL.

Generation chunks/terminals сюда **не передаются и не помещаются в SSE-очередь**.
Это предотвращает выдачу
предварительного/отозванного текста как готового ответа. Ответ — текущая release-
проверенная pending_reply/transcript; ACK отдельно после фактического получения,
без автоматического подтверждения при открытии/обновлении страницы.

Это не raw native SSE schema. OC-03 адаптирует notifications в SDK/invalidation,
не подменяет готовность/доставку и не запускает повтор команды при reconnect.

## Что ещё открыто

Нужен настоящий артефакт OC-03 и общий native journey OC-06/07. Аккаунтный
транспорт ChatGPT не подключён и не использован; API-назначения не менялись.
Модели двух контуров остаются отдельно настраиваемым требованием D68. Нужен
узкий cross-review auth/scope/STOP/static-boundary до личного включения. Это не
разрешение пользователю тестировать непроверенный standalone OpenCode с ключом.
