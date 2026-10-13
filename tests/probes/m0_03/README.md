# M0-03: совместимость storage/embeddings

Изолированный эксперимент, не application repository/schema/recall adapter.
Потребляет зависимости из принятой M0-02; manifests/locks не меняются.
Все тексты вымышленные; каталог не импортирует личную память/env.

Требуется собранный Linux check image M0-02 и свободный слот тяжёлых проб.
Из корня своего checkout:

```powershell
pwsh -File tests/probes/m0_03/run.ps1 -DownloadModel
# После первой загрузки — полностью offline:
pwsh -File tests/probes/m0_03/run.ps1
```

Если образ отсутствует, сначала scripts/verify-linux.ps1 (M0-02).
Runner фиксирует фактический image ID; можно передать -BaseImage sha256:<ID>.
DownloadModel получает только публичный pinned ONNX snapshot Hugging Face,
не inference API. Никакие токены не запрашиваются и не наследуются.
Revision/hashes проверяются по model-manifest.json до inference.
Вес модели и cache остаются в игнорируемом runtime; в Git только receipt hashes.

Для каждого запуска создаётся уникальный Linux named volume для disposable DB,
новые containers, 2 CPU/1536 MiB memory и memory-swap/256 pids. Model cache —
отдельный bind, read-only в offline фазах. Корневой FS read-only, tmpfs 128 MiB.
Порты не публикуются; load/crash/restart/tests/lint/format имеют network none.
Контейнеры после завершения и volume сохраняются для проверки; автоматического
удаления/очистки других проектов нет. Тяжёлые фазы идут последовательно.

Load: 16 synthetic sources, FTS5 unicode61, vec0 cosine float32[384],
FastEmbed native MiniLM vectors (не предполагается unit norm), threads 2/batch 4,
3 RU paraphrases × 10; WAL/FULL/5s busy timeout; SQLAlchemy+aiosqlite
commit/rollback/dispose/reconnect с extension initializer на каждой connection;
legacy implicit begin отключён, SQLAlchemy begin явно выполняет BEGIN.
Crash: отдельный процесс оставляет незакоммиченную запись и завершается os._exit.
Restart: новый процесс и embedder, проверка committed rows/отсутствия crash row,
stale index/tombstone gate, очистка, rebuild из ещё разрешённых по сроку/удалению
sources, повторное открытие и integrity_check.
Scope/persona проверяются при выдаче; partition implementation относится к M2.

Latency — model initialization отдельно от checksum/import cost; 30 warm
query samples включают query embedding и SQLite lookup. RSS — Linux ru_maxrss;
cgroup current/peak включает container/page cache. 16 rows — compatibility
fixture, не 60-query/1k/10k recall benchmark M0-05 и не доказательство его p95.
Нет TTL/permissions/backup/runtime реализации M1/M2/M5.

Результаты каждого run: runtime/m0-03/<run>/load-result.json,
restart-result.json, run-receipt.json; permanent DB остаётся в новом named volume.
Фактические versions/digests/results/непроверенное — implementation report M0-03.
