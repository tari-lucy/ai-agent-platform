# Необязательный OpenCode sandbox (R1 checkpoint)

Версия: [OpenCode v1.19.3](https://github.com/anomalyco/opencode/releases/tag/v1.19.3),
Linux x64 baseline. Это отдельный image для существующего DockerTaskRunner,
не новая команда START и не замена host. Без явного wiring он не используется.

Из корня checkout в PowerShell:

```powershell
./scripts/build_opencode_sandbox.ps1
```

На Linux/macOS тот же архив и контекст: `python scripts/build_opencode_sandbox.py
--pull-base` (без `--pull-base` базовый образ должен уже быть локально).

Скрипт явно скачивает exact release archive (~61 MB), проверяет SHA-256,
состав архива, готовит отдельный runtime/opencode-build/<id> с точным контекстом.
Можно передать `-ArchivePath <уже скачанный архив>`, чтобы не скачивать повторно,
и `-Tag <локальный tag>`. Dockerfile дополнительно проверяет checksum binary.
Python base закреплён digest; сборка может получить его из registry, если он
не доступен локально. RUN не имеет сети. Установщика на Windows нет.

В RunnerTemplate использовать **image ID**, а не изменяемый tag, и prefix:
`/usr/local/bin/python -I /opt/agent-harness/worker.py`.
Роль, max_model_calls, max_prompt_chars, max_model_tokens и RunnerLimits
задаёт доверенная композиция. Параметры не выбираются текстом инструкции.

Пример проверенного тестового потолка: 512 MB RAM, 128 pids, 15 s CPU,
30 s wall, 32 MB tmpfs, 64 KB выхода, два вызова модели, 256 output tokens.
Это тестовые параметры, не изменение профиля/бюджета личного host.

Для режима с workspace MCP **и helper MCP** 512 MiB оказалось недостаточно
(Docker подтвердил OOMKilled). Проверенный потолок OpenCode в этом режиме —
1024 MiB, отдельного Python helper — 128 MiB; учитывать оба плюс Docker/WSL.
Подробности, разрешения и оставшиеся gates:
[helper execution](../../docs/implementation-reports/R1-helper-execution.md).
`max_helper_calls=0` оставляет MCP и запуск helper выключенными.

Проверка с синтетическим провайдером:

```powershell
$env:AW_OPENCODE_IMAGE_ID = docker image inspect --format '{{.Id}}' ai-agent-platform:opencode-1.19.3
$env:PYTHONPATH = 'tests/workplace_sandbox'
python -m pytest tests/workplace_harness -q
```

Полный combined gate выполняется только по необходимости. Для короткого
локального запуска достаточно tests/workplace_harness, добавив test-support
tests/workplace_sandbox в PYTHONPATH (source imports — из venv этого checkout).
Без AW_OPENCODE_IMAGE_ID реальный Docker/OpenCode test пропускается явно.

Изоляция: сеть none, нет mounts/secret/docker socket/опубликованных портов,
read-only root, non-root, tmpfs. Только контейнерный loopback facade → stdio
→ существующий ModelGateway. Отключены project config, title/summary/compaction,
models fetch, обновления, plugins, внешние MCP/LSP/formatter. Config-каталог read-only:
в exact release штатный Npm.install пропускает unwritable каталог.

OpenCode может читать staged inputs и создавать/редактировать outputs. Bash,
сеть, подагенты и внешние каталоги запрещены. Outputs — только staging,
их сохранение требует существующего владельца файлов с актуальными refs/правами.
Ошибки CLI/инструментов не выдаются как подтверждённый результат.

Каждый turn удаляет контейнер и его DB/history/logs. Логическая сессия
продолжается по заново разрешённому контексту владельцев; сырой export/import
OpenCode в долгую память не сохраняется. Cache staged outputs очищается close
или runner lifecycle. Счётчики OpenCode не являются учётом бюджета — используется
host journal, а неизвестный usage не превращается в 0.

Session driver/delivery/ACK и запись результатов через ManagedFiles проверены
на production composition с синтетическим провайдером. Для owner bridge
доверенный RunnerTemplate явно задаёт `max_workspace_calls > 0` (default 0).
Тогда доступен единственный локальный MCP `platform_workspace`: capabilities,
list_files, read_file, write_note, raise_ticket и read_ticket. Он обращается к
контейнерному loopback, затем по существующему stdio-транспорту к владельцам.
Ни scope, ни admission/principal, ни права контейнер не передаёт. Новый
инструмент запрашивается тикетом; ответ оператора не выдаёт Permission.

Для `test_workspace_host.py` и `test_workspace_transport.py` требуется
`AW_WORKSPACE_IMAGE_ID` с проверенным полным image digest. Это отдельный
opt-in от `AW_OPENCODE_IMAGE_ID`. Тестовые host/данные/провайдер синтетические,
OpenCode и Docker настоящие. Включение на личном host отсутствует.

Штатное подключение к host — `enable-work-sessions` и обычный `serve`:
[launcher](../../docs/implementation-reports/opencode-launcher.md).

Python helpers подключаются через `enable-work-sessions --helper`, после
отдельного решения оператора. Старому образу требуется обновление доверенного
harness: `scripts/refresh_opencode_harness.py` использует существующий pinned
образ без установки зависимостей. Инструкция и ограничения:
[Python-песочница](../../docs/opencode-python-sandbox.md).

Постоянная рабочая папка личности (`/workspace`, W2) включается
`enable-work-sessions --home` с образом, несущим метку
`ai-agent-platform.workspace.protocol=1` (тот же `refresh_opencode_harness.py`):
[инструкция](../../docs/agent-workspace.md).
