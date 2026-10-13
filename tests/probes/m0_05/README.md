# M0-05 retrieval baseline

Зона только disposable benchmark; SQL schema не является миграцией приложения.
Точные обещания читаются отдельно от top-k. Search: scope/actor/permission/
TTL/version prefilter → FTS5 unicode61 + cosine sqlite-vec → equal-weight
RRF (constant 60, 32 кандидата на ветку) → дедуп source ID и authoritative
scope/version/hash recheck → максимум 8 fragments. No neural reranker.

Параметры заданы до первого замера. MiniLM/384/CPU 2 threads/batch 4 —
публичная модель M0-03; all manifest checksums проверяются до загрузки.
Модель и check image повторно не загружаются, network none. Cache RO.
Один ограниченный контейнер одновременно, fresh Linux named volume,
RAM/swap 1536 MiB, 2 CPU, pids 256, read-only root, tmpfs 128 MiB.

```powershell
./tests/probes/m0_05/run.ps1 -ModelCache <absolute-public-M0-03-cache>
```

Сначала pytest test_recall.py без модели; benchmark phases index/query на
каждом размере в разных процессах. Время cold_from_script включает imports/
hash check/model init, но не Docker startup; model_init отдельно. Индексация
батчами по 100 строк, внутри embed batch 4. Query phase: 60 unmeasured warm-up,
3 × 60 measured calls каждого режима, каждый query embedding вычисляется
заново. FTS-only без embeddings; vector/hybrid включает query embedding.
p95 nearest-rank; JSON содержит каждую выдачу первого measured pass.
Gate: hybrid Recall@8 ≥0.85; p95 ≤500 ms; peak cgroup ≤1536 MiB; leaks=0.
RSS/cgroup peak cumulative within phase, не сумма пиков разных контейнеров.

Rebuild удаляет revoked/deleted/expired/version-stale производные записи.
Restore overlay имитирует применение более нового deletion/rights/version
ledger перед чтением старого snapshot; без ledger обещание удаления невозможно.
M5 ещё должен реализовать реальный lifecycle/backup/derived summaries.
Context 2000-token packaging и owner concurrency — M2, здесь только 8 fragments.

Результаты копируются в ignored runtime/m0-05; containers/volumes остаются
остановленными, без down/prune/delete. При провале gate runner возвращает
ошибку и сохраняет JSON; пороги/gold не снижаются.
