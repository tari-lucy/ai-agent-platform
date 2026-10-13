# M0-04 Compose foundation

Три Linux/amd64 сервиса: core со статической панелью, idle worker, Redis.
Это проверенная инфраструктура для следующего кода; agent/jobs/API/auth/STOP
ещё не реализованы. Worker не открывает core DB и не скачивает модели.

```powershell
pwsh -File deploy/init-local-secrets.ps1
pwsh -File deploy/compose.ps1 -Action config
pwsh -File deploy/compose.ps1 -Action build
pwsh -File deploy/compose.ps1 -Action up -Project ai-agent-platform -Port 8765
pwsh -File deploy/compose.ps1 -Action stop -Project ai-agent-platform
```

Wrapper отключает implicit .env чтение и передаёт только явно выбранный локальный
env-file для runtime binding; config --quiet не печатает expanded env. Helper
создаёт новый 256-bit random Redis password в ignored .env, не читает и не
перезаписывает существующий файл и не выводит значение. Пароль получает только
core/Redis; worker не получает. На M0-06 — полная config validation/env example.
Redis protected-mode=yes; ephemeral auth config — в private tmpfs mode 0600,
health PING использует REDISCLI_AUTH без password в argv. Node — только build stage;
models не загружаются build/up. Python packages стоят из uv.lock без dev.
.dockerignore задаёт allowlist, исключающий локальные env/data/cache/.git/profiles.
Не менять чужие containers/volumes; project name должен быть собственным.

Core опубликован на 127.0.0.1:<Port>, внутри default 8080 (конфигурируется
AI_PLATFORM_CORE_CONTAINER_PORT). Worker/Redis без published ports. Внутренний network
platform имеет internal:true; Redis/worker находятся только в нём. Core также
подключён к bridge core-loopback для фактического Windows port forwarding;
эта сеть даёт core transport egress, но никакие внешние calls не выполняются.
Host/LLM integrations будут отдельными разрешёнными bindings. Redis доступен в
изолированном network с auth; детальная ACL/service identity относится к M1.

Core-data, worker-cache, redis-data — отдельные Linux named volumes.
SQLite и Redis data не находятся в Windows bind. Redis: AOF/everysec,
maxmemory 128 MiB/noeviction; RDB snapshots выключены. Это не backup и
не обещание нулевой потери последней секунды при аварийном выключении.

Containers: non-root core/worker 10001, Redis 999; read-only root, private tmpfs,
cap_drop ALL/no-new-privileges/pids 256, resource caps 768/1536/256 MiB,
1/2/0.5 CPU, memory-swap равно memory. Logs bounded 10 MiB × 3 per service.
restart:no требует явного up; ни up, restart, recreate не активируют агента.
Health — core TCP listener, worker monotonic heartbeat, Redis PING;
это process liveness, не application readiness/supervisor correctness.

Реальная acceptance проба создаёт новый project/port/volumes и останавливает
его в finally, ничего не удаляя:

```powershell
pwsh -File tests/probes/m0_04/run.ps1
```

Проверяется panel HTTP 200, health/resource/loopback/private ports, SQLite row,
Redis marker+Stream+pending consumer group и worker cache после restart и
force-recreate. Результаты — runtime/m0-04/<run>/result.json, отчёт M0-04.
Поднятый собственный stack завершать stop; down -v/prune здесь не используются.
