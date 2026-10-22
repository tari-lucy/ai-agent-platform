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

## Настоящий OpenCode в изолированном adapter gate

Выбран exact pin **1.19.3, Linux x64 baseline**. Official archive SHA-256:
`531cff2441129c30f0edd5878af494108ad07f5feb46d7ee29a2a5b830a5765d`;
binary SHA-256: `179ca421d61eefeadfa0c50e084a6e79c1623734732f0f4593f81ccfd3ab7ee9`.
Проверены official CLI/server docs и exact source config/paths/npm/read/flags.
Добавлены воспроизводимые scripts/build_opencode_sandbox.ps1, deploy/opencode,
стандартный AgentHarness adapter и standalone image launcher. Binary не в Git;
никакого запуска OpenCode или установщика на Windows-хосте.

Первый --version без выделенного HOME получил правильный EROFS. С временными
HOME/XDG в контейнере — 1.19.3. Первый model handshake выявил недостающие
стандартные stream_options.include_usage, tool_choice=auto в facade; они добавлены
с закрытой проверкой значений. Другие provider options не принимаются.
Прямая read-проба выявила relative-worktree permission paths; доступ ограничен
только inputs/outputs внутри sandbox. Выполнен фактический read manifest →
tool result → final через существующий LLMGateway и scripted provider.

Новый affected gate tests/workplace_harness: **4 passed (6.47 с)** на Windows
Docker Desktop/Linux engine. Из них один настоящий OpenCode, ровно два model
calls, оба под lowered ceiling 256, два technical reports с synthetic provider
usage. Нет title/summary вызовов. Сырые ошибки и partial text не становятся
ответом. Контейнер удалён; close очищает staged cache. Ruff, strict mypy
четырёх source modules и import boundaries — чистые. Личный host и платные
API не использовались. В check_combined добавлен новый набор; full suite не
повторялся. Исходный synthetic probe в runtime/ не включён в Git.

В exact source Npm.install проверяет writable перед любым install. Поэтому
конфигурационный каталог image read-only; project config отключён, нет plugin/
MCP/LSP/formatter, models-fetch/updates/compaction отключены. Контейнер network
none, без mounts/secret/socket. Model token ceiling берётся из trusted template,
не зашит в персонажа. Отдельная задача получения полного transitive notices
upstream binary остаётся до распространяемой поставки (не заявлен release SBOM).

Граница остаётся: это настоящий isolated OpenCode adapter, но ещё не единый
production session driver. Он не подключён автоматически к private host;
platform tools/тикеты/файлы через owner bridge, logical-session responses,
delivery/ACK и staged restore остаются обязательными следующими стыками.
Сырой OpenCode checkpoint не сохраняется: следующая среда должна получать
контекст заново через R2/Source/File owners. Такая реконструкция безопаснее
непроверенного export/import, но не объявляет полный resume gate пройденным.

## SessionBook: внутренний delivery hook

Добавлен record_delivered для доверенной композиции, без новой HTTP-команды
или права агенту публиковать свой ответ. Обязателен отдельный verify_delivery
от владельца доставок; без него CAPABILITY_UNAVAILABLE. Плюс точная проверка
Source/text, generation, доступного prefix и полного набора его оснований.
Ни один исполнитель не может передать boolean «доставлено» вместо этой проверки.

В одной owner transaction добавляется только SourceRef с speaker=character и
reply_to_sequence и продвигается processed_sequence. Новый ввод, поступивший
во время генерации, не теряется и не помечается обработанным. Собственная
добавленная реплика сразу считается обработанной только когда нового ввода нет.
Replay не создаёт второй ответ; raw text не дублируется в message или fingerprint.
После отказа Source должен снять вызывающий memory owner (как в R3 orphan fix).

Affected gate: новые hook cases + прежние session owner tests — **6 passed
(2.87 с)** на настоящем SQLite. Delivery callback в этом наборе синтетический:
это не доказательство существующей UI ACK-интеграции. Проверены отсутствие
callback/подтверждения, неподоказанные основания, чужая generation, пропуск
входов, source/text mismatch, отозванный допуск, close, replay и новый ввод
параллельно ответу. Ruff и strict mypy модуля чистые. Runtime wiring,
очистка orphan ответа и UI delivery/ACK остаются следующей поставкой.
После совместимых добавлений внутренних ports выполнен общий strict mypy:
**277 source files, clean**. Полный pytest gate не запускался повторно.

## 2026-10-22: публичное чтение ManagedFiles

По запросу R3 добавлен `ManagedFiles.released_content(scope, file_id, purpose)`:
текущая версия, release guard до/после bounded blob IO, проверка размера,
проверка неизменности версии перед возвратом. Авторизацию оператора сохраняет
HTTP boundary. Право api_transfer не выводится из private-read. Метод не выдаёт
WorkAdmission и не расширяет права. `workplace.file_content` переведён на него;
дублирование blob/guard-последовательности из композиции убрано.

Новые owner-проверки: **3 passed** (допустимое чтение, запрет API-передачи,
отзыв через ledger во время IO). В библиотечном host-тесте Windows проверяет
настоящий закрытый DACL через `windows_private`, Linux сохраняет проверку
POSIX-битов. Права старых Source не переписывались; изменения production-матрицы
R3 для новых записей сохранены при совмещении с main.

## 2026-10-22: session → job → draft → ACK → опыт

Внутренний session driver использует существующую activity job, её допуск,
ExecutionModelGateway и общий usage/budget. WorkAttempt имеет ровно один
предмет: task или session; фиктивная задача для разговора не создаётся.
Действующий manifest/binding/automatic Permission обязателен; проверяется
повторно перед/после обращения к модели. Неизвестный исход не повторяется
вслепую. Lease обновляется только из доверенного Execution.

Контекст заново собирается через R2 и владельцев Source/File. Входы попытки
сохраняют точные refs. Ответ до ACK живёт как private Utterance и QUEUED
Delivery; WorkSession содержит только ссылку pending_reply. Source ответа,
speaker=character, продвижение processed_sequence и эпизод проверенного опыта
появляются после получения точной редакции оператором. При отказе привязки
созданный Source снимается; повтор ACK не дублирует историю и эпизод.

Панель показывает draft отдельно от истории и автоматически подтверждает
его только в видимой вкладке, при видимости минимум 60% записи, активном run
и серверном acknowledgeable. Одна автоматическая попытка, затем ручной повтор
тем же request_id. Новый ввод отменяет pending pointer; STOP/restart закрывают
применение старого ответа. Старый ACK не подтверждает новый ввод.

При удалении основания lifecycle включает технический Utterance, в том числе
через session → attempt → result. Проверка настоящих RetentionService,
SQLitePhysicalStore и WorkplacePurger доказывает удаление marker из файла БД,
а не только скрытие в API. Не вводится второй ledger.

Проверки на Windows, синтетические данные и транспорт без внешней сети:
сквозной runtime/ACK (повтор, неверная редакция, новый ввод, STOP, restart) и
физическое удаление — **2 passed**. Контрактные тесты панели разговора —
**6 passed**, lint/typecheck/build чистые. Ruff, strict mypy (**278 source
files**), import boundaries и проверка generated exports чистые. Общий pytest
всего проекта повторно не запускался. Один затронутый прогон runtime сессии,
workplace_activity, workplace_conversation, library_host и brief_host —
**11 passed (297.15 с)**. Windows init/ACL делают эти host-тесты заметно
дороже owner-тестов. После исправлений не повторялись уже зелёные независимые
наборы. Ошибка ruff из main устранена самостоятельным коммитом bf84479.

Это совместимый внутренний стык, **не завершение R1/R5**. Factory harness
должна быть явно подключена доверенной композицией; production host не
изменён и автоматически OpenCode не включает. Настоящий OpenCode ранее
проверен изолированно, а этот combined session-тест использует синтетический
harness поверх настоящего model gateway. Следующее: owner bridge инструментов,
подключение OpenCode factory/launcher, browser ACK journey и staged restore.
Файловые outputs harness пока отклоняются: они не объявляются сохранёнными
без отдельной записи через ManagedFiles. Платных API-вызовов не было.

## 2026-10-22: настоящий OpenCode в production session composition

Предыдущая граница staged outputs закрыта для опубликованных файлов:
`OpenCodeSessions(runtime, trusted_template, output_area=...).install()`
подключает настоящий `OpenCodeHarness`/`DockerTaskRunner` к session factory.
Шаблон требует точный executable prefix и роль activity. Image digest, CPU/RAM/
PID/wall/output/disk ceilings, число модельных вызовов и область результатов
поступают из доверенной композиции, не из текста персонажа или ответа модели.
Install не создаёт manifest/binding/Permission, не запускает START и не меняет
host.json. Без уже действующих прав выполнение закрыто. Сессионная среда не
показывается старому task-step как вложенный инструмент второй личности.

Контейнер регистрируется настоящим ResourceTicket supervisor. Только
подтверждённое удаление Docker даёт acknowledge, в том числе после STOP.
Неизвестная очистка сохраняет handle/resource и закрывает выполнение через
FAULTED. Это live handles существующего owner, не второй реестр jobs.
Частый runner fence проверяет epoch/lease/cancel и актуальные права/основания;
полная jobs proof проверяется на create/model/file boundaries. Дублирующий
полный proof в heartbeat создавал конкуренцию с тем же модельным вызовом и
односекундный timeout runner; устранено дублирование, лимиты не увеличены.

`AgentHarness.staged` отдаёт проверенные временные bytes. Driver сохраняет их
через `ManagedFiles.write`: реальные checksum/size, quota, актуальные права
области, неизымаемые inputs/input_files и provenance=generated. Публикация
каждого файла имеет обычную owner-квитанцию. Namespace попытки отделяет
одинаковые имена в разных ходах; файлы прошлой попытки не перезаписываются.
При нескольких outputs запись не является общей транзакцией: если следующий
файл отклонён, уже записанные файлы остаются самостоятельными результатами,
но успешного draft/доставки/эпизода этого хода нет. `result_files` попытки и
контекст draft несут точные сохранённые ArtifactRef; release закрывает ответ,
если файл или его основание уже недоступны.

Проверено на Windows/Docker Desktop, Python 3.14.8, OpenCode 1.19.3:

- Production-built host с настоящими owners, jobs, API, live-адаптером над
  синтетическим HTTP transport и настоящим OpenCode: STOPPED → сообщение →
  START → чтение staged manifest → write в tmpfs → запись ManagedFiles →
  pending draft → ACK → character/history/эпизод. **3 модельных вызова**.
- Следующий настоящий контейнер: STOP во время четвёртого модельного вызова,
  подтверждённое удаление, отсутствие позднего ответа/Source, завершение job
  cancellation receipt → STOPPED. После restart остаётся STOPPED, новых
  модельных вызовов нет. Статус STOPPING до job receipt тест не подменяет
  фактом одного удаления контейнера.
- Новый combined journey и прежний runtime/ACK вместе: **2 passed (102.34 с)**.
  Использован существующий image
  `sha256:29afdbd56f89006dc8c1d380355b72d35a1c8f088e4784066e32ed3c4e4f2477`.
  Worker/upstream binary не менялись и заново не скачивались.
- Быстрый affected-набор: outputs и прежний OpenCode adapter — **8 passed
  (1.73 с)**. Проверены неверный digest, путь за пределами namespace, отзыв
  ledger во время получения bytes, STOP и одинаковые имена двух попыток.
- Ruff, strict mypy (**280 source files**), import boundaries и generated
  exports чистые. UI не менялся; его build и полный pytest повторно не гонялись.

Для Linux/browser R5 опубликован общий тестовый helper
`tests/workplace_sessions/session_support.py` (`install`, `ack`), main b31d356.
Старые импорты через test_runtime сохранены. Claude делает Linux browser ACK
journey и продолжение Linux journey; Windows browser с настоящим OpenCode
остаётся частью R5 Codex. Новая Docker-composition на Linux в этом прогоне
не проверялась. Scripted модель доказывает маршрут, не качество ответов.

R1/R4/R5 целиком **не закрыты**. Отдельными следующими задачами остаются bridge
платформенных инструментов/тикетов в OpenCode, возобновление содержательной
работы с результатами и task goals, durable recovery/reconciliation после
падения процесса, staged restore, production launcher и browser journey.
Закрытый lifecycle/isolation gate пока не разрешает enable на личном host.
Приватный host/профиль/диалоги и .env не читались и не менялись, новых прав
ему не выдавалось, настоящих API-вызовов не было.

## 2026-10-22: owner bridge в настоящем OpenCode

Завершён следующий ограниченный этап: встроенный локальный MCP
`platform_workspace` → контейнерный loopback → существующий stdio runner →
владельцы платформы. Схема local MCP сверена по
[официальной документации](https://docs.opencode.ai/docs/mcp-servers/),
совместимость проверена на pinned OpenCode 1.19.3, без SDK/новых зависимостей.
Внешние MCP, shell, сеть, host mounts, секреты и Docker socket не включаются.

В bridge есть discovery capabilities и чтение списка/текста файлов по opaque
id, запись note/hypothesis через KnowledgeBase/ManagedFiles, problem/question/
idea и capability_request через TicketService, чтение известного ticket_id
через TicketRelease. История/источники/материалы тикета не обходят release.
Права файлов ограничены областью действующей Permission capability этой
сессии. Нет raw SQL, доступа к host-пути или произвольного Python/HTTP tool.
Текст навыка и request capability_id не расширяют полномочия.

`max_workspace_calls` доверенного RunnerTemplate — конечный предел owner-
вызовов на turn (0 по умолчанию, максимум 64); исчерпание даёт RESOURCE_BUSY
без нового owner-вызова. Независимо от него все LLM calls используют activity
Gateway/journal и общий дневной бюджет. MCP не получает scope, principal,
WorkAdmission или модельный endpoint/key: это серверная привязка попытки.
Кадры/аргументы и ответы ограничены; исправлена граница escaped JSON для
обычного кириллического ответа, без снятия ограничения на bytes.

Новые прочитанные SourceRef/ArtifactRef добавляются владельцем WorkAttempt
**до выдачи текста**. Старые inputs не удаляются; разные версии одного Source
не подменяются. Перед последующими модельными вызовами и публикацией draft
driver проверяет уже полный owner-набор. Записи заметок получают все inputs,
а их ArtifactRef идут в result_files и основания draft/тикета. Scope/права/TTL/
ledger проверяются до и после IO. Повтор write с тем же call_id использует
owner receipt; изменённое содержимое под тем же id не становится второй записью.

Финальный affected gate на Windows/Docker Desktop: **31 passed (74.20 с)**,
без полного suite проекта. В нём:

- настоящий OpenCode + production host/API/owners + синтетический HTTP
  provider: write_note → read_file → capability_request → финальный ответ →
  ACK/history. **4** LLM-вызова в activity; Permission до/после побайтово по
  значениям моделей одинаковы. Прочитанный файл присутствует в inputs,
  result_files, origin draft и материалах тикета;
- настоящий Docker transport: предел 2 owner-вызова, третий отказан, новых
  model calls нет, удаление контейнера подтверждено;
- настоящие SQLite/blob/ledger owner-проверки: расширение lineage перед
  возвратом текста, последующая запись с полными grounds, replay/conflict,
  отзыв, STOP, чужой admission и запрещённый scope argument;
- закрытая схема RPC, кириллический ответ, прежние adapter/policy/model
  protocol регрессии. Ruff, strict mypy (**282 source files**), import
  boundaries и generated exports чистые. UI не менялся и не пересобирался.

Финальный image: `sha256:ccbffec52935bc931fa2f5c870d04f5d92b309c14852fe62bb450dc95d556bd3`.
Архив OpenCode использован из проверенного локального cache; бинарник и
базовый image pinned, RUN-сборка без сети. Никаких платных API-вызовов,
переноса/включения личного host или выдачи ему новых прав не было.

Полный R1/R4/R5 остаётся открыт. В этом этапе ещё нет исполнения helpers и
создания SkillVersion через MCP, переноса task goals в OpenCode, автоматического
обнаружения/пробуждения по ответам тикетов и durable recovery/staged restore.
read_ticket требует известного id; наличие owner bridge не объявляется
готовым автономным ticket-resume. Windows browser и production launcher также
остаются следующими gates. После сдачи этого этапа работа остановлена по
договорённости с оператором, следующий этап — только после «продолжай».

## 2026-10-22: продолжение сессии по ответу на связанный тикет

Следующий ограниченный этап R1: созданные/прочитанные через owner bridge
тикеты связываются с попыткой, затем с логической сессией при публикации
draft. Сохраняются только ticket_id и версия, тексты остаются у TicketService.
Нового реестра пробуждений или фонового model-клиента нет. Поля совместимы
со старыми записями (пустые defaults), contracts exports перегенерированы.

Существующий activity tick находит новый ответ оператора на связанный тикет,
при действующем допуске и бюджете продолжает ту же сессию через JobOwner и
настроенный harness. Не требуется фиктивная реплика оператора в рабочем
разговоре. TicketRelease проверяет основания; отвеченные тикеты ставятся
первыми в ограниченном контексте R2. Изменение ответа во время исполнения
отменяет устаревший turn, источники/файлы по-прежнему проверяются перед IO.

Ревизии рассмотренных тикетов фиксируются **при точном ACK**, вместе с
сохранением ответа личности и прогресса сессии. До ACK второй model-turn не
создаётся. Собственный ответ персонажа и метаданные «прочитано» не являются
новым входом. STOP/restart не запускают продолжение; закрытая сессия не
пробуждается. Ответ на capability_request — информация, Permission/binding
по-прежнему нужны отдельно; текст согласия доступа не выдаёт.

Ресурсные границы: до 20 связей на сессию/попытку, за turn до 10 отвеченных
тикетов в пределах текущего R2 context. Лишние ответы не отмечаются
рассмотренными; их очередь сохраняется. При заполнении связей новый tool-
запрос отказывает RESOURCE_BUSY до создания обращения. Чтобы завести новое
дело сверх этого лимита, нужна другая сессия; автоматического удаления связей
нет. Просмотр истории использует существующий ограниченный каталог сообщений
TicketService, отдельная оптимизация его пагинации сюда не добавлялась.

Финальный affected gate на Windows: **16 passed (64.50 с)** —
`test_ticket_resume`, `test_ticket_wake`, `test_sessions`, `test_delivery_hook`,
`test_workspace`, `test_context_assembly`, `test_released_context` в
`tests/workplace_sessions/`. Настоящие production init/build/owners/API,
синтетический harness и HTTP provider, ровно **2** activity model calls:
создание запроса → ACK → STOP → ответ оператора → restart STOPPED → START →
продолжение без новой chat-реплики → ACK/replay → закрытие → поздний ответ
без третьего вызова. Permission для запрошенной capability не появился.
Отдельно проверены метаданные «прочитано» и закрытый release оснований.
Ruff, strict mypy (**283 source files**), import boundaries, generated export
check и панельный `tsc --noEmit` чистые. Полный suite, реальный OpenCode и
браузер заново не запускались; Docker/worker/MCP image в этом этапе не менялись.

Первый синтетический integration fixture был неполон (files/ledger), исправлен;
ещё одно ожидание теста исправлено под существующий отказ ticket.read для уже
прочитанного операторского ответа. Production-проверки ради теста не ослаблены.
Два первых sandbox-прогона зависли и остановлены по проверенным PID; результат
принимается только из завершённого прогона вне sandbox на временных данных.

Данный этап не решает recovery незавершённого owner-tool вызова: до публикации
draft связь есть у WorkAttempt; неизвестный исход job не повторяется вслепую.
Пробуждение именно по последующей выдаче Permission без нового ответа, helpers,
skills, перенос task goals в OpenCode, staged restore/launcher и Windows browser
остаются отдельными gates. Частный host не менялся, новых прав ему не выдавалось,
реальных/платных API-вызовов нет. Полный R1/R4/R5 этим этапом не закрывается.

## 2026-10-22: Python helper execution

Следующий ограниченный этап и его границы:
[R1-helper-execution](R1-helper-execution.md). Отдельный MCP и networkless
контейнер для сохранённых script-файлов, дополнительное helper Permission,
owner receipts и lineage. Skills-зона и три переданных Claude shared-файла
не меняются. Production enable и полный R1/R4/R5 этим не закрываются.
Первоначальный smoke/protocol: 10 passed, но STOP gate оставался открытым.
Следующий ограниченный этап установил гонку shutdown при записи job receipt
и защитил техническую запись после завершения исполнителя; подробные проверки
и границы — в том же R1-helper-execution.md. Закрытие только Docker-контейнеров
не считается завершением Job: smoke теперь ждёт его отдельную квитанцию.

Сообщение Claude по skills: при отзыве основания сообщения разговор может
оставаться open без нового хода и без видимой причины. Это отдельный открытый
пункт session lifecycle/UI; текущая правка STOP его не решает. Skills MCP и
Linux ticket-resume интегрированы Claude, Windows/OpenCode проверка навыков
и staged restore остаются отдельными gates. Личный host не включался.

## 2026-10-22: staged restore рабочего места

Отдельный этап R4: [поставка и инструкция для Claude/R5](R4-staged-restore.md).
В существующий backup подключены snapshot blob bundle, ledger/purger в staging,
owner reset сессионного контекста, закрытое переключение и finish-restore.
Итоговый affected gate на Windows — 63 passed; Docker и реальная модель в этом
этапе не запускались. Это не production launcher и не перенос личного host.
