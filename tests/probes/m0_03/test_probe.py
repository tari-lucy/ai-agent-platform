"""Real SQLite/extension tests without downloading/instantiating any model."""

import sqlite3

import pytest
from probe import install_rows, lexical_ids, open_db, permitted, schema, vector_ids


def test_restart_rollback_and_stale_source_gate(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    conn = open_db(path)
    schema(conn)
    rows = [
        (1, "кошка зонтик", "work", "probe-a", 0, 200),
        (2, "кошка зонтик", "test", "probe-a", 0, 200),
        (3, "кошка зонтик", "work", "probe-b", 0, 200),
        (4, "кошка зонтик", "work", "probe-a", 1, 200),
        (5, "кошка зонтик", "work", "probe-a", 0, 99),
    ]
    import struct

    blob = struct.pack("<384f", 1, *([0] * 383))
    install_rows(conn, rows, [blob] * len(rows))
    assert lexical_ids(conn, "кошка") == [1]
    assert vector_ids(conn, blob) == [1]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("UPDATE sources SET text='not committed' WHERE id=1")
    conn.execute("ROLLBACK")
    conn.close()
    conn = open_db(path)
    assert conn.execute("SELECT text FROM sources WHERE id=1").fetchone()[0] == "кошка зонтик"
    assert vector_ids(conn, blob) == [1]
    conn.execute("UPDATE sources SET deleted=1,text='' WHERE id=1")
    assert not permitted(conn, 1)
    assert lexical_ids(conn, "кошка") == []
    assert vector_ids(conn, blob) == []
    conn.close()


def test_extension_loading_disabled_after_trusted_load(tmp_path):
    conn = open_db(tmp_path / "extensions.sqlite3")
    assert conn.execute("SELECT vec_version()").fetchone()[0] == "v0.1.9"
    with pytest.raises(sqlite3.OperationalError, match="not authorized"):
        conn.execute("SELECT load_extension('untrusted-fixture')")
    conn.close()
