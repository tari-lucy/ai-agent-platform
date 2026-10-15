"""Restore on a rehearsal area: what comes back, what must not, and every refusal."""

import asyncio
import shutil
import sqlite3
from types import SimpleNamespace

from ai_agent_adapters.retention import SQLiteDeletionLedger
from lb_support import (
    KEPT,
    SECRET,
    Live,
    Serve,
    cli,
    copy_id,
    digest_of,
    live_digest,
    make_host,
    rehearsal,
    residue,
    run,
    with_live,
)

POST = "Запись после снимка о тихом озере."


def backup(host, *more):
    code, out, err = cli("create", *host.args(*more))
    assert code == 0, err
    return copy_id(out)


def restore(host, backup_id, decision="dec-1"):
    code, out, err = cli("preview-restore", *host.args("--backup-id", backup_id))
    assert code == 0, (out, err)
    return cli(
        "restore",
        *host.args("--backup-id", backup_id, "--decision-id", decision, "--digest", digest_of(out)),
    )


def issue_codes(host, backup_id):
    code, out, err = cli("preview-restore", *host.args("--backup-id", backup_id))
    return code, [line.split()[1] for line in out.splitlines() if line.startswith("issue:")], err


async def seed(host):
    async def work(live):
        await live.add(KEPT, "k-1")
        return await asyncio.to_thread(backup, host)

    return await with_live(host, work)


# ---- what is restored --------------------------------------------------------------------


def test_changes_after_the_snapshot_are_gone_and_the_kept_fact_is_readable(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        backup_id = await seed(host)

        async def later(live):
            await live.add(POST, "p-1")
            assert await live.hears("тихом озере")

        await with_live(host, later)
        area = rehearsal(host, tmp_path)
        before = live_digest(host)
        code, out, err = await asyncio.to_thread(restore, area, backup_id)
        assert code == 0, err

        async def after(live):
            return await live.hears("парусах"), await live.hears("озере")

        return host, area, before, out, await with_live(area, after)

    host, area, before, out, (kept, gone) = run(scenario())
    assert kept and not gone  # the kept fact reads through owner/recall; the later one is absent
    assert "agent state: STOPPED" in out and "safety_copy=bk-" in out
    assert live_digest(host) == before  # the original area was never touched
    sources = (
        sqlite3.connect(area.paths.database)
        .execute("SELECT COUNT(*) FROM records WHERE kind='Source'")
        .fetchone()[0]
    )
    assert sources == 1
    assert (area.store_dir / ".replaced").is_dir()  # the replaced files wait for the operator


def test_a_deletion_after_the_snapshot_does_not_come_back(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)

        async def first(live):
            await live.add(KEPT, "k-1")
            return await live.add(f"Он сказал: {SECRET}, вот так.", "d-1")

        async def snapshot(live):
            return await asyncio.to_thread(backup, host)

        doomed = await with_live(host, first)
        backup_id = await with_live(host, snapshot)

        async def erase(live):
            await live.delete(doomed)
            assert not await live.hears("Янтарный-шифр")

        await with_live(host, erase)
        code, out, err = await asyncio.to_thread(restore, host, backup_id)
        assert code == 0, err

        async def after(live):
            return await live.hears("Янтарный-шифр"), await live.hears("парусах")

        return host, out, await with_live(host, after)

    host, out, (revived, kept) = run(scenario())
    assert not revived and kept
    assert "reapplied_deletions=1" in out
    assert residue(host, SECRET) == []  # not in the database, WAL, SHM or the rebuilt index


# ---- refusals ----------------------------------------------------------------------------


def test_a_tampered_copy_is_refused_and_the_live_files_stay_as_they_are(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        return host, await seed(host)

    host, backup_id = run(scenario())
    target = host.store_dir / backup_id / "database.sqlite"
    raw = bytearray(target.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    target.write_bytes(bytes(raw))
    before = live_digest(host)
    code, found, _ = issue_codes(host, backup_id)
    assert code != 0 and "CHECKSUM_MISMATCH" in found
    assert restore_refused(host, backup_id) and live_digest(host) == before


def restore_refused(host, backup_id):
    code, _, err = cli(
        "restore", *host.args("--backup-id", backup_id, "--decision-id", "d", "--digest", "0" * 64)
    )
    return code != 0 and ("PREVIEW_CHANGED" in err or "refused" in err)


def test_a_missing_empty_or_substituted_ledger_closes_the_restore(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        return host, await seed(host)

    host, backup_id = run(scenario())
    kept = host.ledger.with_name("kept.sqlite")
    host.ledger.rename(kept)

    code, _, err = cli("preview-restore", *host.args("--backup-id", backup_id))
    assert code != 0 and "LEDGER_UNAVAILABLE" in err
    assert not host.ledger.exists()  # no empty ledger was created in place of the history

    host.ledger.write_bytes(b"")  # an empty file would silently become a new, empty ledger
    code, _, err = cli("preview-restore", *host.args("--backup-id", backup_id))
    assert code != 0 and "LEDGER_UNAVAILABLE" in err and host.ledger.stat().st_size == 0

    host.ledger.unlink()
    SQLiteDeletionLedger(host.ledger).close()  # a valid but different, empty ledger
    code, found, _ = issue_codes(host, backup_id)
    assert code != 0 and "LEDGER_UNAVAILABLE" in found

    host.ledger.unlink()
    kept.rename(host.ledger)
    assert issue_codes(host, backup_id)[0] == 0  # the real ledger admits the copy again


def test_a_rolled_back_ledger_closes_the_restore(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)

        async def first(live):
            await live.add(KEPT, "k-1")
            return await live.add(f"Он сказал: {SECRET}.", "d-1")

        doomed = await with_live(host, first)
        first_copy = await with_live(host, lambda live: asyncio.to_thread(backup, host))

        async def erase(live):
            await live.delete(doomed)

        await with_live(host, erase)
        second_copy = await with_live(host, lambda live: asyncio.to_thread(backup, host))
        return host, first_copy, second_copy

    host, first_copy, second_copy = run(scenario())
    older = host.ledger.with_name(host.ledger.name + ".copies") / f"{first_copy}.ledger.sqlite"
    host.ledger.unlink()
    shutil.copyfile(older, host.ledger)  # same ledger, earlier history
    code, found, _ = issue_codes(host, second_copy)
    assert code != 0 and "LEDGER_OLDER_THAN_COPY" in found


def test_a_running_serve_refuses_preview_and_restore(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        return host, await seed(host)

    host, backup_id = run(scenario())
    with Serve(host, tmp_path / "serve.log"):
        code, out, err = cli("preview-restore", *host.args("--backup-id", backup_id))
        assert code != 0 and "HOST_ALREADY_RUNNING" in err and not out
        code, out, err = cli(
            "restore",
            *host.args("--backup-id", backup_id, "--decision-id", "d", "--digest", "0" * 64),
        )
        assert code != 0 and "HOST_ALREADY_RUNNING" in err
    assert not (host.store_dir / ".replaced").exists()
    assert len([p for p in host.store_dir.iterdir() if not p.name.startswith(".")]) == 1
    assert not list((host.store_dir / ".work").glob("restore-*"))  # nothing was even staged


def test_a_changed_preview_or_a_missing_decision_never_restores(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        return host, await seed(host)

    host, backup_id = run(scenario())
    before = live_digest(host)
    code, _, err = cli(
        "restore", *host.args("--backup-id", backup_id, "--decision-id", "d", "--digest", "a" * 64)
    )
    assert code != 0 and "PREVIEW_CHANGED" in err
    _, out, _ = cli("preview-restore", *host.args("--backup-id", backup_id))
    code, _, err = cli(
        "restore",
        *host.args(
            "--backup-id", backup_id, "--decision-id", "bad id!", "--digest", digest_of(out)
        ),
    )
    assert code != 0 and "INVALID_OPTION" in err and live_digest(host) == before


def test_a_failure_in_staging_or_in_the_switch_leaves_the_live_files_alone(tmp_path, monkeypatch):
    import ai_agent_adapters.backup.switch as switch
    from ai_agent_adapters.backup import SQLiteRestoreTarget

    async def scenario():
        host = await make_host(tmp_path)
        return host, await seed(host)

    host, backup_id = run(scenario())
    before = live_digest(host)

    async def broken(self, scope):
        raise RuntimeError("staging-failed")

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteRestoreTarget, "rebuild_index", broken)
        code, _, err = restore(host, backup_id)
    assert code != 0 and "RuntimeError" in err and live_digest(host) == before

    def refuse(*_args, **_kwargs):
        raise OSError("disk-full")

    with monkeypatch.context() as patch:
        patch.setattr(  # only the switcher's own copy fails; the rest keeps the real shutil
            switch, "shutil", SimpleNamespace(copyfile=refuse, rmtree=shutil.rmtree)
        )
        code, _, err = restore(host, backup_id)
    assert code != 0 and "OSError" in err and live_digest(host) == before
    assert str(tmp_path) not in err and "disk-full" not in err

    code, out, err = restore(host, backup_id)  # the same copy restores once the fault is gone
    assert code == 0, err


# ---- compatibility with the host ----------------------------------------------------------


def adopt(copy_from, copy_to, backup_id):
    shutil.copytree(copy_from.store_dir / backup_id, copy_to.store_dir / backup_id)


def test_a_copy_of_another_host_configuration_is_refused(tmp_path):
    async def scenario():
        mock = await make_host(tmp_path, "mock")
        backup_id = await seed(mock)
        pilot = await make_host(tmp_path, "pilot", live_model="fictional-model")
        wide = await make_host(tmp_path, "wide", test_scopes=3)
        return mock, backup_id, pilot, wide

    mock, backup_id, pilot, wide = run(scenario())
    for other in (pilot, wide):
        adopt(mock, other, backup_id)
        before = live_digest(other)
        code, found, _ = issue_codes(other, backup_id)
        assert code != 0 and "HOST_CONFIG_INCOMPATIBLE" in found
        assert "ROLE_BINDINGS_DIFFER" in found or "SCOPES_INCOMPATIBLE" in found
        assert "GRANTS_DIFFER" in found or other is pilot
        assert live_digest(other) == before
    # the copy fits the host it came from, with no host-compatibility issue at all
    compat = {
        "HOST_CONFIG_INCOMPATIBLE",
        "SCOPES_INCOMPATIBLE",
        "GRANTS_DIFFER",
        "ROLE_BINDINGS_DIFFER",
    }
    assert not compat & set(issue_codes(mock, backup_id)[1])


def test_a_restore_cannot_lower_the_durable_api_attempt_count(tmp_path):
    async def scenario(model):
        host = await make_host(tmp_path, "live" if model else "mock", live_model=model)

        async def first(live: Live):
            await live.attempt("op-1")
            return await asyncio.to_thread(backup, host)

        backup_id = await with_live(host, first)
        await with_live(host, lambda live: live.attempt("op-2"))
        return host, backup_id

    pilot, pilot_copy = run(scenario("fictional-model"))
    code, found, _ = issue_codes(pilot, pilot_copy)
    assert code != 0 and "API_ATTEMPTS_WOULD_ROLL_BACK" in found

    mock, mock_copy = run(scenario(None))  # no live pilot: no cap to reset, restore is allowed
    assert "API_ATTEMPTS_WOULD_ROLL_BACK" not in issue_codes(mock, mock_copy)[1]


def test_a_copy_without_the_retention_tables_is_not_restorable(tmp_path):
    host = run(make_host(tmp_path, retention=False))
    backup_id = backup(host)
    code, found, _ = issue_codes(host, backup_id)
    assert code != 0 and "RETENTION_NOT_INSTALLED" in found  # the production binding is open


def test_a_copy_with_an_unlisted_file_is_refused(tmp_path):
    host = run(make_host(tmp_path))
    backup_id = backup(host)
    folder = host.store_dir / backup_id
    extra = folder / "operator-password.txt"
    extra.write_text("fictional-password\n")
    import hashlib
    import json

    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["files"].append(
        {
            "name": extra.name,
            "sha256": hashlib.sha256(extra.read_bytes()).hexdigest(),
            "size": extra.stat().st_size,
        }
    )
    (folder / "manifest.json").write_text(json.dumps(manifest))
    code, found, _ = issue_codes(host, backup_id)
    assert code != 0 and "FILE_NOT_ALLOWED" in found


# ---- after the restore --------------------------------------------------------------------


def test_after_restore_a_new_serve_is_stopped_and_nothing_old_runs_again(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)

        async def work(live):
            await live.add(KEPT, "k-1")

        await with_live(host, work)
        connection = sqlite3.connect(host.paths.database)
        scope_id = connection.execute("SELECT scope_id FROM scopes").fetchone()[0]
        connection.execute(
            "INSERT INTO records (scope_id, kind, id, owner, version, created_at, updated_at,"
            " deleted, available, read_allowed, api_transfer_allowed, public_disclosure_allowed)"
            " VALUES (?, 'Job', 'job-1', 'jobs', 1, '2026-10-13T12:00:00', '2026-10-13T12:00:00',"
            " 0, 1, 1, 1, 1)",
            (scope_id,),
        )
        connection.execute(
            "INSERT INTO record_revisions VALUES (?, 'Job', 'job-1', 1, '1.0',"
            ' \'{"id":"job-1","schema_version":"1.0","status":"RUNNING"}\', NULL)',
            (scope_id,),
        )
        connection.execute("UPDATE outbox SET status='delivered' WHERE rowid % 2 = 0")
        connection.commit()
        old_events = {r[0] for r in connection.execute("SELECT event_id FROM outbox")}
        connection.close()
        backup_id = await with_live(host, lambda live: asyncio.to_thread(backup, host))
        return host, backup_id, old_events

    host, backup_id, old_events = run(scenario())
    code, out, err = cli("preview-restore", *host.args("--backup-id", backup_id))
    assert code == 0 and "open_operations=1" in out and "OPERATIONS_AT_RISK warning" in out
    code, out, err = restore(host, backup_id)
    assert code == 0, err
    with Serve(host, tmp_path / "serve.log") as server:
        assert server.run_state() == "STOPPED"  # a restore never starts the agent
    connection = sqlite3.connect(host.paths.database)
    job = connection.execute(
        "SELECT json_extract(payload,'$.status') FROM record_revisions WHERE id='job-1'"
    ).fetchone()[0]
    live_events = connection.execute(
        "SELECT COUNT(*) FROM outbox WHERE status IN ('pending','delivered')"
        f" AND event_id IN ({','.join('?' * len(old_events))})",
        sorted(old_events),
    ).fetchone()[0]
    attempts = connection.execute(
        "SELECT COUNT(*) FROM records WHERE kind='LLMAttempt'"
    ).fetchone()[0]
    connection.close()
    assert job == "RUNNING"  # unknown stays unknown: nobody re-executed or closed it
    assert live_events == 0 and attempts == 0
