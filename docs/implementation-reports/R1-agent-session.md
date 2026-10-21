# R1 — рабочая сессия личности

Дата: 2026-10-21. Исполнитель: Codex. Base `ab269dd`, ветка
`codex/r1-agent-session`, отдельный checkout `runtime/development/r1-agent-session`.
Оператор возобновил свою часть обновлённого плана после D62/D63. Передача
Claude принята: [handover](claude-r1-handover.md); AW-08 не была начата.

Статус: **in progress**. Первая поставка — контракт и activity owner сессий.
Это не запущенный OpenCode, не готовый рабочий разговор и не runtime enable.

## Принятый стык для R2/R3

- `ai_agent_contracts.agent_sessions`: WorkSession и WorkSessionMessage.
  Один существующий owner ACTIVITY, стабильная persona/WORK-or-TEST область,
  отдельная generation контекста. State open/waiting/closed/invalidated — логика
  дела, не ACTIVE supervisor. Ни сериализованная сессия, ни checkpoint не
  разрешают исполнение. Checkpoint — только ArtifactRef, без raw host paths.
- Сообщение хранит SourceRef, порядковый sequence и speaker, не второй body.
  Текст хранит существующий владелец Source с его retention/правами/ledger.
  Внешний ввод не назначает sequence, speaker, generation или API permissions.
- Wire contracts: CreateCommand `work.session.open`, target `work`, extension
  `work.session.open-args` (title, optional task_id). EditCommand
  `work.session.message` и `work.session.close`: target=session ID,
  expected_version, соответствующие `…-args` (session_id, для message ещё text).
  Событие `work-session.changed`; модели чтения work-sessions/work-session-messages.
  JSON Schema/OpenAPI/TS перегенерированы. **Названия приняты, HTTP dispatch и
  production registration ещё не подключены**; не выдавать API за working journey.
- `SessionBook` (`agent_activity/sessions.py`): реальные SQLite/CAS/receipts,
  open/append/close и SessionHistory. Авторизация текущего оператора обязательна,
  как и callback владельца Source: проверка exact source/version/body, TTL и
  ledger. Компонент не доступен как агентный tool. Квоты задаются конструктором.
  Его operator intake не требует ACTIVE, но не вызывает модель/runner/START.
- Последовательность не зависит от hash-ID: `messages(after_sequence, limit)`
  читает ожидаемые IDs по sequence; скрытый/утраченный элемент — отказ, не
  «полная пустая история». Unique keys защищают sequence и source внутри сессии.
- `SessionContextProvider.build(admission, session, max_messages, max_chars)`
  и immutable `SessionContext` — агрегирующий порт R1. Контекст содержит session_id,
  generation, through_sequence, ChatMessages, **все реально использованные**
  sources/files. SessionHistory даёт ordered refs; bodies — через Source owner.
  through_sequence не означает исполнение. R1 сверяет generation/admission и
  использует существующий ReleaseGuard до/после IO; refs входят в WorkAttempt.
- Existing lifecycle знает оба новых вида и all-revision зависимости
  session↔message, session→task/checkpoint/material/source. Отзыв основания
  может инвалидировать всю неразделимую историю. Второго ledger нет. Текущий
  production guard WORKPLACE_RESTORE_NOT_READY автоматически включает новые kinds.

## Проверено

Windows Python 3.14.8, отдельный frozen/offline venv, реальные SQLite и ledger.

- Session owner + прежние workplace contracts: **11 passed, 1.69 с**.
- После добавления exact source/body binding: sessions + реальный purge с
  session/history/marker: **5 passed, 2.54 с**. Старые file/skill/ticket copies
  удаляются тем же RetentionService; marker не остаётся в DB/blob bytes.
- Дополненный порядок двух входов и replay после второго: только этот тест,
  **1 passed, 1.25 с**. Закрытие, stale CAS, changed request, cross-scope,
  текущая authority, quotas, history gap и closed input также покрыты.
- Первый запуск выявил ошибку фикстуры: extension без registry; исправлена.
  Mypy выявил слишком общий выведенный тип списка refs; уточнена аннотация.
- Ruff; strict mypy изменённых 5 source modules, Windows/Linux settings;
  export --check — чистые. Полный suite не запускался.

## Следующие части R1

1. R1 адаптирует опубликованный R2 `SessionContextPort`/`WorkContext`
   (`agent_experience/session.py`, фабрика `workplace_experience.session_context(rt)`)
   к SessionContextProvider, добавляя ordered SessionHistory. Вторую реализацию
   persona/ticket release не создаёт. До интеграции R2 нельзя принимать его ветку
   за ready dependency. R3 может готовить
   свой HTTP/UI adapter поверх SessionBook. Полный HTTP→Source intake и reply
   delivery/ACK ещё требуют wiring и проверок; сообщение не дублирует dialogue job.
2. Существующий `ModelGateway`/sandbox broker сейчас text-only. Реальный OpenCode
   с tools требует совместимого структурированного tool-call/result стыка через
   **тот же** LLMGateway/admission/usage. Не обходить это прямым provider ключом
   и не объявлять text-only mock запуском OpenCode. Проверить call_id каждого
   внутреннего запроса и фактическое соблюдение max_output_tokens.
3. Заменяемый session driver связывает ведение дела с jobs/WorkAttempt,
   актуальными правами/происхождением, отменой и limited checkpoint/resume.
   Старый ActivityStep не должен параллельно принимать смысловые решения
   для той же сессии. Character output остаётся точной доставляемой редакцией.
4. Пин версии/образа, настоящий harness, ephemeral home и trusted tool bridge;
   проверки STOP/revoke/context cleanup/budget. Нет установки OpenCode или
   скачивания образа в этой поставке. Полный AW-04/08/09 gate пока открыт.

Официальные первичные точки transport/tool-проверки (не доказательство runtime):
[server](https://opencode.ai/docs/server/),
[custom tools](https://opencode.ai/docs/custom-tools/),
[providers](https://opencode.ai/docs/providers/).
Существующий generic task runner/текстовый loopback endpoint сам по себе не
доказывает возможность удержать полноценную session/tool конфигурацию OpenCode.

Приватный host, реальные API, grants и Docker не менялись. Linux runtime,
браузер и настоящий OpenCode в этом checkpoint не проверялись. До полного
lifecycle/isolation gate и узкого взаимного review режим выключен.

## Передача shared-окон Claude R2/R3

По сообщению оператора о R2 `087a80b`: Claude получает короткое окно на одну
строку `m3_composition/service.py::access_ports()` для своей TicketRelease-обёртки
и её теста. Codex этот файл не правит до окончания окна; модуль/тест остаются R2.
Обычная самоприёмка и интеграция по doc27, без отдельного одобрения Codex.
После публикации этой session-поставки Claude R3 получает совместимые additions
в contracts/api.py, catalogue.py, panel extensions/exports, панельных
events.ts/validation.ts и dispatch `m3_composition/access.py`. Новый HTTP module
принадлежит R3. R1 сохраняет ports.py, agent_activity, activity.py и harness;
повторная генерация exports R1 в то же время не выполняется.
Session-команды сейчас имеют настоящий owner, но **не runtime dispatch**;
R3 делает его через owner и Source intake, не создаёт свой реестр сессий.
До интеграции его wiring старый сервер возвращает штатный отказ, UI-only
не объявляется работающим разговором. Character output/ACK и harness driver
остаются последующим стыком R1. Перед enable R2 обязателен также на старом
`activity.context()`: titles/replies не передаются модели в обход release.

## Следующая поставка: model admission/call identity/output ceiling

После публикации session-стыка исправлены prerequisites существующего шлюза.
Sandbox назначает каждому внутреннему вызову отдельный стабильный call_id
(`sandbox-1`, `sandbox-2` в рамках данной попытки); Execution уже связывает
его с operation/lease attempt. Два запроса больше не разделяют default `call`.
ExecutionModelGateway дополнительно сверяет exact scope/freshness/operation/
lease version, а не только предоставленный callback is_current.

ModelRequest.max_output_tokens теперь проходит в GenerationRequest и реально
ограничивает binding.parameters до отправки провайдеру. Используется минимум
конфигурации роли и запроса, также для fallback; увеличить потолок роли этим
аргументом нельзя. Настройка роли не меняется. Legacy callers без аргумента
сохраняют прежний путь; bool/zero/out-of-range limits отвергаются.

Один affected gate: прежние gateway tests + новые admission/ceiling + один
реальный Docker loopback broker — **53 passed (4.47 с)**. Существующий local
Linux image, сеть none, синтетический provider, никаких платных API.
Проверены два разных call_id, лимиты fallback/role, чужой scope/epoch/operation/
lease и прежний отказ лишнего вызова. Ruff и strict mypy 4 source modules —
чистые. Это не full shared-journal/reserve gate и не поддержка tool calls.
Docker engine Linux через Windows CLI доступен; OpenCode не установлен.
На момент проверки после `749ac9e` R2 ещё не в main: исправление старого
activity.context через R2 остаётся обязательным следующим стыком, не объявлено
закрытым. До него новый session runtime на личных данных не включается.

Дополнительно после изменения admission выполнен один прежний production-host
journey assign → ticket → operator answer → completion → restart STOPPED:
**1 passed (31.81 с)**. Для отдельного запуска явно добавлены только test-support
каталоги (`pytest -o pythonpath=…`), source imports — из собственного frozen venv.
Первый collection не нашёл dev_support; Windows absolute backslashes в override
также не подошли, использованы относительные tests/* пути. Полные suites не повторялись.

## R2 integrated: устранён прямой путь ticket → activity prompt

Сведён main `0475221` с принятой R2. `activity.context()` больше не берёт
raw title/body: использует TicketView из единого `session_context(rt)`.
Refused capability IDs остаются структурными control-данными, не свободным
текстом. Подготовка WorkAttempt теперь получает также точные ticket/material
refs показанного контекста. Прежнее silent `[-200:]` для provenance убрано:
overflow отказывает до вызова модели. Основания из предыдущих tool steps
сохраняются. `extra_sources` участвуют также в existing Execution.verify.

WorkContext.still_valid проверяется непосредственно до и после model call,
включая goal-путь. Поздний отзыв закрывает текст/решение, оплаченная technical
квитанция остаётся в общем journal. Политика release не дублируется в R1.

Добавлен `ReleasedSessionContext`, реализация агрегирующего R1 порта поверх
R2 SessionContextPort + SessionHistory + Source owner + existing ReleaseGuard.
Контекст transient, bounded по сообщениям/символам, с полными SourceRef/
ArtifactRef. Входы берутся по порядку: backlog не перескакивается ради newest.
through_sequence учитывает только реально показанный prefix; source/close/
generation/version/STOP проверяются до возврата. Checkpoint без повторной
проверки не загружается; физическое хранение checkpoint/harness ещё не подключено.

Affected bridge gate: sanitized title/replies, pre/post-model revoke, прежний
production-host assignment/ticket/answer/restart и own-goal/quiet/disabled —
**4 passed (55.07 с)**. Сборка с настоящими SessionBook/Source/ReleaseGuard,
bounded pending prefix и STOP при чтении — **2 passed (1.48 с)**. Первая
фикстура сборки использовала Source reader без API-transfer grant и получила
правильный отказ; исправлена на существующий reader с явным тестовым grant,
production права не менялись. Ruff и strict mypy — чистые.

После этого Claude опубликовал `96c1ae2` с однострочным ReleasedReader wiring
в своё выданное окно `service.py::access_ports()`. Оно принимается merge из main;
R1 не переписывает его и не создаёт второй ticket reader. Регрессионный
host journey выше выполнен до этой отдельной строки; её проверка — отчёт R2.

Следующий незакрытый объём: полноценный tool-call/result protocol существующего
LLMGateway → broker → OpenCode, настоящий harness внутри sandbox, bounded
session driver/checkpoint/cancel и delivery/ACK. При renew аренды драйвер
обновляет доверенный WorkAdmission для следующего RPC, а не принимает lease
от клиента. Ни один из этих gates не закрывается текстовым broker-тестом.

## Structured tools: существующий gateway и stdio broker

Добавлен provider-neutral протокол описаний инструментов, завершённых вызовов
и связанных результатов в истории. Схемы/аргументы bounded; повторяющиеся JSON
ключи, нечисловые значения, неизвестные имена, дубликаты id, разрывы индексов
и незаконченные аргументы не выпускаются как completed proposals. Fragment
не исполняется. После частичного tool output нет автоматического retry/fallback.
STOP удаляет proposal, техническая запись попытки остаётся. Старые текстовые
запросы сохраняют форму без tools. Это внутренние типы, wire-контракты R3
и generated exports не менялись.

OpenAI-compatible adapter передаёт tools и tool-result историю, собирает SSE
фрагменты через gateway. ExecutionModelGateway сохраняет scope/epoch/lease,
pre/post release, per-call ceiling и общий journal/budget. Размер запроса теперь
считает также schemas и tool arguments, включая existing live prompt ceiling.
Runner принимает закрытый bounded формат и отдаёт завершённый tool proposal
внутрь песочницы; запрос не может выбрать роль/credential/host-tool. Внешняя
сеть, mounts и права не расширены. Ответ broker тоже ограничен по байтам.

Проверки: llm + bound gateway — **114 passed**, один прежний тест получил
PROTOCOL из-за старой фикстуры без обязательного index. Фикстура исправлена,
повтор только этого теста — **1 passed**. Sandbox protocol/policy + два реальных
Docker loopback сценария — **17 passed (3.87 с)**: прежний текст/отказ/unknown
route и tools SSE → tool result → итоговый текст через настоящий LLMGateway
с синтетическим провайдером. Docker использовал уже имеющийся локальный image,
контейнеры удалены runner. Платных вызовов не было. Ruff и strict mypy по
изменённым модулям проверяются перед commit.

Граница: настоящий OpenCode ещё не запускался. Этот тест не доказывает его
совместимость, shared-budget harness gate или session delivery/ACK. Дальнейшие
поставки: driver, checkpoint, видимая отмена по release; Windows/OpenCode R5.
R3 `03fb572` и Linux R5 `5bf92bd` опубликованы Claude отдельно; его shared
окна освобождены. Личные runtime-настройки/данные не менялись.

## Видимость отменённых попыток и покрытие общего gate

Activity tick после сверки завершённых jobs пишет в существующий WorkTask.outcome
фиксированное техническое пояснение для cancelled/failed/unknown, а при исчерпании
числа попыток — явное отсутствие автоматического продолжения. Не утверждает точную
причину отмены, если JobOwner её не хранит. Закрытый текст источников не используется.
Это доверенная reconciliation вне отозванного execution fence, но под актуальным
ACTIVE/OPEN fence supervisor. Повтор идемпотентен; завершённая/отменённая оператором
задача не переписывается, STOP запрещает запись. Следующее успешное решение
убирает устаревшее техническое пояснение. Отдельный SQLite owner test — **1 passed
(1.51 с)**; первый запуск выявил только пропущенный limit в новой test fixture.

В scripts/check_combined.py добавлены tests/workplace_sessions,
tests/workplace_lifecycle и tests/workplace_sandbox (Docker остаётся opt-in).
Новый gate не был выдан за уже пройденный полный suite. Claude R5 уже добавил
свой headless journey независимо; shared UI/контракты этой правкой не затронуты.
После изменения reconciliation и очистки старого пояснения выполнен один прежний
production-host путь assignment → ticket → answer → completion → restart STOPPED:
**1 passed (34.21 с)**. Полные suites не повторялись. Ruff и strict mypy изменённой
композиции чистые.
