"""Copies: WAL consistency, allowlist, private files, explicit non-overlapping locations."""

import asyncio
import json
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from lb_support import (
    FOREIGN_NAMES,
    KEPT,
    SECRET,
    Serve,
    cli,
    copy_id,
    make_host,
    run,
    with_live,
)

from ai_agent_core.local_backup import create_backup_callback, layout
from ai_agent_core.local_host.layout import HostError


def test_create_list_verify_a_copy_taken_while_wal_is_live(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)

        async def work(live):
            await live.add(KEPT, "k-1")
            await live.add(f"Он сказал: {SECRET}.", "d-1")
            # the store is still open: committed pages sit in the -wal file, not in the main file
            wal = Path(str(host.paths.database) + "-wal")
            assert wal.exists() and wal.stat().st_size > 0
            return await asyncio.to_thread(cli, "create", *host.args("--kind", "manual"))

        return host, await with_live(host, work)

    host, (code, out, err) = run(scenario())
    assert code == 0, err
    backup_id = copy_id(out)
    assert "verified=true" in out
    listed = cli("list", *host.args(ledger=False))
    assert listed[0] == 0 and backup_id in listed[1]
    verified = cli("verify", *host.args("--backup-id", backup_id, ledger=False))
    assert verified[0] == 0 and "verified: true" in verified[1]

    copy = host.store_dir / backup_id
    connection = sqlite3.connect(f"{(copy / 'database.sqlite').as_uri()}?mode=ro", uri=True)
    sources = connection.execute("SELECT COUNT(*) FROM records WHERE kind='Source'").fetchone()[0]
    connection.close()
    assert sources == 2  # both commits were only in the WAL of the live file


def test_a_copy_holds_only_allowlisted_files_and_no_secret_or_log(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        (host.root / ".env").write_text("AI_PLATFORM_LLM_API_KEY=sk-FICTIONAL-0000\n")
        (host.root / "debug.log").write_text("log line with Янтарный-шифр\n")
        return host

    host = run(scenario())
    code, out, err = cli("create", *host.args())
    assert code == 0, err
    copy = host.store_dir / copy_id(out)
    assert sorted(p.name for p in copy.iterdir()) == [
        "database.sqlite",
        "host.json",
        "manifest.json",
    ]
    manifest = json.loads((copy / "manifest.json").read_text())
    assert sorted(f["name"] for f in manifest["files"]) == ["database.sqlite", "host.json"]
    password = host.paths.password.read_text().strip()
    blob = b"".join(p.read_bytes() for p in copy.iterdir())
    assert not any(name.encode() in blob for name in FOREIGN_NAMES[:1])
    assert b"sk-FICTIONAL-0000" not in blob and password.encode() not in blob
    assert host.paths.password_hash.read_text().strip().encode() not in blob


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes; Windows ACLs are not claimed")
def test_copies_work_files_and_ledger_copies_are_owner_only(tmp_path):
    host = run(make_host(tmp_path))
    code, out, err = cli("create", *host.args())
    assert code == 0, err
    copy = host.store_dir / copy_id(out)
    modes = {p: stat.S_IMODE(p.stat().st_mode) for p in (host.store_dir, copy, host.ledger.parent)}
    assert modes[host.store_dir] == 0o700 and modes[copy] == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in copy.iterdir())
    ledger_copies = host.ledger.with_name(host.ledger.name + ".copies")
    assert stat.S_IMODE(ledger_copies.stat().st_mode) == 0o700
    assert [p.name for p in host.store_dir.iterdir() if not p.name.startswith(".")] == [copy.name]
    assert list((host.store_dir / ".work").glob("make-*")) == []  # no snapshot left behind


def test_locations_must_be_absolute_explicit_and_apart_from_the_live_host(tmp_path):
    host = run(make_host(tmp_path))
    good = layout(host.root, host.store_dir, host.ledger)
    assert good.store_dir == host.store_dir

    def refused(root, store, ledger):
        with pytest.raises(HostError) as info:
            layout(root, store, ledger)
        return info.value.code

    assert refused(Path("rel/host"), host.store_dir, host.ledger) == "ABSOLUTE_ROOT_REQUIRED"
    assert refused(host.root, Path("copies"), host.ledger) == "ABSOLUTE_PATH_REQUIRED"
    assert refused(host.root, host.store_dir, Path("l.sqlite")) == "ABSOLUTE_PATH_REQUIRED"
    assert refused(host.root, host.root / "data" / "copies", host.ledger) == "BACKUP_PATHS_OVERLAP"
    assert refused(host.root, host.store_dir, host.root / "ledger.sqlite") == "BACKUP_PATHS_OVERLAP"
    assert refused(host.root, tmp_path, host.ledger) == "BACKUP_PATHS_OVERLAP"  # contains the host
    assert refused(host.root, host.store_dir, host.store_dir / "ledger.sqlite") == (
        "BACKUP_PATHS_OVERLAP"
    )
    link = tmp_path / "link-to-store"
    link.symlink_to(host.store_dir if host.store_dir.exists() else tmp_path)
    assert refused(host.root, link, host.ledger) == "BACKUP_PATH_UNSAFE"


def test_errors_never_print_paths_or_content(tmp_path):
    host = run(make_host(tmp_path, ledger=False))
    code, out, err = cli("create", *host.args())
    assert code != 0 and "LEDGER_UNAVAILABLE" in err
    assert str(tmp_path) not in out + err and SECRET not in out + err


def test_create_runs_beside_a_running_host_and_the_callback_needs_no_runtime(tmp_path):
    host = run(make_host(tmp_path))
    with Serve(host, tmp_path / "serve.log") as server:
        assert server.run_state() == "STOPPED"
        code, out, err = cli("create", *host.args())  # online: SQLite backup API, no host lock
        assert code == 0, err

    place = layout(host.root, host.store_dir, host.ledger)

    async def scenario():
        callback = create_backup_callback(place, kind="daily")  # opens the existing ledger itself
        return await callback()

    result = run(scenario())
    assert result.manifest.kind == "daily" and result.manifest.schema_head == "0003"
    code, out, err = cli("list", *host.args(ledger=False))
    assert result.manifest.backup_id in out
