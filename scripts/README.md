# Проверки каркаса M0-02

Python — один uv-проект, шесть import packages по ролям из doc23. Core/worker
entrypoints только проверяют установленный каркас и завершаются; они не слушают
порты, не запускают агента и не открывают DB/Redis/модели. UI — статическая
заглушка без API клиента/сессии. Экраны/auth/контракты принадлежат M1/M4.

## Windows: чистая установка

В собственном checkout с Node 24.19.0, npm 11.17.0 и uv 0.12.23:

```powershell
$env:UV_PYTHON_INSTALL_DIR = Join-Path $PWD 'runtime/toolchains'
uv python install 3.14.8 --no-bin --no-registry
uv sync --locked --all-extras --python 3.14.8
npm.cmd ci --prefix apps/panel
uv run --locked --no-sync python scripts/check.py
uv build --wheel
```

Локальный .venv/node_modules/runtime не коммитятся. `recall` extra фиксирует
пакеты для M0-03, но установка не вызывает embedder и не скачивает модели.
Playwright package фиксируется в dev группе; браузеры отдельно не установлены.
После изменения manifests единственный владелец обновляет lockfiles.
`uv.lock` использует публичный PyPI; npm lock — публичный npm registry.
Не наследовать user indexes, секреты или окружения другого проекта.

## Linux/amd64: изолированная build-проверка

```powershell
pwsh -File scripts/verify-linux.ps1
```

Требуется уже запущенный Docker Engine. Скрипт создаёт новый уникальный context
в игнорируемом runtime, копирует только разрешённые файлы исходников, использует
закреплённые Python/Node/uv digests и tag `ai-agent-platform:m0-02-check`.
Это build/test image, не deployment recipe. Нет Compose, ports, volumes,
.env, личной памяти или платного API. Build может скачивать packages/base images.
Проверки после установки выполняются с BuildKit `--network=none`.
Автоматическое удаление чужих контейнеров/образов/volumes не выполняется.
Не запускать второй тяжёлый probe одновременно.

`scripts/check_imports.py` — guard прямых статических внутренних imports по
doc23, не sandbox и не детектор любых динамических imports/внешних эффектов.
Полные функциональные/failure/STOP/recall/browser проверки пока не реализованы.
