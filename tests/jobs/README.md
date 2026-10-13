# M1-04 acceptance

Данные полностью синтетические. SQLite и Redis настоящие; readiness, action
Policy и вычисления — fixtures. Нет live LLM, платных API, env/рабочей памяти.

Windows: подготовленный Python из M0-02; PYTHONPATH указывает шесть src текущего
checkout. Выполнить pytest для tests/jobs, tests/control, tests/storage,
tests/tooling, tests/contracts, tests/probes/m0_05 и m0_06/test_configuration.py.
Четыре Redis cases пропускаются без M1_04_REDIS_HOST, не используют .env.

Linux: PowerShell 7.3+, ./tests/jobs/run-linux.ps1. Требуются уже подготовленные
immutable check/Redis images; pull запрещён. Одна проба с private internal
network, отдельными named volumes, без published ports и рабочей DB. Публичный
synthetic-test-only — только пароль disposable fixture, не секрет пользователя.
Python image UID 10001/2 CPU/1536 MiB, Redis UID 999/0.5 CPU/256 MiB,
read-only roots. В finally останавливается собственный Redis; volumes и
контейнеры сохраняются. Проверяется настоящий Redis restart/AOF reconnect.

Проверки: rollback/commit boundaries, outbox loss/redelivery, PEL reclaim/XACK,
capacity, scoped service authority, request/result dedup, 96-секундный heartbeat,
fencing старой попытки, unknown settlement/history, quotas, paginated recovery,
restart без autorun, direct STOP с настоящим SQLite write lock и paused/full Redis.
Сбой worker/core на transaction boundary инъецирован; OS process kill не имитируется
как реальная проверка. Технический success не публикует/не применяет payload.

results.json содержит обезличенную квитанцию финального Linux прогона,
hash source files/lock и результаты Windows/Linux. Сырые логи/runtime вне Git.
