"""M0-03 disposable compatibility probe, never an application repository/schema."""

import argparse
import asyncio
import json
import math
import os
import resource
import sqlite3
import statistics
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np
import sqlite_vec
from download import FILES, MODEL, REVISION, checksums

DIM = 384
NOW = 100
QUERIES = (
    ("Где можно взять зонтик в дождливую погоду?", 1),
    ("Как согреть комнату, когда на улице мороз?", 2),
    ("Чем кормить домашнего кота?", 3),
)


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE sources(
            id INTEGER PRIMARY KEY, text TEXT NOT NULL, scope TEXT NOT NULL,
            persona TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
            expires INTEGER NOT NULL DEFAULT 200
        );
        CREATE VIRTUAL TABLE lexical USING fts5(text, tokenize='unicode61');
        CREATE VIRTUAL TABLE vectors USING vec0(embedding float[384] distance_metric=cosine);
    """)


def fixtures() -> list[tuple[int, str, str, str, int, int]]:
    texts = [
        "Во время дождя можно взять зонтик у входа в дом.",
        "Зимой для обогрева комнаты включают отопление и закрывают окна.",
        "Домашняя кошка ест специальный корм, а свежая вода должна быть рядом.",
        "Для поездки на велосипеде нужны шлем и исправные тормоза.",
        "В библиотеке книги возвращают в установленный срок.",
        "Чай заваривают горячей водой и подают в чашке.",
        "Цветы в горшке регулярно поливают небольшим количеством воды.",
        "Перед приготовлением супа овощи моют и нарезают.",
        "Ночью звёзды видны лучше вдали от ярких фонарей.",
        "Музыкант настраивает гитару перед выступлением.",
        "На вокзале расписание поездов размещают на табло.",
        "Для рисования акварелью используют кисти и плотную бумагу.",
    ]
    rows = [(i, text, "work", "probe-a", 0, 200) for i, text in enumerate(texts, 1)]
    rows += [
        (13, texts[0], "test", "probe-a", 0, 200),
        (14, texts[1], "work", "probe-b", 0, 200),
        (15, texts[2], "work", "probe-a", 1, 200),
        (16, texts[0], "work", "probe-a", 0, 99),
    ]
    return rows


def install_rows(conn: sqlite3.Connection, rows: list[tuple[Any, ...]], blobs: list[bytes]) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.executemany("INSERT INTO sources VALUES(?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT INTO lexical(rowid,text) VALUES(?,?)", [(r[0], r[1]) for r in rows]
        )
        conn.executemany(
            "INSERT INTO vectors(rowid,embedding) VALUES(?,?)",
            zip((r[0] for r in rows), blobs, strict=True),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def permitted(conn: sqlite3.Connection, rowid: int) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sources WHERE id=? AND scope='work' AND persona='probe-a' "
            "AND deleted=0 AND expires>?",
            (rowid, NOW),
        ).fetchone()
        is not None
    )


def vector_ids(conn: sqlite3.Connection, blob: bytes) -> list[int]:
    # Small fixture only: examine all candidates, then recheck authoritative rows.
    # Not the production top-k/filter implementation assigned to M2/M5.
    rows = conn.execute(
        "SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=32 ORDER BY distance",
        (blob,),
    ).fetchall()
    return [r[0] for r in rows if permitted(conn, r[0])][:8]


def lexical_ids(conn: sqlite3.Connection, query: str) -> list[int]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT lexical.rowid FROM lexical JOIN sources s ON s.id=lexical.rowid "
            "WHERE lexical MATCH ? AND s.scope='work' AND s.persona='probe-a' "
            "AND s.deleted=0 AND s.expires>? ORDER BY bm25(lexical)",
            (query, NOW),
        )
    ]


def rss_mib() -> float:
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 3)


def cgroup_memory() -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for name in ("current", "peak", "max"):
        path = Path(f"/sys/fs/cgroup/memory.{name}")
        raw = path.read_text().strip() if path.exists() else "max"
        result[name + "_mib"] = round(int(raw) / 1024**2, 3) if raw.isdigit() else None
    return result


def model_load() -> tuple[Any, float]:
    import onnxruntime
    from fastembed import TextEmbedding

    onnxruntime.disable_telemetry_events()

    manifest = json.loads(Path("/cache/model-manifest.json").read_text())
    expected = json.loads(Path(__file__).with_name("model-manifest.json").read_text())
    assert manifest == expected
    assert manifest["revision"] == REVISION and manifest["model"] == MODEL
    assert manifest["files"] == checksums(Path("/cache/model"))
    assert set(manifest["files"]) == set(FILES)
    started = time.perf_counter()
    model = TextEmbedding(
        model_name=MODEL,
        threads=2,
        providers=["CPUExecutionProvider"],
        cuda=False,
        cache_dir="/tmp/fastembed",
        specific_model_path="/cache/model",
        local_files_only=True,
    )
    return model, round((time.perf_counter() - started) * 1000, 3)


def embed(model: Any, texts: list[str]) -> list[bytes]:
    values = list(model.embed(texts, batch_size=4, parallel=None))
    assert len(values) == len(texts)
    assert all(v.shape == (DIM,) and np.isfinite(v).all() for v in values)
    assert all(float(np.linalg.norm(v)) > 0 for v in values)
    return [v.astype("<f4").tobytes() for v in values]


def queries(conn: sqlite3.Connection, model: Any) -> dict[str, Any]:
    durations = []
    ids = []
    for i in range(30):
        query, expected = QUERIES[i % len(QUERIES)]
        started = time.perf_counter()
        results = vector_ids(conn, embed(model, [query])[0])
        durations.append((time.perf_counter() - started) * 1000)
        assert results and results[0] == expected, (i, expected, results)
        assert not ({13, 14, 15, 16} & set(results))
        if i < len(QUERIES):
            ids.append(results[0])
    return {
        "count": len(durations),
        "ru_top1_ids": ids,
        "median_ms": round(statistics.median(durations), 3),
        "p95_ms": round(sorted(durations)[math.ceil(0.95 * len(durations)) - 1], 3),
        "max_ms": round(max(durations), 3),
    }


async def async_probe(path: Path) -> dict[str, Any]:
    from sqlalchemy import event, text
    from sqlalchemy.ext.asyncio import create_async_engine

    def make_engine() -> Any:
        engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}", connect_args={"isolation_level": None}
        )

        async def init(raw: Any) -> None:
            await raw.enable_load_extension(True)
            try:
                await raw.load_extension(sqlite_vec.loadable_path())
            finally:
                await raw.enable_load_extension(False)
            for command in (
                "PRAGMA journal_mode=WAL",
                "PRAGMA synchronous=FULL",
                "PRAGMA busy_timeout=5000",
            ):
                cursor = await raw.execute(command)
                await cursor.close()

        @event.listens_for(engine.sync_engine, "connect")
        def on_connect(dbapi: Any, record: Any) -> None:
            dbapi.run_async(init)

        @event.listens_for(engine.sync_engine, "begin")
        def on_begin(conn: Any) -> None:
            conn.exec_driver_sql("BEGIN")

        return engine

    engine = make_engine()
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE IF NOT EXISTS async_fixture(id INTEGER PRIMARY KEY)"))
    try:
        async with engine.begin() as conn:
            await conn.execute(text("INSERT INTO async_fixture VALUES(2)"))
            raise RuntimeError("rollback fixture")
    except RuntimeError as error:
        assert str(error) == "rollback fixture"
    async with engine.begin() as conn:
        await conn.execute(text("INSERT INTO async_fixture VALUES(1)"))
    await engine.dispose()
    engine = make_engine()
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT id FROM async_fixture"))).all() == [(1,)]
        extension = (await conn.execute(text("SELECT vec_version()"))).scalar_one()
        assert (await conn.execute(text("PRAGMA journal_mode"))).scalar_one() == "wal"
        assert (await conn.execute(text("PRAGMA synchronous"))).scalar_one() == 2
    await engine.dispose()
    return {"commit_rollback_reconnect": "pass", "sqlite_vec": extension}


def rebuild(conn: sqlite3.Connection, model: Any) -> None:
    rows = conn.execute(
        "SELECT id,text FROM sources WHERE deleted=0 AND expires>?", (NOW,)
    ).fetchall()
    blobs = embed(model, [r[1] for r in rows])
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM lexical")
        conn.execute("DROP TABLE vectors")
        conn.execute(
            "CREATE VIRTUAL TABLE vectors USING vec0(embedding float[384] distance_metric=cosine)"
        )
        conn.executemany("INSERT INTO lexical(rowid,text) VALUES(?,?)", rows)
        conn.executemany(
            "INSERT INTO vectors(rowid,embedding) VALUES(?,?)",
            zip((r[0] for r in rows), blobs, strict=True),
        )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", required=True, choices=("load", "crash", "restart"))
    args = parser.parse_args()
    data = Path("/data")
    path = data / "probe.sqlite3"
    if args.phase == "crash":
        conn = open_db(path)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO sources VALUES(99,'uncommitted synthetic','work','probe-a',0,200)"
        )
        os._exit(0)  # Intentional death of this isolated probe process, no commit/close.
    model, cold_ms = model_load()
    report: dict[str, Any] = {
        "phase": args.phase,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "packages": {
            n: version(n)
            for n in ("sqlite-vec", "fastembed", "onnxruntime", "numpy", "sqlalchemy", "aiosqlite")
        },
        "model_revision": REVISION,
        "threads": 2,
        "batch": 4,
        "cold_model_init_ms": cold_ms,
        "rss_after_model_mib": rss_mib(),
    }
    conn = open_db(path)
    report["extension"] = conn.execute("SELECT vec_version()").fetchone()[0]
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    if args.phase == "load":
        schema(conn)
        rows = fixtures()
        started = time.perf_counter()
        blobs = embed(model, [r[1] for r in rows])
        install_rows(conn, rows, blobs)
        report["index_16_rows_ms"] = round((time.perf_counter() - started) * 1000, 3)
        report["rss_after_index_mib"] = rss_mib()
        assert lexical_ids(conn, "кошка") == [3]
        report["queries"] = queries(conn, model)
        report["async_driver"] = asyncio.run(async_probe(data / "async.sqlite3"))
        report["load"] = "pass"
    else:
        assert conn.execute("SELECT count(*) FROM sources").fetchone()[0] == 16
        assert conn.execute("SELECT 1 FROM sources WHERE id=99").fetchone() is None
        assert lexical_ids(conn, "кошка") == [3]
        report["queries"] = queries(conn, model)
        report["restart_and_uncommitted_recovery"] = "pass"
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE sources SET deleted=1,text='' WHERE id=1")
        conn.execute("COMMIT")
        # Stale derived rows still exist; authoritative recheck must hide id=1 now.
        assert conn.execute("SELECT 1 FROM lexical WHERE rowid=1").fetchone() is not None
        query_blob = embed(model, [QUERIES[0][0]])[0]
        assert 1 not in vector_ids(conn, query_blob)
        assert lexical_ids(conn, "зонтик") == []
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM lexical WHERE rowid=1")
        conn.execute("DELETE FROM vectors WHERE rowid=1")
        conn.execute("COMMIT")
        assert conn.execute("SELECT 1 FROM vectors WHERE rowid=1").fetchone() is None
        started = time.perf_counter()
        rebuild(conn, model)
        report["rebuild_ms"] = round((time.perf_counter() - started) * 1000, 3)
        assert conn.execute("SELECT count(*) FROM vectors").fetchone()[0] == 13
        assert conn.execute("SELECT 1 FROM vectors WHERE rowid IN(1,15,16)").fetchone() is None
        assert 1 not in vector_ids(conn, query_blob)
        conn.close()
        conn = open_db(path)
        assert 1 not in vector_ids(conn, query_blob)
        assert lexical_ids(conn, "зонтик") == []
        report["delete_stale_gate_rebuild_reopen"] = "pass"
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    report["peak_rss_mib"] = rss_mib()
    report["cgroup_memory"] = cgroup_memory()
    assert report["peak_rss_mib"] <= 1536
    (data / (args.phase + "-result.json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
