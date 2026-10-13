"""Offline Linux M0-05 retrieval benchmark, synthetic data only."""

import argparse
import json
import math
import resource
import sqlite3
import statistics
import sys
import time
from importlib.metadata import version
from pathlib import Path

SCRIPT_START = time.perf_counter()
sys.path.insert(0, "/fixtures")
sys.path.insert(0, "/compat")
from corpus import corpus, load_queries  # noqa: E402
from probe import cgroup_memory, embed, model_load  # noqa: E402
from recall import (  # noqa: E402
    apply_tombstones,
    corpus_hash,
    exact_open_promises,
    install,
    open_db,
    rebuild,
    recall,
    recheck,
    schema,
)


def percentile(values):
    return round(sorted(values)[math.ceil(len(values) * 0.95) - 1], 3)


def memory():
    current = None
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            current = round(int(line.split()[1]) / 1024, 3)
    return {
        "rss_current_mib": current,
        "rss_peak_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3),
        "cgroup": cgroup_memory(),
    }


def measure(conn, model, queries, mode):
    # Every measured call computes its query embedding anew; no query/result cache.
    totals, embedding_ms, search_ms, scores, outputs = [], [], [], [], []
    for repetition in range(3):
        for query in queries:
            started = time.perf_counter()
            blob = embed(model, [query["text"]])[0] if mode != "fts" else None
            embedded = time.perf_counter()
            result = recall(conn, query, blob, mode=mode)
            ended = time.perf_counter()
            totals.append((ended - started) * 1000)
            embedding_ms.append((embedded - started) * 1000)
            search_ms.append((ended - embedded) * 1000)
            ids = [r["id"] for r in result]
            assert len(ids) <= 8 and len(ids) == len(set(ids))
            assert result == recheck(conn, ids, query), "Provenance/scope mismatch"
            if query["gold"]:
                scores.append(len(set(ids) & set(query["gold"])) / len(query["gold"]))
            # All disallowed fixtures are 25..60; check every query, not only negatives.
            assert not set(ids) & set(range(25, 61)), "Forbidden source disclosure"
            if repetition == 0:
                outputs.append(
                    {
                        "query_id": query["id"],
                        "source_ids": [r["source_id"] for r in result],
                        "chunk_ids": ids,
                        "gold": query["gold"],
                        "recall_at_8": scores[-1] if query["gold"] else None,
                    }
                )
    return {
        "mode": mode,
        "calls": len(totals),
        "positive_queries": 48,
        "negative_queries": 12,
        "recall_at_8": round(statistics.mean(scores), 6),
        "p95_ms": percentile(totals),
        "median_ms": round(statistics.median(totals), 3),
        "max_ms": round(max(totals), 3),
        "embedding_p95_ms": percentile(embedding_ms),
        "search_p95_ms": percentile(search_ms),
        "forbidden_disclosures": 0,
        "outputs_first_pass": outputs,
    }


def lifecycle(conn, path, queries):
    backup = open_db(path.with_name(path.stem + "-backup.db"))
    conn.backup(backup)
    q = queries[0]
    cached = [1, 7, 11, 25, 31]
    assert recheck(conn, [1], q)
    apply_tombstones(conn, [1])
    conn.execute("UPDATE sources SET permission=0 WHERE id=7")
    conn.execute("UPDATE sources SET version=3 WHERE id=11")
    assert recheck(conn, cached, q) == []
    cleaned = rebuild(conn)
    assert conn.execute("SELECT count(*) FROM indexed WHERE id IN (1,7,11)").fetchone()[0] == 0
    # Simulated restore overlays the later deletion/rights/version ledger BEFORE any recall.
    apply_tombstones(backup, [1])
    backup.execute("UPDATE sources SET permission=0 WHERE id=7")
    backup.execute("UPDATE sources SET version=3 WHERE id=11")
    assert recheck(backup, cached, q) == []
    rebuild(backup)
    backup.close()
    reopened = open_db(path)
    assert recheck(reopened, cached, q) == []
    assert exact_open_promises(reopened, queries[12]) == []  # revoked promise 7
    reopened.close()
    return {
        "stale_cache_gate": True,
        "source_delete": True,
        "permission_revoke": True,
        "version_gate": True,
        "rebuild_removed": cleaned,
        "reopen_gate": True,
        "restore_ledger_overlay": True,
        "scope": "disposable fixture; production backup/lifecycle is M5",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("index", "query"), required=True)
    parser.add_argument("--size", type=int, choices=(1000, 10000), required=True)
    args = parser.parse_args()
    path = Path(f"/data/corpus-{args.size}.db")
    queries = load_queries()
    rows = corpus(args.size)
    conn = open_db(path)
    model, init_ms = model_load()
    cold = round((time.perf_counter() - SCRIPT_START) * 1000, 3)
    base = {
        "phase": args.phase,
        "chunks": args.size,
        "corpus_sha256": corpus_hash(rows),
        "queries_sha256": corpus_hash(queries),
        "cold_from_script_ms": cold,
        "model_init_ms": init_ms,
        "versions": {p: version(p) for p in ("fastembed", "onnxruntime", "sqlite-vec", "numpy")},
        "sqlite": sqlite3.sqlite_version,
    }
    if args.phase == "index":
        schema(conn)
        started = time.perf_counter()
        for offset in range(0, len(rows), 100):
            batch = rows[offset : offset + 100]
            install(conn, batch, embed(model, [r["text"] for r in batch]))
            if (offset + len(batch)) % 1000 == 0:
                print(json.dumps({"indexed": offset + len(batch), "target": args.size}), flush=True)
        base["index_ms"] = round((time.perf_counter() - started) * 1000, 3)
        base["memory"] = memory()
        base["vector_rows"] = conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
        assert base["vector_rows"] == args.size
        assert exact_open_promises(conn, queries[12]) == ["episode-007"]
    else:
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == args.size
        # Complete unmeasured warm-up pass, identical queries but no embedding cache.
        for query in queries:
            recall(conn, query, embed(model, [query["text"]])[0])
        base["measurements"] = [
            measure(conn, model, queries, mode) for mode in ("fts", "vector", "hybrid")
        ]
        base["memory"] = memory()
        base["lifecycle"] = lifecycle(conn, path, queries)
        hybrid = base["measurements"][-1]
        base["acceptance"] = {
            "recall": hybrid["recall_at_8"] >= 0.85,
            "latency": hybrid["p95_ms"] <= 500,
            "memory": base["memory"]["cgroup"]["peak_mib"] <= 1536,
            "privacy": hybrid["forbidden_disclosures"] == 0,
        }
    conn.close()
    Path(f"/data/{args.phase}-{args.size}.json").write_text(
        json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {k: v for k, v in base.items() if k not in ("measurements",)}, ensure_ascii=False
        )
    )
    if args.phase == "query" and not all(base["acceptance"].values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
