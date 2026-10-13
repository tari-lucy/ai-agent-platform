"""Real SQLite/FTS5/sqlite-vec negative cases, no model download or live LLM."""

import importlib.util
import sys
from pathlib import Path

import pytest
import sqlite_vec
from recall import (
    apply_tombstones,
    exact_open_promises,
    install,
    lexical_ids,
    open_db,
    rebuild,
    recall,
    recheck,
    schema,
    vector_ids,
)

FIXTURE = (
    Path("/fixtures")
    if Path("/fixtures").exists()
    else Path(__file__).parents[2] / "fixtures" / "recall"
)
spec = importlib.util.spec_from_file_location("fixture_corpus", FIXTURE / "corpus.py")
fixture = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = fixture
spec.loader.exec_module(fixture)
BLOB = sqlite_vec.serialize_float32([1.0] + [0.0] * 383)
QUERY = {"text": "янтарный компас", "scope": "work", "persona": "iskra", "actor": "person-01"}


def row(rowid, **overrides):
    result = {
        "id": rowid,
        "source_id": f"source-{rowid}",
        "text": "янтарный компас",
        "scope": "work",
        "persona": "iskra",
        "actor": "person-01",
        "date": "2026-09-03",
        "version": 1,
        "index_version": 1,
        "deleted": False,
        "expires": 200,
        "permission": True,
        "kind": "promise",
        "status": "open",
    }
    result.update(overrides)
    return result


@pytest.fixture
def db(tmp_path):
    conn = open_db(tmp_path / "fixture.db")
    schema(conn)
    yield conn
    conn.close()


def test_corpus_and_gold_are_stable_and_scoped():
    queries = fixture.load_queries()
    assert len(queries) == 60 and len({q["id"] for q in queries}) == 60
    assert sum(bool(q["gold"]) for q in queries) == 48
    smaller, larger = fixture.corpus(1000), fixture.corpus(10000)
    assert smaller == larger[:1000]
    assert fixture.corpus(10000) == larger
    assert len({r["text"] for r in larger}) == 9976
    for query in queries:
        for gold in query["gold"]:
            source = larger[gold - 1]
            assert (source["scope"], source["persona"], source["actor"]) == (
                query["scope"],
                query["persona"],
                query["actor"],
            )
            assert not source["deleted"] and source["permission"] and source["expires"] > 100
    assert {"conflict", "entity/date", "paraphrase", "nick", "version"} <= {
        q["category"] for q in queries
    }


def test_prefilter_prevents_foreign_top_k_starvation(db):
    rows = [row(i, scope="test") for i in range(1, 65)] + [row(65)]
    install(db, rows, [BLOB] * len(rows))
    assert vector_ids(db, BLOB, QUERY) == [65]
    assert lexical_ids(db, QUERY) == [65]
    assert [r["id"] for r in recall(db, QUERY, BLOB)] == [65]


@pytest.mark.parametrize(
    "overrides",
    [
        {"scope": "test"},
        {"persona": "other"},
        {"actor": "unlinked-person"},
        {"deleted": True},
        {"expires": 100},
        {"permission": False},
        {"version": 2, "index_version": 1},
    ],
)
def test_invalid_source_never_appears(db, overrides):
    install(db, [row(1, **overrides)], [BLOB])
    assert recall(db, QUERY, BLOB) == []
    assert recall(db, QUERY, None, mode="fts") == []
    assert recheck(db, [1], QUERY) == []


def test_permission_revoked_between_candidates_and_release(db):
    install(db, [row(1)], [BLOB])
    assert (
        recall(
            db,
            QUERY,
            BLOB,
            before_recheck=lambda: db.execute("UPDATE sources SET permission=0 WHERE id=1"),
        )
        == []
    )


def test_hash_version_and_duplicate_origin_gate(db):
    install(db, [row(1), row(2, source_id="source-1")], [BLOB, BLOB])
    assert len(recheck(db, [1, 2], QUERY)) == 1
    db.execute("UPDATE sources SET text='changed without new index' WHERE id=1")
    assert recheck(db, [1], QUERY) == []
    db.execute("UPDATE sources SET version=2 WHERE id=2")
    assert recheck(db, [2], QUERY) == []


def test_delete_cache_rebuild_and_restore_gate(db, tmp_path):
    install(db, [row(1)], [BLOB])
    snapshot = open_db(tmp_path / "snapshot.db")
    db.backup(snapshot)
    assert exact_open_promises(db, QUERY) == ["source-1"]
    apply_tombstones(db, [1])
    assert recheck(db, [1], QUERY) == []
    assert exact_open_promises(db, QUERY) == []
    assert rebuild(db) == 1
    assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
    # Tombstone overlay precedes ALL reads after restore.
    apply_tombstones(snapshot, [1])
    assert recheck(snapshot, [1], QUERY) == []
    rebuild(snapshot)
    snapshot.close()
