"""Backup / restore / migration guard on real SQLite files (M5-02)."""

import asyncio
import shutil
import sqlite3
from datetime import timedelta

import pytest
from ai_agent_application.backup import BackupPolicy
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from backup_support import (
    HEAD,
    NOW,
    make_scope,
    new_bworld,
    reopen,
    residue,
    run,
)

A = make_scope("persona-a")
B = make_scope("persona-b")
SECRET = "Янтарный-шифр-СеребристыйКлюч"


def code(info):
    return info.value.error.code


def codes(issues):
    return {i.code for i in issues}


async def deleted_and_expired_world(tmp_path):
    """Backup taken while two sources are alive; afterwards one is deleted and one expires."""
    bw = await new_bworld(tmp_path, [A, B])
    w = bw.world
    doomed = await w.add(A, f"Он сказал: {SECRET}, вот так.", external_id="d-1")
    aging = await w.add(A, f"Старая беседа {SECRET} о погоде.", external_id="a-1", protected=False)
    keep = await w.add(A, "Долгая беседа о парусах и ветре.", external_id="k-1", protected=True)
    other = await w.add(B, "Запись другой личности про озеро.", external_id="b-1", protected=True)
    return bw, doomed, aging, keep, other


# ---- copies -------------------------------------------------------------------------------


def test_snapshot_is_consistent_while_a_writer_is_active(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        w = bw.world
        for number in range(5):
            await w.add(A, f"Исходная запись номер {number}.", external_id=f"s-{number}")

        async def writer():
            for number in range(40):
                await w.add(A, f"Параллельная запись {number}.", external_id=f"w-{number}")

        task = asyncio.create_task(writer())
        made = await bw.service.create("daily")
        await task
        problems = await bw.service.verify(made.manifest.backup_id)
        copy = await bw.store.path(made.manifest.backup_id, "database.sqlite")
        connection = sqlite3.connect(copy)
        rows = connection.execute("SELECT COUNT(*) FROM records WHERE kind='Source'").fetchone()[0]
        orphans = connection.execute(
            "SELECT COUNT(*) FROM records r LEFT JOIN record_revisions v"
            " ON r.scope_id=v.scope_id AND r.kind=v.kind AND r.id=v.id AND r.version=v.version"
            " WHERE v.id IS NULL"
        ).fetchone()[0]
        connection.close()
        # a plain file copy of the live main file (what a naive backup does) misses WAL pages
        naive = tmp_path / "naive.sqlite"
        shutil.copyfile(w.paths[0], naive)
        try:
            connection = sqlite3.connect(naive)
            naive_rows = connection.execute(
                "SELECT COUNT(*) FROM records WHERE kind='Source'"
            ).fetchone()[0]
            connection.close()
        except sqlite3.DatabaseError:
            naive_rows = -1
        return problems, rows, orphans, naive_rows, made

    problems, rows, orphans, naive_rows, made = run(scenario())
    assert problems == [] and orphans == 0  # every row has its revision: one moment in time
    assert 5 <= rows <= 45
    assert naive_rows < rows  # the file-copy control really is worse
    assert made.manifest.schema_head == HEAD


def test_seven_daily_copies_and_a_separate_pre_migration_copy(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        await bw.world.add(A, "Запись для копий.", external_id="s-1", protected=True)
        kept_ids = []
        for _day in range(10):
            bw.world.clock.advance(days=1)
            kept_ids.append((await bw.service.create("daily")).manifest.backup_id)
        migration = await bw.service.create("pre_migration")
        daily = [m for m in await bw.service.copies() if m.kind == "daily"]
        return bw, kept_ids, daily, migration

    bw, ids, daily, migration = run(scenario())
    assert [m.backup_id for m in daily] == ids[:2:-1]  # the 7 newest, newest first
    assert len(daily) == 7 and migration.manifest.kind == "pre_migration"
    assert not any(not (bw.base / "copies" / i).exists() for i in [m.backup_id for m in daily])
    assert all(not (bw.base / "copies" / i).exists() for i in ids[:3])  # older are gone
    assert not (bw.base / "ledger-copies" / f"{ids[0]}.ledger.sqlite").exists()


def test_a_copy_never_outlives_the_text_term(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        old = await bw.service.create("pre_migration")
        bw.world.clock.advance(days=31)
        fresh = await bw.service.create("daily")
        return old, fresh, await bw.service.copies()

    old, fresh, left = run(scenario())
    assert fresh.pruned == (old.manifest.backup_id,)
    assert [m.backup_id for m in left] == [fresh.manifest.backup_id]


def test_manifest_lists_only_expected_files_and_no_secrets(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        env = tmp_path / ".env"
        env.write_text("AI_PLATFORM_KEY=do-not-copy")
        profile = tmp_path / "profile.md"
        profile.write_text("# Профиль\nВымышленный характер.")
        bw.extra.items.append(("profile-main.md", profile))
        made = await bw.service.create("manual")
        return bw, made

    bw, made = run(scenario())
    names = sorted(f.name for f in made.manifest.files)
    assert names == ["database.sqlite", "profile-main.md"]
    files = sorted(p.name for p in (bw.base / "copies" / made.manifest.backup_id).iterdir())
    assert files == ["database.sqlite", "manifest.json", "profile-main.md"]
    blob = (bw.base / "copies" / made.manifest.backup_id / "manifest.json").read_text()
    assert "do-not-copy" not in blob and "AI_PLATFORM" not in blob


def test_extra_files_cannot_escape_or_shadow_the_database(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        outcomes = []
        for name in ("../escape", "database.sqlite", "manifest.json", ""):
            bw.extra.items = [(name, tmp_path / "x")]
            (tmp_path / "x").write_text("x")
            with pytest.raises(ContractError) as info:
                await bw.service.create("manual")
            outcomes.append(code(info))
        return outcomes, await bw.service.copies()

    outcomes, left = run(scenario())
    assert outcomes == [ErrorCode.INVALID_ARGUMENTS] * 4 and left == []


def test_quota_warns_at_eighty_and_ninety_percent_and_stops_when_full(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        first = await bw.service.create("manual")
        used = bw.store.used_bytes()
        results = {}
        for label, quota in (
            ("80", int(used / 0.85)),
            ("90", int(used / 0.95)),
            ("none", used * 10),
        ):
            bw.service.policy = BackupPolicy(quota_bytes=quota, keep_daily=1)
            results[label] = bw.service._quota_warnings()
        bw.service.policy = BackupPolicy(quota_bytes=used)
        with pytest.raises(ContractError) as info:
            await bw.service.create("manual")
        return first, results, code(info), await bw.service.copies()

    first, results, full, left = run(scenario())
    assert results == {"80": ["QUOTA_80"], "90": ["QUOTA_90"], "none": []}
    assert full is ErrorCode.RESOURCE_BUSY
    assert [m.backup_id for m in left] == [first.manifest.backup_id]  # nothing erased to make room


def test_policy_is_validated():
    for bad in (
        dict(keep_daily=0),
        dict(keep_pre_migration=0),
        dict(max_age=timedelta(0)),
        dict(quota_bytes=0),
    ):
        with pytest.raises(ContractError):
            BackupPolicy(**bad)


# ---- verification ---------------------------------------------------------------------------


def test_a_tampered_copy_is_detected_and_cannot_be_restored(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        await bw.world.add(A, "Запись для проверки.", external_id="s-1", protected=True)
        made = await bw.service.create("daily")
        path = await bw.store.path(made.manifest.backup_id, "database.sqlite")
        data = bytearray(path.read_bytes())
        data[len(data) // 2] ^= 0xFF
        path.write_bytes(bytes(data))
        issues = await bw.service.verify(made.manifest.backup_id)
        preview = await bw.service.preview_restore(made.manifest.backup_id)
        with pytest.raises(ContractError) as info:
            await bw.service.restore(preview, "decision-1")
        return issues, preview, code(info)

    issues, preview, refused = run(scenario())
    assert "CHECKSUM_MISMATCH" in codes(issues) and preview.blocked
    assert refused is ErrorCode.CAPABILITY_UNAVAILABLE


def test_a_missing_or_resized_file_is_reported(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        made = await bw.service.create("daily")
        (await bw.store.path(made.manifest.backup_id, "database.sqlite")).unlink()
        return await bw.service.verify(made.manifest.backup_id)

    assert codes(run(scenario())) == {"FILE_MISSING_OR_RESIZED"}


# ---- restore gate ---------------------------------------------------------------------------


def test_restore_is_refused_while_the_agent_is_not_stopped(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        made = await bw.service.create("daily")
        bw.gate.state = "ACTIVE"
        preview = await bw.service.preview_restore(made.manifest.backup_id)
        with pytest.raises(ContractError) as info:
            await bw.service.restore(preview, "decision-1")
        return preview, code(info), bw

    preview, refused, bw = run(scenario())
    assert "AGENT_NOT_STOPPED" in codes(preview.issues) and refused is ErrorCode.ACCESS_DENIED
    assert not (bw.base / "replaced").exists()  # nothing was touched


def test_restore_needs_an_operator_decision_and_an_unchanged_preview(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        await bw.world.add(A, "Запись.", external_id="s-1", protected=True)
        made = await bw.service.create("daily")
        preview = await bw.service.preview_restore(made.manifest.backup_id)
        with pytest.raises(ContractError) as no_decision:
            await bw.service.restore(preview, "")
        # a deletion made after the preview is NOT a change of the decision: restore applies it
        await bw.world.ledger.block(A, "src-x", 1, "operator_delete", NOW)
        same = await bw.service.preview_restore(made.manifest.backup_id)
        path = await bw.store.path(made.manifest.backup_id, "database.sqlite")
        data = bytearray(path.read_bytes())
        data[len(data) // 3] ^= 0xFF  # the copy itself changes after the operator looked
        path.write_bytes(bytes(data))
        stale = await bw.service.preview_restore(made.manifest.backup_id)
        with pytest.raises(ContractError) as changed:
            await bw.service.restore(preview, "decision-1")
        return preview, same, stale, code(no_decision), code(changed)

    preview, same, stale, denied, conflict = run(scenario())
    assert denied is ErrorCode.ACCESS_DENIED and conflict is ErrorCode.VERSION_CONFLICT
    assert preview.digest == same.digest != stale.digest


def test_a_lost_replaced_or_rolled_back_ledger_blocks_the_copy(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        await bw.world.ledger.block(A, "src-1", 1, "operator_delete", NOW)
        made = await bw.service.create("daily")
        id_ = made.manifest.backup_id
        ok = await bw.service.preview_restore(id_)
        # a brand-new empty ledger instead of the real one
        from ai_agent_adapters.retention import SQLiteDeletionLedger

        fresh = SQLiteDeletionLedger(tmp_path / "new-ledger.sqlite")
        original = bw.service.ledger
        bw.service.ledger = fresh
        replaced = await bw.service.preview_restore(id_)
        # the right ledger, but an older copy of it (rolled back)
        old = SQLiteDeletionLedger(tmp_path / "old-ledger.sqlite")
        bw.service.ledger = old
        old_identity = await old.identity()
        # same id, lower counter
        connection = sqlite3.connect(tmp_path / "old-ledger.sqlite")
        connection.execute("UPDATE ledger_meta SET ledger_id=?, seq=0", (made.manifest.ledger_id,))
        connection.commit()
        connection.close()
        old.close()
        old = SQLiteDeletionLedger(tmp_path / "old-ledger.sqlite")
        bw.service.ledger = old
        rolled = await bw.service.preview_restore(id_)
        bw.service.ledger = original
        return ok, replaced, rolled, old_identity, made

    ok, replaced, rolled, _, made = run(scenario())
    assert made.manifest.ledger_seq >= 1
    assert "LEDGER_UNAVAILABLE" not in codes(ok.issues) and not ok.blocked
    assert "LEDGER_UNAVAILABLE" in codes(replaced.issues) and replaced.blocked
    assert "LEDGER_OLDER_THAN_COPY" in codes(rolled.issues) and rolled.blocked


def test_a_broken_ledger_blocks_instead_of_being_ignored(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        made = await bw.service.create("daily")

        class Broken:
            async def identity(self):
                raise RuntimeError("ledger file unreadable")

        bw.service.ledger = Broken()
        return await bw.service.preview_restore(made.manifest.backup_id)

    preview = run(scenario())
    assert "LEDGER_UNAVAILABLE" in codes(preview.issues) and preview.blocked


def test_schema_newer_than_software_is_blocked_and_older_needs_a_migrator(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        made = await bw.service.create("daily")
        bw.service.current_head = "0002"  # software older than the copy
        newer = await bw.service.preview_restore(made.manifest.backup_id)
        bw.service.current_head = "0004"  # software newer: the copy needs a migration
        older = await bw.service.preview_restore(made.manifest.backup_id)
        return newer, older

    newer, older = run(scenario())
    assert "SCHEMA_NEWER_THAN_SOFTWARE" in codes(newer.issues) and newer.blocked
    assert older.needs_migration and "MIGRATION_REQUIRED" in codes(older.issues)


# ---- the restore itself ---------------------------------------------------------------------


def test_restoring_an_old_copy_does_not_revive_deleted_or_expired_text(tmp_path):
    async def scenario():
        bw, doomed, aging, keep, other = await deleted_and_expired_world(tmp_path)
        w = bw.world
        made = await bw.service.create("daily")  # all four sources are alive in the copy
        assert residue([bw.base / "copies" / made.manifest.backup_id / "database.sqlite"], SECRET)
        # later: the operator deletes one; another reaches the end of its term
        preview = await w.retention.preview_delete(A, [doomed.source_id])
        await w.retention.delete(A, preview)
        w.clock.advance(days=40)
        await w.retention.sweep(A)
        view = await bw.service.preview_restore(made.manifest.backup_id)
        report = await bw.service.restore(view, "decision-1")
        store, memory, recall, physical, index = await reopen(bw)
        from memory_support import World

        answer = await recall.recall(
            __import__("ai_agent_contracts.memory", fromlist=["RecallRequest"]).RecallRequest(
                scope=A, query=SECRET, purpose="private_context"
            )
        )
        stubs = {
            "doomed": await physical.source(A, doomed.source_id),
            "aging": await physical.source(A, aging.source_id),
            "keep": await physical.source(A, keep.source_id),
            "other": await physical.source(B, other.source_id),
        }
        inventory = await index.inventory(A)
        await store.close()
        index.close()
        del World
        return bw, view, report, answer, stubs, inventory, (doomed, aging, keep)

    bw, view, report, answer, stubs, inventory, (doomed, aging, keep) = run(scenario())
    assert not view.blocked and report.agent_state == "STOPPED"
    assert not stubs["doomed"].has_content and stubs["doomed"].availability == "deleted"
    assert not stubs["aging"].has_content and stubs["aging"].availability == "expired"
    assert stubs["keep"].has_content and stubs["other"].has_content  # the rest is restored
    assert {doomed.source_id, aging.source_id} <= set(report.reapplied) | set()
    assert all(
        h.source.source_id not in {doomed.source_id, aging.source_id} for h in answer.fragments
    )
    assert doomed.source_id not in inventory and aging.source_id not in inventory
    assert keep.source_id in inventory  # the index was rebuilt from the cleaned authority
    live = [bw.world.paths[0], bw.world.paths[1]]
    assert residue(live, SECRET) == []  # not in the restored database, WAL or index either


def test_restore_takes_a_safety_copy_and_keeps_the_replaced_files_aside(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        w = bw.world
        await w.add(A, f"Запись до копии {SECRET}.", external_id="s-1", protected=True)
        made = await bw.service.create("daily")
        await w.add(A, "Запись после копии, её нет в копии.", external_id="s-2", protected=True)
        view = await bw.service.preview_restore(made.manifest.backup_id)
        report = await bw.service.restore(view, "decision-1")
        copies = await bw.service.copies()
        return bw, report, copies

    bw, report, copies = run(scenario())
    assert report.safety_backup_id in {m.backup_id for m in copies}
    assert any(m.kind == "pre_restore" for m in copies)
    assert len(bw.switcher.replaced()) == 1  # the replaced files are kept, not deleted
    safety = next(m for m in copies if m.kind == "pre_restore")
    assert safety.created_at <= bw.world.clock.now
    kept = bw.base / "replaced" / bw.switcher.replaced()[0]
    assert any(item.suffix == ".sqlite" for item in kept.iterdir())
    bw.switcher.discard(bw.switcher.replaced()[0])
    assert bw.switcher.replaced() == []  # the operator can wipe them after checking


def test_restored_events_are_quarantined_and_risky_operations_are_reported(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        w = bw.world
        await w.put_projection(A, "op-open", "QUEUED", age_days=0)
        connection = sqlite3.connect(w.paths[0])
        connection.execute("PRAGMA journal_mode=WAL")
        queued = connection.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
        connection.close()
        made = await bw.service.create("daily")
        view = await bw.service.preview_restore(made.manifest.backup_id)
        report = await bw.service.restore(view, "decision-1")
        connection = sqlite3.connect(w.paths[0])
        live = connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE status IN ('pending','delivered')"
        ).fetchone()[0]
        connection.close()
        return queued, view, report, live

    queued, view, report, live = run(scenario())
    assert view.pending_outbox == queued and queued >= 1
    assert report.quarantined_outbox == queued and live == 0  # nothing replays after a restore
    assert not view.blocked  # a warning, not a refusal


def test_a_failed_verification_leaves_the_live_database_untouched(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        w = bw.world
        made_source = await w.add(A, f"Живая запись {SECRET}.", external_id="s-1", protected=True)
        made = await bw.service.create("daily")
        before = (w.paths[0].read_bytes(), made_source)
        view = await bw.service.preview_restore(made.manifest.backup_id)

        class Failing:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                return getattr(self.inner, name)

            async def rebuild_index(self, scope):
                raise RuntimeError("index build failed")

        original = bw.service.staging.build

        async def build(database):
            return Failing(await original(database))

        bw.service.staging.build = build
        with pytest.raises(RuntimeError):
            await bw.service.restore(view, "decision-1")
        return bw, before

    bw, (before, _) = run(scenario())
    assert bw.base.joinpath("replaced").exists() is False  # no switch happened
    assert not (bw.base / "work" / f"restore-{next(iter([]), '')}").exists()
    live = sqlite3.connect(bw.world.paths[0])
    count = live.execute("SELECT COUNT(*) FROM records WHERE kind='Source'").fetchone()[0]
    live.close()
    assert count == 1 and before  # the live store is as it was


def test_restore_migrates_an_older_copy_in_staging_before_the_switch(tmp_path):
    async def scenario():
        seen = []

        async def upgrade(path):
            seen.append(path.name)
            connection = sqlite3.connect(path)
            connection.execute("UPDATE alembic_version SET version_num=?", (HEAD,))
            connection.commit()
            connection.close()

        bw = await new_bworld(tmp_path, [A], upgrade_staged=upgrade)
        connection = sqlite3.connect(bw.world.paths[0])  # the live file stands for an older release
        connection.execute("UPDATE alembic_version SET version_num='0002'")
        connection.commit()
        connection.close()
        made = await bw.service.create("daily")
        view = await bw.service.preview_restore(made.manifest.backup_id)
        report = await bw.service.restore(view, "decision-1")
        connection = sqlite3.connect(bw.world.paths[0])
        head = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        connection.close()
        return made, view, report, seen, head

    made, view, report, seen, head = run(scenario())
    assert made.manifest.schema_head == "0002" and view.needs_migration and not view.blocked
    assert head == HEAD and seen  # migrated in staging, never on the copy or the live file


# ---- migration guard ------------------------------------------------------------------------


def test_the_upgrade_runs_only_after_a_verified_copy_exists(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        calls = []

        async def upgrade():
            copies = [m.kind for m in await bw.service.copies()]
            calls.append(copies)

        report = await bw.service.guarded_migration(upgrade)
        return report, calls

    report, calls = run(scenario())
    assert report.migrated and calls == [["pre_migration"]]  # the copy existed at upgrade time


def test_a_failed_upgrade_names_the_copy_to_restore_from(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])

        async def upgrade():
            raise RuntimeError("secret detail must not leak: " + SECRET)

        return await bw.service.guarded_migration(upgrade)

    report = run(scenario())
    assert not report.migrated and report.error == "RuntimeError"
    assert report.restore_from == report.backup_id and SECRET not in repr(report)


def test_an_unverified_copy_blocks_the_upgrade(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        calls = []

        async def upgrade():
            calls.append(1)

        async def corrupt(backup_id):
            return [
                __import__("ai_agent_application.backup", fromlist=["Issue"]).Issue(
                    "CHECKSUM_MISMATCH"
                )
            ]

        bw.service.verify = corrupt
        return await bw.service.guarded_migration(upgrade), calls

    report, calls = run(scenario())
    assert not report.migrated and report.error == "BACKUP_UNVERIFIED" and calls == []


def test_migration_needs_a_stopped_agent(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        bw.gate.state = "ACTIVE"
        with pytest.raises(ContractError) as info:
            await bw.service.guarded_migration(lambda: asyncio.sleep(0))
        return code(info), await bw.service.copies()

    refused, copies = run(scenario())
    assert refused is ErrorCode.ACCESS_DENIED and copies == []


def test_a_rehearsal_runs_on_a_copy_and_leaves_the_live_database_alone(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        await bw.world.add(A, "Живая запись.", external_id="s-1", protected=True)
        before = (
            sqlite3.connect(bw.world.paths[0])
            .execute("SELECT version_num FROM alembic_version")
            .fetchone()[0]
        )

        async def upgrade(path):
            connection = sqlite3.connect(path)
            connection.execute("UPDATE alembic_version SET version_num='0004'")
            connection.commit()
            connection.close()

        good = await bw.service.rehearse_migration(upgrade)

        async def failing(path):
            raise RuntimeError("bad migration")

        bad = await bw.service.rehearse_migration(failing)
        after = (
            sqlite3.connect(bw.world.paths[0])
            .execute("SELECT version_num FROM alembic_version")
            .fetchone()[0]
        )
        return good, bad, before, after, await bw.service.copies()

    good, bad, before, after, copies = run(scenario())
    assert good.migrated and not bad.migrated and bad.error == "RuntimeError"
    assert before == after == HEAD and copies == []


def test_removed_copies_are_wiped_before_unlinking(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        await bw.world.add(A, f"Запись {SECRET}.", external_id="s-1", protected=True)
        made = await bw.service.create("manual")
        path = await bw.store.path(made.manifest.backup_id, "database.sqlite")
        with_text = SECRET.encode() in path.read_bytes()
        await bw.store.remove(made.manifest.backup_id)
        return with_text, path.exists(), bw

    with_text, exists, bw = run(scenario())
    assert with_text and not exists


# ---- gaps closed after the first mutation pass ----------------------------------------------


def test_ledger_identity_counts_every_change_and_survives_a_reopen_and_a_copy(tmp_path):
    async def scenario():
        from ai_agent_adapters.retention import SQLiteDeletionLedger

        path = tmp_path / "ledger.sqlite"
        ledger = SQLiteDeletionLedger(path)
        first = await ledger.identity()
        await ledger.block(A, "s1", 1, "operator_delete", NOW)
        second = await ledger.identity()
        await ledger.mark_purged(A, "s1", NOW)
        third = await ledger.identity()
        await ledger.block(A, "s1", 2, "expired", NOW)  # a no-op upgrade still counts
        fourth = await ledger.identity()
        ledger.close()
        reopened = SQLiteDeletionLedger(path)
        again = await reopened.identity()
        copy = tmp_path / "ledger-copy.sqlite"
        await reopened.snapshot_to(copy)
        copied = SQLiteDeletionLedger(copy)
        return first, second, third, fourth, again, await copied.identity(), await copied.entries(A)

    first, second, third, fourth, again, copied, entries = run(scenario())
    assert second[1] == first[1] + 1 and third[1] == second[1] + 1 and fourth[1] == third[1] + 1
    assert again == fourth == copied and first[0] == again[0]  # same identity across reopen
    assert [e.source_id for e in entries] == ["s1"]


def test_wiping_overwrites_the_bytes_before_unlinking(tmp_path):
    from ai_agent_adapters.backup import wipe

    target = tmp_path / "secret.bin"
    target.write_bytes(SECRET.encode() * 50)
    alias = tmp_path / "alias.bin"
    alias.hardlink_to(target)  # another name for the same bytes
    wipe(target)
    assert not target.exists()
    assert alias.read_bytes() == b"\0" * (len(SECRET.encode()) * 50)  # not merely unlinked


def test_delivered_events_and_open_jobs_are_both_neutralised_and_reported(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])
        w = bw.world
        await w.put_projection(A, "op-open", "QUEUED", age_days=0)
        connection = sqlite3.connect(w.paths[0])
        connection.execute("UPDATE outbox SET status='delivered'")
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
        connection.commit()
        connection.close()
        made = await bw.service.create("daily")
        view = await bw.service.preview_restore(made.manifest.backup_id)
        report = await bw.service.restore(view, "decision-1")
        connection = sqlite3.connect(w.paths[0])
        left = connection.execute(
            "SELECT COUNT(*) FROM outbox WHERE status IN ('pending','delivered')"
        ).fetchone()[0]
        connection.close()
        return view, report, left

    view, report, left = run(scenario())
    assert view.non_terminal_operations == 1 and "OPERATIONS_AT_RISK" in codes(view.issues)
    assert view.pending_outbox >= 1 and report.quarantined_outbox >= 1 and left == 0
    assert not view.blocked  # the operator is told, and decides


def test_a_rehearsal_that_corrupts_the_copy_is_reported(tmp_path):
    async def scenario():
        bw = await new_bworld(tmp_path, [A])

        async def damaging(path):
            data = bytearray(path.read_bytes())
            for index in range(2048, 4096):
                data[index] ^= 0xFF
            path.write_bytes(bytes(data))

        return await bw.service.rehearse_migration(damaging)

    report = run(scenario())
    assert (
        not report.migrated
        and report.error in {"INTEGRITY_FAILED", "UNREADABLE"}
        or (not report.migrated)
    )
