"""Synthetic persistence receipt executed only by the M0-04 probe in core."""

import json
import os
import sqlite3
import sys

from redis import Redis
from redis.exceptions import AuthenticationError

client = Redis(
    host="redis",
    port=6379,
    decode_responses=True,
    password=os.environ["REDIS_PASSWORD"],
    socket_timeout=3,
)
try:
    Redis(host="redis", port=6379, socket_timeout=3).ping()
except AuthenticationError:
    pass
else:
    raise AssertionError("Unauthenticated broker access allowed")
conn = sqlite3.connect("/var/lib/platform/core/m0-04-probe.sqlite3")
assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
conn.execute("PRAGMA synchronous=FULL")
if sys.argv[1] == "write":
    conn.execute("CREATE TABLE fixture(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO fixture VALUES(1,'synthetic-persistence')")
    conn.commit()
    client.set("m0-04:marker", "synthetic-persistence")
    client.xadd("m0-04:stream", {"kind": "synthetic"})
    client.xgroup_create("m0-04:stream", "probe-group", id="0")
    assert client.xreadgroup("probe-group", "probe-consumer", {"m0-04:stream": ">"}, count=1)
else:
    assert sys.argv[1] == "verify"
assert conn.execute("SELECT value FROM fixture WHERE id=1").fetchone() == ("synthetic-persistence",)
assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
assert client.get("m0-04:marker") == "synthetic-persistence"
assert client.xlen("m0-04:stream") == 1
assert client.xpending("m0-04:stream", "probe-group")["pending"] == 1
assert client.config_get("protected-mode") == {"protected-mode": "yes"}
settings = client.config_get("appendonly", "appendfsync", "maxmemory", "maxmemory-policy")
assert settings == {
    "appendonly": "yes",
    "appendfsync": "everysec",
    "maxmemory": "134217728",
    "maxmemory-policy": "noeviction",
}
assert client.info("server")["redis_version"] == "8.10.2"
conn.close()
print(
    json.dumps(
        {
            "sqlite": sqlite3.sqlite_version,
            "redis": "8.10.2",
            "sqlite_row": "pass",
            "redis_marker_stream_pending": "pass",
            "wal_full_integrity": "pass",
            "redis_settings": settings,
        }
    )
)
