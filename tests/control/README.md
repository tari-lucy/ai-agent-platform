# Проверки M1-03

Все данные вымышленные. Реальные SQLite/WAL/migrations и блокировка writer;
readiness, LLM/Redis waits и исполнитель — synthetic protocol fixtures.
Настоящие LLM, Redis dispatcher, каналы, голос и game bridge не подключаются.

42 control cases проверяют start/STOP races, late results, pause/resume,
отдельные mute/GM, sleep/expiry, full buffers, scope/authority, workspace slot,
cancel ID и restart без autorun. 40 samples измеряют STOP при 200 resources /
256 admissions. Цели p95 <= 250 мс и p99 <= 500 мс являются assertions.

С подготовленным Python 3.14.8 и PYTHONPATH шести src-каталогов:

```text
python -m pytest tests/control tests/storage tests/tooling tests/contracts tests/probes/m0_05 tests/probes/m0_06/test_configuration.py -q
python -m mypy apps/core/src apps/worker/src packages scripts
python scripts/check_imports.py
python -m ai_agent_contracts.export --output packages/contracts/generated --check
```

Linux: PowerShell >= 7.3, `pwsh -File tests/control/run-linux.ps1`.
Используется только существующий pinned M0-02 check image, no pull,
network none, UID/GID 10001, CPU 2 / RAM 1536 MiB, read-only source allowlist,
своя synthetic named volume. Тесты storage и control используют эту volume.
Полный checkout/.env/живые данные не монтируются. Volumes/containers не удаляются.

## Packaging

Стандартная `deploy/Dockerfile --target core --network=none` проба не завершилась:
в build cache недоставало registry metadata setuptools. Загрузки для
исправления не выполнялись. Это не успешная cold Docker build.

Дополнительная offline проверка использует новый universal project wheel:

```text
uv build --offline --wheel --python <prepared-3.14.8> --cache-dir <prepared-public-cache> --out-dir runtime/m1-03/dist
pwsh -File tests/control/check-image.ps1
```

Три локальные base tags проверяются по immutable IDs. Из check image копируется
только уже установленный pinned greenlet 3.5.6 и dist-info, который теперь
принадлежит runtime graph SQLAlchemy[asyncio]; остальные dev packages не
переносятся. Wheel устанавливается offline/no-index/no-deps в существующий
core-deps, затем venv и migrations копируются в собственный core proof image.
Production Dockerfile/Compose не переориентируются на test image.

Installed-image probe проверяет отсутствие pytest/playwright, правильную
runtime metadata, SHA256 LF исходников/миграций, реальные empty→0002 и
start→STOP→flush→restart STOPPED. Runtime network none, UID/GID10001,
RAM768 MiB, CPU2, read-only root, отдельная DB volume, без published ports.
Результаты probes лежат в игнорируемом runtime/m1-03; обезличенный summary —
results.json и implementation report. .env не читается.

## Lock

Pinned versions и artifact hashes сохранены. Полный offline resolve был
невозможен из-за неполного metadata cache. Минимально изменены extra links
root→SQLAlchemy[asyncio]→существующий greenlet по установленной pinned metadata.
`uv lock --check --offline` и `uv export --locked --offline --no-dev` прошли;
no-dev graph включает greenlet==3.5.6. Это явно отличается от новой cold install.