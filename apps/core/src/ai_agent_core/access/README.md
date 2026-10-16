# M1-05: доступ оператора и worker

Один core process/event loop. `create_app` создаёт STOPPED Supervisor и устанавливает
ASGI access boundary. Не создавать Supervisor в другом потоке, не использовать
несколько uvicorn workers: сессии и ресурсы принадлежат одному process owner.

Без `AI_PLATFORM_OPERATOR_PASSWORD_HASH` login отвечает safe503, чтения/команды
неавторизованному клиенту — 401. Нет встроенного пароля или service grant.
Пароль проверяется scrypt-v1 (N131072/r8/p1, соль16, hash32, maxmem192 MiB):
одна CPU-проверка в отдельном thread, 5 попыток/60 секунд, очередь не создаётся.
До8 RAM sessions, absoluteTTL1h/idle15min; restart/shutdown/rollback clocks
отзывают их. logout отзывает текущую сессию, в том числе ожидающий control.
Текущий запущенный агент сам logout не останавливает; STOP — отдельная команда.

## Локальная настройка оператором

В установленном окружении проекта вручную:
`uv run python -m ai_agent_core.access.setup`.
Команда скрыто запрашивает пароль и создаёт новый ignored `.env.access`;
существующий файл не читает и не заменяет, значения не выводит.
Linux file mode0600; Windows использует права локального каталога.
Пароль/hash/worker token не отправлять агенту или в Git.
Для Compose подключить binding через `--env-file .env --env-file .env.access`;
для native core — `AI_PLATFORM_ENV_FILE=.env.access` либо process env.
Свой Redis binding остаётся в обычном локальном файле.
Хэш в dotenv должен быть в одинарных кавычках: это сохраняет символы `$`.
Добавление credentials само по себе не запускает агент и не выдаёт Capability.

По умолчанию origin `http://127.0.0.1:<core_host_port>`; разрешён один настроенный
loopback origin (localhost/127.0.0.1/::1 с явным портом, http/https).
Reverse-proxy/Forwarded/X-Forwarded-* не расширяют allowlist.
Compose публикует core только на loopback. Режим удалённого доступа не задан.
Для HTTPS cookie Secure; для локального HTTP HttpOnly/SameSite=Strict.
CORS middleware отсутствует. UI login входит в M4-01.

## HTTP и WebSocket

- POST /api/v1/auth/login: canonical OperatorLogin, обязательный Origin.
  Случайная новая cookie ai_operator_session; body OperatorSession содержит scopes,
  expires_at и nonce csrf_token. Старая cookie игнорируется при повторном login.
- GET /api/v1/auth/session: сессия/стабильный CSRF nonce для reload UI.
- POST /api/v1/auth/logout: сессия+Origin+X-CSRF-Token, 204 и cookie deletion.
- GET /api/v1/{resource}[/{id}]: четыре обязательных partition query fields,
  limit1..200/default50, cursor ID; dialogue collections additionally support order=desc
  with an opaque time/id cursor (max512), deliveries accept exact utterance_id. Проверка scope до и после awaited read.
  Текущий RunSession доступен из RAM; SQLiteReader использует существующие
  repository ACL/TTL/source rights и purpose=api_transfer. Reader не подключается
  к рабочей БД автоматически; без owner composition — safe503.
- POST /api/v1/commands: canonical PanelCommand; Control/Stop идут Supervisor,
  остальные владельцы пока закрыты. М1-05 не реализует бизнес-команды M2.
- POST /api/v1/agents/{agent_id}/stop: прямой RAM путь; нет journal/Redis/LLM await.
  Отдельная transport capacity8 и body4KiB/2sec, остальные HTTP requests до64.
  Общий body1MiB, header16KiB, URL/query2KiB; owner wait10sec; ошибки fixed ApiError.
  STOPPING/FAULTED не объявляются подтверждённым освобождением resources.
- WS /api/v1/events: cookie+Host+Origin до accept; первый frame EventSubscription
  до4KiB/2sec, scope до получения события. До32 подключений включая handshakes,
  до256 metadata events/client; slow client закрывается 1013, auth error1008.
  Logout/expiry проверяются каждый цикл, нет idle renewal через фоновые события.
  Scope/run/session/resource filters; incoming commands запрещены.
  after_event_id пока закрывается: refresh projections, без повторения model call.
  STOP/control публикуют run.changed; фоновый outbox всех владельцев ещё не подключён.
  Producers публикуют только canonical PanelEvent в Events; stream/chunk owner
  не подключён. deploy/core.py ограничивает ws_max_size4096/ws_max_queue1; deflate отключён.
  Явно выбран проверенный backend websockets (locked17.2/Uvicorn0.54).
  Legacy backend выдаёт deprecation warnings; при смене lock отдельно проверить
  backpressure/queue semantics нового backend. Не менять auto mode без этой пробы.

## Service identity и композиция

Bearer token — независимые случайные32 bytes/64hex из локального binding.
Worker получает только service token; operator hash ему не передаётся.
Service HTTP не использует browser Host/Origin, иначе core:8080 в Compose
конфликтовал бы с внешним loopback портом. Любые Origin/cookie/CSRF запрещены.
Служебные пути доступные на том же listener всегда требуют purpose-specific
identity и grants; их физическое разделение сетью/портом не заявляется.

Trusted AccessPorts(worker=WorkerGrant(...), jobs=JobOwner(...), extensions=registry)
задаёт partition/capability allowlists. Токен без WorkerGrant ничего не разрешает.
Authority выдаётся на20sec; JobOwner проверяет capability/policy/current admission/
worker owner/attempt/lease version. Никаких прав из body/URL.

POST /internal/v1/jobs/{operation_id}/claim возвращает ServiceClaimReceipt:
command+lease или null/null/acquiredFalse. При повторном active claim acquiredFalse:
не повторять вычисление. /lease с job.renew возвращает ServiceLeaseReceipt;
job.cancel_receipt — Receipt только после реальной остановки исполнителя.
/result принимает canonical ServiceResultCommand и отдаёт technical Receipt.
HTTP клиент не задаёт transient/retry_safe; job.progress пока safe503.
Это не permission на effect: worker соблюдает current admission, BudgetLease и
JobOwner/Policy checks M1-03/04/06 перед IO и после await; result не пишет чужую БД.
Нет автоматического создания DB/schema/grants/capabilities/worker execution loop.

create_app(access_factory=lambda supervisor: AccessPorts(...)) позволяет построить
JobOwner для того же нового Supervisor в owning thread. Чужой Supervisor у JobOwner
отвергается; access_factory/access_ports одновременно запрещены. Прямой install_access
также требует тот же owner. Recovery/readiness остаются отдельной явной композицией.

ExtensionRegistry и Ports передаются только trusted core composition. Не создавать
параллельный owner, обходную модель Lease или второй storage для подключения M2.
