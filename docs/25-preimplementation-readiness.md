# Готовность к реализации: итог планирования

Актуализация 2026-10-21: ниже исторический итог первого MVP, не утверждение
об отсутствии сегодняшнего кода. Подключённые функции — [текущая сводка](mvp-current-state.md).
Новая [концепция рабочего места](28-agent-workplace-concept.md) зафиксирована;
[AW-план](29-agent-workplace-implementation-plan.md) учитывает существующий
runtime и равноправную интеграцию Codex/Claude. После фиксации плана оператор
разрешил coding-пулы AW; текущие назначения/зависимости — doc29. Это не закрывает
оставшиеся старые M5/Compose/quality gates.

Дата: 2026-10-13. **Планирование завершено; M0-01–M0-06, M1-01–M1-06, M2-01–M2-07, M3-01–M3-04, M5-01–M5-02 интегрированы.**
Среда, каркас, Compose, RU recall, безопасный bootstrap, contracts, SQLite и
supervisor/direct STOP, durable jobs/Redis, telemetry/scheduler и
authenticated HTTP/WS/service access проверены. M3 composition, private mock
HTTP dialogue, production local host и ручные owner controls подключены;
живой API и полный M4/M5 gate ещё не приняты. Фактические интеграционные стыки —
[актуальная сверка](implementation-reports/M4-M5-integration-readiness.md).
Текущие назначения/интеграция — [doc24](24-mvp-implementation-plan.md).
Пользователь поручил самостоятельно довести план до конца; дополнительные
технические defaults выбраны в этих пределах, а не выданы за отдельно
произнесённое согласие по каждому пункту. История — [реестр D01–D52](07-decisions.md).

## 1. Что готово

- Требования R01–R26, границы первого текстового MVP и будущих эпиков.
- Модульные владельцы, состояния, API/версии, сохранение/отмена/права.
- Устойчивая личность, память/отношения и развитие через проверенный опыт;
  заметный внутренний GM без голоса, сюжетные границы и операторский контроль.
- Составная память и выбранный CPU stack, сравнительные пробы и fallback.
- Семь областей панели, формы и empty/error/degraded состояния.
- Docker/Redis, бюджеты, retention, backup/delete/restore и runbook.
- Каталог зависимых задач с acceptance и правила веток нескольких разработчиков.

Основные точки входа: [стек/память](20-stack-recall-selection.md),
[экраны](21-mvp-panel-spec.md), [тестовый профиль](22-reference-persona.md),
[эксплуатация](23-project-delivery-operations.md),
[список задач](24-mvp-implementation-plan.md),
[будущие интеграции](26-future-integrations.md),
[Codex/Claude Code](27-coding-agents-workflow.md).

## 2. Покрытие исходных требований

«Покрыто» ниже означает наличие архитектуры, задачи и критерия проверки.
Не означает, что функция написана или прошла испытания.

| ID | Содержание | Реализация/приёмка |
| --- | --- | --- |
| R01 | Настраиваемая личность | M2-01, M4-03; профиль заменяется, state/канон разделены |
| R02 | Память и продолжение после restart | M2-02/03/06, M5-02/03; проверенный multi-session цикл |
| R03 | Вводные/тесты/основание реакции | M3-04, M4-02/06; scope/срок/отчёт |
| R04 | Следование/препятствия/ручное управление | F2-01/02; finite recovery и lease ввода |
| R05 | Исследование/наблюдения/выводы | F2-01/02, M3-01; доступность мира и provenance |
| R06 | Голос/чат/нет повторных вопросов | M2-03/06, F1, F2-03; тема/участник/одна реплика |
| R07 | Live2D | F3-01; mapping/actual playback/neutral on cancel |
| R08 | YouTube/Twitch/вводные/зрители | F4-01/02; platform IDs и public rehearsal gate |
| R09 | Посты со скриншотами | F5-01/02; sources/exact revision/operator release |
| R10 | Отдельный монтаж | F6-01/02; EDL/non-destructive/worker/export |
| R11 | Логи/STOP/ограничения | M1-03/05/06; independent revocation/failure scenarios |
| R12 | Замена провайдера/среды без смены личности | M1-01, M2-04, F1/F3; adapter contract/mocks/binding |
| R13 | GM и развитие по выбору/опыту | M3-01/02/03; no forced outcome, bounded state |
| R14 | Видимые линии/границы/контроль | M4-05; четыре представления, evidence/rollback |
| R15 | Независимая модерация | M2-07, F4/F5/F6 release gate; exact version, fail closed |
| R16 | Рабочее пространство агентов | M1, M2-01, F7-01; profiles/tools/resources отдельно |
| R17 | Невысокие требования к железу | M0-03/05, M5-04; CPU/API baseline, budgets measured |
| R18 | Local/API STT и TTS | F1-02/03; все четыре комбинации |
| R19 | Markdown и несколько видов хранения | M2-01/02, M5; authoritative/derived source rules |
| R20 | Биография/находки/эфиры/посты | M3-01, F2/F4/F5; original experience dedup |
| R21 | Люди/отношения/повторные встречи | M2-03/06, M3-03; stable IDs/evidence |
| R22 | Комментаторы и зрители в той же памяти | M2-03, F4-01/F5-02; verified account links |
| R23 | Docker | M0-04/06; три сервиса/volumes/loopback |
| R24 | Устойчивые очереди | M1-04/06; DB/outbox/lease/retry/reconciliation |
| R25 | Агрегатор/streaming | M2-04; endpoint/key env, provisional vs final/cancel |
| R26 | Модели/цены/токены/расходы | M2-05, M4-08; versions/unknown/attempt ledger |

## 3. Независимые проверки полноты

| Риск | Предусмотренный механизм | Где проверить |
| --- | --- | --- |
| Персонаж меняется от каждой реплики/однотипный сюжет | Independent evidence, bounded rate, future manifestation, calm periods/cooldown | doc22, M3/M5-04 |
| Неточный recall забывает обещание | Точные people/topics/promises до semantic search | doc18/20, M2-03/06 |
| Пересказы усиливают ложную память | Provenance/dedup/конфликты и исправление | doc03/18, M3-01 |
| Тест/другой профиль/площадка смешались | Scope на источнике/индексе/команде, ID и проверка owner | doc17–20, M1/M2/M3-04 |
| Поздняя модель/worker действует после STOP | Epoch/lease и повторная проверка допуска; usage отдельно | doc15/17/19, M1-03/04 |
| Дубли/потерянный ACK/результат неизвестен | Operation IDs, outbox, terminal state и сверка без слепого эффекта | doc15/19, M1-04 |
| Backup/версии оживляют удалённый текст | Tombstone/expiry ledger, revoke до очистки, restore gate | doc23, M5-01/02 |
| Dashboard выдаёт оценку за точный счёт | Rate versions, неизвестное, attempts/cached/reasoning semantics | doc16/23, M2-05 |
| Модерация своей речи не покрывает весь эфир | Отдельный public media/foreign input path, rehearsal/release gate | doc10/26, F4 |
| Разработчики одновременно меняют контракт | File ownership, отдельные checkout, contract-first integration | doc27, AGENTS/CLAUDE |

Каждый механизм имеет задачу и сценарий; запретов, которые держатся только
на характере LLM, в критичном контуре быть не должно.

## 4. Осталось проверить при реализации

| Неизвестное | Когда и кто закрывает | Поведение до проверки |
| --- | --- | --- |
| Exact Python/package/image/model versions, sqlite-vec loading | M0, исполнитель probe и интегратор | Ветки выбранного стека — план, не готовая сборка |
| Actual RAM/latency/recall и mixed load | M0/M5; позднее F4 rehearsal | Resource defaults, никаких обещаний совместной работы по наличию CUDA |
| Агрегатор URL/key, role model IDs/capabilities | Оператор локально + M2-04/M5-04 | Mock; нет платных вызовов без настроенного binding |
| Реальные ставки/валюты | Оператор в dashboard | Token counts/unknown money; нет исследования цен сейчас |
| Финальный авторский персонаж | Оператор при настройке профиля | Заменяемая вымышленная Искра достаточна для тестов |
| Game/version/server rules | F2, оператор/исполнитель bridge | Local test world, без неизвестного чужого сервера |
| Audio/voice rights/rig mappings | F1/F3 | Mock/CPU candidates; без объявления готового голоса/аватара |
| Право/территории/аудитория/channel rules/public delay | До реальных F4/F5/F6 выходов | Public disabled; ни одна LLM не объявляет все нормы соблюдёнными |
| Channels/OAuth/bot scopes/edit presets | На входе F4/F5/F6 | Приватные draft/fixtures; секреты локально |

Ни один пункт не требует ещё одного архитектурного обсуждения до M0.
Если проба опровергает выбор, применяется заранее указанный fallback и запись
решения; неизвестный параметр не зашивается произвольной догадкой.

## 5. Текущая граница

Планирование завершено; по поручению оператора M0-01–M0-06 и M1-01–M1-04
интегрированы. Созданы каркас, contract schemas, Compose, SQLite/migrations,
supervisor, durable job/outbox/lease/reconciliation и отдельные модули persona/memory.
Runtime-подключение persona/memory ещё не выполнено. Это базовые компоненты,
а не завершённый текстовый MVP: access handlers, scheduler, dialogue/LLM и
остальные owners ещё требуют своих задач и meaningful acceptance.

M1-06 по поручению оператора реализована первой: bounded telemetry и resource budgets,
без authenticated handlers/реальных executors. Следующая задача — M1-05. Блок M2 выполняет
второй coding agent; его зоны не входят в текущее поручение. Обычная реализация/
согласованная локальная интеграция назначенных задач разрешена оператором.
Реальные LLM/публичные/игровые эффекты требуют отдельной авторизации. Новая
миграция получает очередной номер после принятого head 0003 у координатора.

M1-05 integrated62a8fb7: operator/session/CSRF/Origin/service grants, direct STOP,
real HTTP/WS and SQLite paths verified. Combined Windows510/Linux511 и strict
types77/lint/import/export passed; core768 MiB auth78 cases no OOM.
Единственное новое runtime prerequisite — websockets17.2 в frozen uv.lock.
После pull синхронизировать своё окружение; реальный VPS/cold image/live provider
этими проверками не объявляются принятыми. Подробности — [M1-05](implementation-reports/M1-05.md).


### Основа панели M4-01 — 2026-10-14

Оболочка, operator login/session, shared API/WS/cache и independent STOP
подготовлены в task/m4-01-panel-shell; приёмка **UI-only / review**.
Windows/Linux client23/build и Edge11 browser cases прошли; полный
backend cycle M2/M3 и остальные экраны ещё требуют интеграционной проверки.
Порты: apps/panel/README.md; приёмка: docs/implementation-reports/M4-01.md.
Shared panel shell/API client остаются у M4-01; будущие screen owners
подключают свой экран через эти порты, не создают альтернативные клиенты.

### Integration-contracts / private dialogue — 2026-10-14

Приватный mock путь M2/M3 через настоящий HTTP/WS и SQLite соединён с панелью:
[Integration-dialogue](implementation-reports/Integration-dialogue.md).
Public схемы/target/CAS/rules/catalogue — [Integration-contracts](implementation-reports/Integration-contracts.md).
Это явный entrypoint для подготовленной среды; default deploy остаётся закрытым.
Остальные UI-only формы и M5 host/operations/live-model gates не приняты этим этапом.
