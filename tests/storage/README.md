# Проверки M1-02

Только синтетические данные и реальные отдельные SQLite databases.
Ни ключи, ни рабочая память, ни API провайдеров не используются.

В Python 3.14 окружении из uv.lock направьте PYTHONPATH на src текущего checkout:

```powershell
$env:PYTHONPATH = (@(
  'apps/core/src', 'apps/worker/src', 'packages/contracts/src',
  'packages/domain/src', 'packages/application/src', 'packages/adapters/src'
) | ForEach-Object { Join-Path $PWD $_ }) -join ';'
python -m pytest tests/storage -q
python -m mypy
python -m ruff check packages tests/storage migrations
```

Для Linux/Docker Desktop: `./tests/storage/run-linux.ps1`.
Нужен уже проверенный локальный image digest из скрипта; implicit pull нет.
Каждый запуск создаёт свой named volume и завершающиеся контейнеры. Маленький
helper меняет владельца только этого volume на UID/GID 10001; suite работает
под 10001:10001, network none, readonly root, CPU 2 / RAM 1536 MiB.
Source mounts — allowlist readonly; root checkout, .env и живая БД не монтируются.
Контейнеры/volumes не удаляются автоматически. Локальная квитанция —
runtime/m1-02; обезличенный итог — results.json.

Проверки: empty/previous migration, единственный head, integrity/FK/PRAGMA,
CAS и дубли параллельно, rollback/cancellation/caught failure, durable replay,
scope/owner/ACL/rights/TTL, source dependencies и зарегистрированные extensions,
удалённые исходные редакции, точные people/accounts, immutable attempts,
один активный workspace, bounded page и отсутствие core DB mount у worker.
