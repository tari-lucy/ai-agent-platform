# OC-01: опубликованный стык `aw-opencode/1`

Дата: 2026-10-23. Потребитель — адаптер SDK родного OpenCode Web 1.19.3 (Claude,
OC-03). Это работающий owner bridge, **не native API OpenCode** и не новый чат.
Родные assets/client пока к нему не подключены. Самостоятельно запускать native
server с ключом провайдера вместо платформы нельзя. Инварианты — doc30/D64–D67.

## Вход и адреса

Один loopback origin с платформой. Вход/восстановление браузерной сессии через
существующие `/api/v1/auth/login` и `/api/v1/auth/session`; HttpOnly cookie,
Origin/Host и CSRF прежние. Bridge не вводит пароль, OAuth, refresh token или
учётную запись. Все ответы `no-store`, включая проекции, история не сохраняется
в localStorage/IndexedDB/native cache. Auth после restart недействительна.

Префикс: `/api/v1/opencode`. `GET /manifest` публикует версию/доступные команды.
Пути ниже относительны префиксу. Не передавать физический каталог в `directory`.

| Метод/путь | Ответ/поведение |
| --- | --- |
| GET `/agents` | `Catalogue`: protocol, items, complete=true. Не больше 8 уже разрешённых оператору областей с читаемым Profile. Карточка: directory, scope, profile_id/profile_version, label, active_runtime |
| GET `/agents/{directory}/sessions?limit=50&cursor=...` | `SessionPage`, прежний ограниченный cursor владельца. В карточке только id, scope, title, version, generation, state, last_sequence, processed_sequence, created_at, updated_at. Контекст/refs/checkpoint не выдаются |
| GET `/agents/{directory}/sessions/{session_id}` | Прежний `WorkSessionTranscript`: release/withheld, generation через owner, entries, pending_reply, progress и причины ожидания. Частичная история обозначена complete=false |
| GET `/agents/{directory}/run` | Прежний `RunSessionPage`: пустой items у неактивной области. Не подставлять активного агента вместо выбранного |
| GET `/agents/{directory}/files` | Прежняя `WorkplaceLibrary`, только разрешённые owner-проекции |
| GET `/agents/{directory}/files/{file_id}` | Прежний `WorkplaceFileContent`, текущая release/TTL/ledger проверка |
| GET `/agents/{directory}/operations/{operation_id}` | Прежняя `Operation`, включая OUTCOME_UNKNOWN без преобразования в failed/retryable |
| POST `/agents/{directory}/commands` | Точная существующая `PanelCommand`, ограниченная allowlist ниже; ответ — прежний Receipt. Scope команды обязан совпадать с directory |

Непредусмотренный метод/маршрут — отказ; query разрешает только limit/cursor на
постраничных списках. Ни arbitrary path, ни клиентские persona_id/workspace_id
в query не меняют выбранную область. Ключ directory определяется как `aw_` +
SHA-256 канонического JSON массива Scope.partition (ASCII, без пробелов).
Он стабилен между run/reload, но не является правом доступа. Каждый запрос снова
сверяет его с областями авторизованной сессии и читает владельца.

Логический session_id — id SessionBook, **не id процесса OpenCode/run**.
Первый client adapter обязан использовать пару (directory, session_id), даже
если заголовки разговоров совпадают. Не создавать второй реестр сессий.
Технические native IDs/cache OC-04 связывает с этой парой и generation.
Если upstream требует свой формат ID, OC-03 делает обратимое отображение
транспорта, не создаёт вторую историю. Подмена directory/чужого id закрывается
owner scope, а не фактом знания alias.

`label` сейчас равен persona_id: отдельного display_name в Profile нет.
Не извлекать имя из приватного канона и не хардкодить Опечатку. Human-friendly
метаданные профиля — согласованное расширение owner-проекции, если потребуются.

## Команды, квитанции и доставка

Разрешены только `work.session.open`, `work.session.message`, `work.session.close`, `work.session.rename`,
`agent.start`, `dialogue.acknowledge`. Тело/ExtensionPayload/ожидаемая версия —
ровно из `packages/contracts/generated/contracts.ts` и прежнего API.
Bridge не переписывает request_id, scope, expected_version или revision.

- Open/message/close используют stable Scope и существующий SessionBook/Source.
  При STOPPED сообщение сохраняется, inference не выполняется. В уже разрешённой
  и подготовленной неактивной области intake также допустим; это не активация.
- Rename меняет только заголовок с CAS/replay, без нового сообщения, модели и START.
  Во время подготовленного/выполняемого хода возвращает RESOURCE_BUSY, чтобы не сбить
  текущий ответ. Даты карточки/расшифровки — сохранённые даты owner-записи, не время GET.
- Request ID удерживается для той же отправки, в том числе при потере ответа.
  Replay возвращает прежнюю квитанцию; другой текст с тем же ID даёт 409.
  Устаревший expected_version даёт 409, черновик остаётся у оператора.
- START требует точные scope/version выбранного текущего RunSession. Клиент не
  выполняет его при просмотре/reload/создании разговора. Автоматического выбора
  другого runtime нет. Переключение активного персонажа ещё не опубликовано:
  в MVP один исполнитель, другой профиль можно просматривать/оставлять сообщение.
- **STOP вызывается прямо** `POST /api/v1/agents/{agent_id}/stop`, существующим
  `StopCommand`. Не отправлять STOP через bridge/catalogue и не ждать загрузки БД.
  Это сохраняет отдельный admission/request-limit и независимость остановки.
- Ответ читать из release-проверенного transcript.pending_reply. Редактор не
  показывает raw model/tool JSON как ответ личности. generation.chunk — только
  предварительный вывод, не подтверждение действия или доставки.
- ACK — только точные utterance_id, version, revision и **run scope из draft**,
  командой dialogue.acknowledge после фактического получения/видимости. Сам GET,
  поток, скрытая вкладка и предварительный фрагмент не вызывают ACK.
  Native читатель должен реализовать это в OC-03, bridge сам не подтверждает.
- Owner проверяет epoch/run/исход/release и записывает доставку/character Source
  и опыт один раз. Старый draft после STOP/revoke не возрождается. При unknown
  читается прежняя операция; новый request_id не создаётся автоматически.

HTTP статусы/коды — прежние 401/403/409/429/503; ошибок с текстом провайдера нет.
Обычные ответы и права по обращениям остаются в полноценной панели D67. Здесь
нет endpoint для native allow/remember или permission.decision: клиент даёт
ссылку на существующий тикет, а не обходит preview/execute.

## События

Сохраняется уже принятый `/api/v1/events` WebSocket с `EventSubscription` и Scope.
Долговечного replay store нет: старый after_event_id требует перечитать проекции.
OC-04 добавляет owner notifications через SSE:
`/api/v1/opencode/agents/{directory}/events` (connected/refetch/closed).
Это не raw native schema и не поток готовых ответов;
[поставка, auth, пределы и стык SDK](opencode-delivery-v1.md).
`opencode_ui.protocol.invalidation(PanelEvent, selected_scope)` выдаёт только
directory, event_id, owner_event, resource_id/resource_version и type=refetch;
чужая partition даёт None. Подписку/авторизацию проверяет существующий сервер.

`work-session.changed`, `job.changed`, `utterance.changed`, `delivery.changed`,
`run.changed`, `ticket.changed`, `usage.changed` — сигналы перечитать соответствующие
owner view. Событие не несёт память или готовый private text и не выполняет команду.
После обрыва перечитать release-проверенные представления: никакого повтора START,
message или ACK с новым ID. Не выдавать cache/replay события за актуальный допуск.

## Полная native поверхность и границы

[Инвентаризация exact 1.19.3](opencode-1.19.3-routes.json): 162 path templates,
все methods/operationId из OC-00 с SHA-256 исходного /doc. Каждый отмечен
`deny_native_passthrough`. `/api`, legacy, experimental, auth/provider/config,
PTY/shell/git/share/sync/permission/prompt не проксируются общим catch-all.
Это классификация **прямого прохода**, а не отказ от необходимых функций UI:
OC-03 адаптирует нужные методы к опубликованным owner-проекциям выше.
Остальные функции показываются как недоступные, а не исполняются native server.
Получение настоящих HTML/JS/CSS и server transport/isolation — OC-04/03.

## Два назначения моделей — уточнение оператора

Оператор хочет независимо настраивать рабочий контур OpenCode (в перспективе
доступ через аккаунт ChatGPT) и API-модели автономного персонажа, игры, Telegram,
стрима и памяти. Это одна личность/память/опыт и разные назначения/транспорты LLM.
Не вводить отдельного персонажа Build/Plan или независимую память OpenCode.

OC-01 не привязан к провайдеру и не меняет нынешние назначения. Аккаунтный доступ,
его доступность в установленной версии, безопасное хранение credentials и
совместимость с gateway/STOP/bounded budget **ещё не проверены и не подключены**.
Это явный стык OC-04: такой транспорт нельзя незаметно подменить API-ключом,
пропустить мимо допуска или считать расходы подписки нулевыми. Лимиты/usage
контуров показываются раздельно, недоступные измерения — unknown. Не копировать
учётные данные оператора в персонажную песочницу и не открывать native /auth
через bridge. Отдельная поставка auth не является зависимостью публикации OC-01.
