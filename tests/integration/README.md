# Общая проверка M1 и M2

Из корня checkout, в его подготовленном окружении:

```text
python scripts/check_combined.py
```

Скрипт использует текущий интерпретатор, исходники этого checkout, проверки
ruff/mypy/import boundaries/contract export и общий pytest. Он не устанавливает
пакеты и не читает .env. Подготовка venv по принятому lock: `uv sync --locked --extra recall`;
нужен uv версии из pyproject.toml. Не переносить Windows venv на Linux.

Важно: `tests/memory/test_recall.py` и `tests/probes/m0_05/test_recall.py`
имеют одинаковое имя. Общая команда использует `--import-mode=importlib` и
явные пути тестовых helpers; обычный prepend-режим pytest не подходит для этого набора.
Наличие installed metadata `ai-agent-platform` проверяют tooling/bootstrap tests:
ошибка PackageNotFoundError указывает на неполную подготовку venv, её нельзя считать
успешным прохождением или подавлять.

Все профили/SQLite-данные синтетические и изолированные. По умолчанию четыре
Redis tests пропускаются без специального fixture host; Windows также пропускает FIFO.
Проверка с настоящим Redis и его restart/AOF — отдельная изолированная проба
`tests/jobs/run-linux.ps1`. Runtime-композиция persona/memory/social, HTTP,
платные API и публичные эффекты этой проверкой не подтверждаются.
