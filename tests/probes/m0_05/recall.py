"""M0-05 disposable benchmark adapter, not a production repository or migration."""

import hashlib
import json
import re
import sqlite3
from collections import defaultdict

import sqlite_vec

NOW = 100
DIM = 384
CANDIDATES = 32
TOP_K = 8
RRF_CONSTANT = 60
STOPWORDS = frozenset(
    "а и в во на по с со у из к ко как какой какая какие где когда что чего "
    "чем почему ли не о об для от до после перед при это этого или можно "
    "его ее ещё есть было был была сколько".split()
)
ELIGIBLE = (
    "s.scope=? AND s.persona=? AND s.actor=? AND s.deleted=0 "
    "AND s.expires>? AND s.permission=1 AND s.version=i.version"
)


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def open_db(path):
    conn = sqlite3.connect(path, isolation_level=None, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def schema(conn):
    conn.executescript("""
        CREATE TABLE sources(
            id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, text TEXT NOT NULL,
            scope TEXT NOT NULL, persona TEXT NOT NULL, actor TEXT NOT NULL,
            date TEXT NOT NULL, version INTEGER NOT NULL, deleted INTEGER NOT NULL,
            expires INTEGER NOT NULL, permission INTEGER NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL
        );
        CREATE TABLE indexed(id INTEGER PRIMARY KEY, version INTEGER NOT NULL, hash TEXT NOT NULL);
        CREATE INDEX source_scope ON sources(scope,persona,actor,deleted,permission);
        CREATE VIRTUAL TABLE lexical USING fts5(text, tokenize='unicode61');
        CREATE VIRTUAL TABLE vectors USING vec0(embedding float[384] distance_metric=cosine);
    """)


def install(conn, rows, blobs):
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row, blob in zip(rows, blobs, strict=True):
            conn.execute(
                "INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                tuple(
                    row[k]
                    for k in (
                        "id",
                        "source_id",
                        "text",
                        "scope",
                        "persona",
                        "actor",
                        "date",
                        "version",
                        "deleted",
                        "expires",
                        "permission",
                        "kind",
                        "status",
                    )
                ),
            )
            conn.execute(
                "INSERT INTO indexed VALUES(?,?,?)",
                (row["id"], row["index_version"], digest(row["text"])),
            )
            conn.execute("INSERT INTO lexical(rowid,text) VALUES(?,?)", (row["id"], row["text"]))
            conn.execute("INSERT INTO vectors(rowid,embedding) VALUES(?,?)", (row["id"], blob))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def parameters(query):
    return (query["scope"], query["persona"], query["actor"], NOW)


def recheck(conn, ids, query):
    """Revalidate authority after candidate selection, including stale cache IDs."""
    result, seen = [], set()
    for rowid in ids:
        row = conn.execute(
            "SELECT s.*,i.hash FROM sources s JOIN indexed i ON i.id=s.id "
            "WHERE s.id=? AND " + ELIGIBLE,
            (rowid, *parameters(query)),
        ).fetchone()
        if row is None or digest(row["text"]) != row["hash"] or row["source_id"] in seen:
            continue
        seen.add(row["source_id"])
        result.append(
            {
                "id": row["id"],
                "source_id": row["source_id"],
                "version": row["version"],
                "date": row["date"],
                "sha256": row["hash"],
                "text": row["text"],
                "scope": row["scope"],
                "persona": row["persona"],
                "actor": row["actor"],
            }
        )
        if len(result) == TOP_K:
            break
    return result


def vector_ids(conn, blob, query):
    # The IN constraint filters BEFORE KNN on the pinned extension, not after global top-k.
    return [
        r["rowid"]
        for r in conn.execute(
            "SELECT rowid,distance FROM vectors WHERE embedding MATCH ? AND k=? "
            "AND rowid IN(SELECT s.id FROM sources s JOIN indexed i ON i.id=s.id WHERE "
            + ELIGIBLE
            + ") ORDER BY distance",
            (blob, CANDIDATES, *parameters(query)),
        )
    ]


def lexical_ids(conn, query):
    tokens = sorted(set(re.findall(r"\w+", query["text"].lower())) - STOPWORDS)
    if not tokens:
        return []
    expression = " OR ".join('"' + word + '"' for word in tokens)
    return [
        r["rowid"]
        for r in conn.execute(
            "SELECT lexical.rowid FROM lexical JOIN sources s ON s.id=lexical.rowid "
            "JOIN indexed i ON i.id=s.id WHERE lexical MATCH ? AND "
            + ELIGIBLE
            + " ORDER BY bm25(lexical),lexical.rowid LIMIT ?",
            (expression, *parameters(query), CANDIDATES),
        )
    ]


def fused_ids(lexical, vector):
    scores = defaultdict(float)
    for ranking in (lexical, vector):
        for rank, rowid in enumerate(ranking, 1):
            scores[rowid] += 1 / (RRF_CONSTANT + rank)
    return sorted(scores, key=lambda rowid: (-scores[rowid], rowid))


def recall(conn, query, blob, mode="hybrid", before_recheck=None):
    lexical = lexical_ids(conn, query) if mode != "vector" else []
    vector = vector_ids(conn, blob, query) if mode != "fts" else []
    selected = fused_ids(lexical, vector)
    if before_recheck:
        before_recheck()
    return recheck(conn, selected, query)


def exact_open_promises(conn, query):
    # Independent exact ledger; no vector top-k is consulted.
    return [
        r["source_id"]
        for r in conn.execute(
            "SELECT s.source_id FROM sources s WHERE s.scope=? AND s.persona=? "
            "AND s.actor=? AND s.deleted=0 AND s.expires>? AND s.permission=1 "
            "AND s.kind='promise' AND s.status='open' ORDER BY s.id",
            parameters(query),
        )
    ]


def apply_tombstones(conn, ids):
    for rowid in ids:
        conn.execute("UPDATE sources SET deleted=1 WHERE id=?", (rowid,))


def rebuild(conn):
    """Discard inadmissible index records; authority remains unchanged."""
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT i.id FROM indexed i JOIN sources s ON s.id=i.id WHERE "
            "s.deleted=1 OR s.expires<=? OR s.permission=0 OR s.version<>i.version",
            (NOW,),
        )
    ]
    for rowid in ids:
        conn.execute("DELETE FROM vectors WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM lexical WHERE rowid=?", (rowid,))
        conn.execute("DELETE FROM indexed WHERE id=?", (rowid,))
    return len(ids)


def corpus_hash(rows):
    return digest(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
