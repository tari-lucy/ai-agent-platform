# Проверки M1-01

Только синтетические IDs/текст/публичные dummy markers. Реальной БД, модели,
.env, пользовательских профилей и внешних API нет.

Windows: использовать Python из проверенного M0-02 lock environment и
PYTHONPATH своего checkout (six apps/packages src roots).

```text
python -m pytest tests/tooling tests/contracts tests/probes/m0_06/test_configuration.py
python -m ruff check packages/contracts tests/contracts
python -m mypy
python -m ai_agent_contracts.export --output packages/contracts/generated --check
node <M0-02-panel-node_modules>/typescript/bin/tsc --strict --noEmit --target ES2022 --module ESNext --moduleResolution Bundler tests/contracts/type-consumer.ts
```

Linux из PowerShell: `./tests/contracts/run-linux.ps1`. Только уже имеющийся
pinned M0-02 check image; отсутствие образа завершает проверку без pull/install.
Bind mounts перечисляют только код, generated contracts, tests, scripts,
два entrypoint и config/defaults. .env/Git/runtime не монтируются.
Network none; readonly; CPU 2 / RAM+swap 1536 MiB / pids 256 / tmpfs 128 MiB.
Проверяются export, import boundaries, lint/types и combined M0+M1 suite.
Контейнер останавливается по завершении и сохраняется; удаления нет.
Квитанция с image/lock/export hashes и exit/OOM/network — ignored runtime/m1-01.

78 contract checks: версии/UTC/scope, registered extension, безопасные ошибки,
полный JSON/дубли/NaN/depth/размер, replay/conflict, stale run/epoch/profile/policy/
attempt/lease/operation, неизвестные usage/cost, independent state dimensions,
finite waiting/reservation, removed sources, streaming, STOP receipt,
OpenAPI refs/security и детерминированный экспорт.
