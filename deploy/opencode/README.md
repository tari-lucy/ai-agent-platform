# Необязательный OpenCode sandbox (R1 checkpoint)

Версия: [OpenCode v1.19.3](https://github.com/anomalyco/opencode/releases/tag/v1.19.3),
Linux x64 baseline. Это отдельный image для существующего DockerTaskRunner,
не новая команда START и не замена host. Без явного wiring он не используется.

Из корня checkout в PowerShell:

```powershell
./scripts/build_opencode_sandbox.ps1
```

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
models fetch, обновления, plugins/MCP/LSP/formatter. Config-каталог read-only:
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

**Ещё не готово:** production session driver/delivery/ACK, platform tools bridge,
safe staged restore и full runtime integration. Эти ограничения нельзя считать
закрытыми успешным isolated harness тестом.
