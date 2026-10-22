# R1/R4: управляемые Python helpers

Дата: 2026-10-22. База реализации: main `97df10d`.
Исполнитель: Codex, изолированный checkout `codex/r1-agent-session`.
Этап ограничен helper execution; полный R1/R4/R5 не закрывается.

## Что поставлено

В OpenCode добавлен отдельный локальный MCP `helpers_scripts`. Его операции:

- `save_script(file_id, body, expected_version?)` — сохранить/обновить ManagedFile
  вида script в выбранной доверенной композицией области. Создание файла само
  по себе не запускает его; версия меняется через CAS владельца файлов.
- `run_script(file_id, version, inputs?, argv?)` — исполнить точную версию.
  Аргументы MCP — строки; inputs — JSON-массив `{id, version}`, argv — JSON-массив
  строк. До 8 дополнительных файлов и 16 аргументов. Версии/ids — ссылки
  каталога, не пути компьютера. Script — `inputs/input-0000`, остальные файлы
  начинаются с `inputs/input-0001`; manifest содержит соответствие ссылок.

Исполнение проходит отдельным DockerTaskRunner через fixed prefix
`/usr/local/bin/python -I /work/inputs/input-0000`. Образ, команда интерпретатора,
область результатов и RunnerLimits выбираются доверенной конфигурацией.
Частичный JSON, тело навыка и переданный argv не выбирают host command/image.
Создание shell/python-процесса на Windows не является агентским инструментом.

Не менялись переданные Claude файлы `work_session_workspace.py`,
`sandbox/model_protocol.py`, `agent_harness/workspace_mcp.py` и его skills-зона.
Helper имеет отдельный закрытый RPC kind и MCP-файл; старый workspace RPC
сохраняет прежние операции и лимит. Общие owner interfaces не дублируются.

## Права, источники и результаты

Для запуска нужны оба действующих допуска: рабочая сессия и отдельная
зарегистрированная helper capability с ready binding/automatic Permission.
Проверяется истечение/отзыв, разрешённые области входов и результатов; heartbeat
повторяет проверку. Наличие скрипта/навыка или согласие текстом не выдаёт прав.
Сохранение script остаётся обычной файловой операцией с правом сессии.

Runner-template helper использует id capability текущей WorkAttempt для
существующей файловой/runner привязки. Дополнительное helper-право проверяется
отдельно trusted callback, а его capability_id задаёт операторская композиция.
Это не child job с выдуманным разрешением и не замена capability у попытки.
Helper-capability доступна как executor только сессионному пути, не старому
ActivityStep, у которого нет такого адаптера.

Тексты/версии файлов читаются через ManagedFiles. Полные SourceRef и ArtifactRef
добавляются к WorkAttempt до исполнения/передачи результата. ReleaseGuard
проверяет api_transfer и ledger до/после IO. Файлы outputs и bounded stdout
сохраняются через существующий `save_outputs` и владельца файлов; пути включают
attempt/call, прежние результаты не перезаписываются. Ответ MCP содержит только
статус и ссылки. Текст можно читать через файловый owner, пока основания открыты.
Добавлены media types для txt/json/py, чтобы диагностика читалась этим же путём.

`WorkAttempt.helper_runs` — bounded owner receipts, не второй реестр jobs:
перед стартом сохраняется prepared с fingerprint аргументов, после сохранения
результатов — терминальный статус и refs. Повтор завершённого call_id возвращает
тот же receipt с повторной проверкой release; другое содержимое конфликтует.
Prepared/unknown не исполняется повторно; смена call_id внутри этой попытки
также не обходит неизвестный исход. Записей до 16, старые не выбрасываются.
Все новые поля совместимы со старыми JSON-записями (пустой default).

## Изоляция, лимиты и остановка

Helper — одноразовый **отдельный** контейнер: network none, read-only root,
UID 65532, no-new-privileges/cap-drop, bounded tmpfs/pids/CPU/RAM/wall/output.
Нет mounts, Docker socket, host ports, секретов, model/owner gateway или ключа.
Даже зная адрес loopback OpenCode, скрипт находится в другой network namespace.
У helper нет модельного клиента и возможности сделать неучтённый API-вызов.

На ход не больше `RunnerTemplate.max_helper_calls` (default 0, максимум 16).
Это отдельный конечный лимит owner RPC, включающий save_script; общий бюджет
LLM остаётся в Gateway/журнале activity. Одновременно один OpenCode + один helper.
Оба контейнера регистрируются ResourceTicket одного действующего admission;
STOP отзывает их допуск. Удаление признаётся только по Docker receipt, неизвестная
очистка закрывает runtime и оставляет handles для доверенной сверки.

Выявлен измерением **OOMKilled при 512 MiB** для OpenCode с двумя MCP.
В проверенном helper smoke установлен явный потолок OpenCode **1024 MiB**,
helper **128 MiB**: суммарный потолок контейнеров 1152 MiB плюс расходы Docker/WSL.
Это потолок, не измеренный общий peak платформы; launcher должен учитывать весь
бюджет doc20. CPU/wall/число вызовов и права при исправлении не ослаблялись.
Старый пример 512 MiB для меньшего OpenCode-сценария не доказывает этот режим.

## Проверки

Последний целевой прогон: **10 passed (138.77 с)** — `test_helper_host.py` и
`test_helper_protocol.py`. Перед ним прошли остальные affected owner/transport/
outputs/adapter/policy проверки; после исправления гонки и сокращения сценария
повторялись только затронутые. Единый итоговый полный suite не запускался.
Ruff, strict mypy (**286 файлов**), import boundaries, generated exports check
и типы панели чистые. Все данные вымышленные, HTTP provider scripted.
Настоящий OpenCode 1.19.3 и Docker Desktop/Linux-контейнеры на Windows.

Путь: save_script → отказ run_script без отдельного права → финальный ответ/ACK;
trusted fixture выдаёт helper Permission → новая реплика → Python helper →
сохранённый результат/receipt → ответ/ACK → released_content; следующий helper → STOP при
работающем контейнере → удаление двух контейнеров → restart STOPPED.
Шесть модельных запросов относятся к activity, платного провайдера нет.
Fixture grant не выдаётся агентским кодом и не заменяет product preview/execute.

Внутри script проверяются non-root/read-only root, отсутствие host/socket/
gateway credentials и отказ исходящего соединения. Отдельно проверены
конечный RPC budget, закрытая схема, owner replay/conflict/unknown/STOP/revoke,
lineage результатов и старые adapter/output/policy сценарии. Ошибка JSON-RPC
сохраняет request id, чтобы клиент не ждал ответа до таймаута.

Найдена также гонка intake: между выбором и исполнением приходило новое
сообщение. TaskInput теперь pin-ит sequence, selector повторно сверяет сессию,
а устаревший ход отменяется STALE_CONTEXT до контекста/модели. Старые команды
без необязательного поля читаются совместимо. Отдельный regression доказывает
раннюю отмену; разрешения ради устаревшего trigger не расширялись.

Первые прогоны выявили ошибки тестовой сборки запросов/SSE-заголовка и OOM.
Ошибки fixture исправлены; причина закрытия attach установлена через
технический Docker State, а не снятие guards. Расширенный сценарий с отдельным
модельным read_file ранее прошёл (1 passed, 128.64 с), но на повторах не всегда
укладывался в 55 s wall. В сдаваемой проверке лишний модельный круг заменён
прямым production `released_content`; таймаут не увеличен. Размер более
длинного рабочего turn требует отдельной настройки и измерения в launcher.

**Открытый STOP gate:** в последнем Windows-прогоне оба контейнера удалились,
позднего ответа/нового model call нет, restart STOPPED. Однако первый run остался
FAULTED, Job — OUTCOME_UNKNOWN, в unconfirmed остался `job-*`, не контейнер.
Поэтому graceful STOP/reconciliation **не объявляются закрытыми**. Подготовленный
helper receipt остаётся для безопасной сверки, автоматического replay нет.
Это следующий конкретный R4 стык; личный host до его закрытия не включать.

Финальный local image:
`sha256:bb47f421ff18302c38a075c9e21d972a510191103fda477198a71f317adda1f8`.
Архив/бинарник OpenCode и базовый образ — прежние pinned digests из
`deploy/opencode/README.md`. Новых загрузок/зависимостей нет.

## Что остаётся

Private host/профиль/.env не читались и не менялись, mode не включался,
новые права пользователю не выдавались. Это opt-in factory, не production
launcher и не готовое развертывание. Windows browser и реальная модель здесь
не проверялись; качество кода/ответов synthetic provider не оценивается.
Автоматическое восстановление незавершённых helper receipts, staged restore,
полный task-goal path и итоговый общий lifecycle gate остаются отдельными задачами.
Создание SkillVersion через MCP делает Claude в своей зоне.
