"""Offline preparation of derived directories after a restore: previewed, decided, honest."""

import contextlib
import hashlib
import io
from pathlib import Path

import pytest
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_contracts.codec import ContractError
from la_support import CATALOGUE, FileHolds, make_world, residue, run
from test_purger import delete, tree

from ai_agent_core.local_artifacts import (
    ArtifactsError,
    apply_restore,
    plan_restore,
    verified_roots,
)
from ai_agent_core.local_artifacts.cli import main
from ai_agent_core.local_host.process import exclusive_host
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.artifacts import Reports

A = "Лазурный-якорь-Т7"
B = "Серебряный-маяк-Т9"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def stale_world(tmp_path):
    """Two lab areas; A's source is deleted while no derived purger is wired: files stay stale."""
    world = await make_world(tmp_path)
    first, second = await world.lab(0, A), await world.lab(1, B)
    await delete(world, first, ())
    world.close_indexes()  # a restore is offline: nothing holds the index files
    (world.host.reports / "keep.txt").write_text("neighbour file")
    await world.store.close()
    scopes = [world.work, *(world.scope(n) for n in range(3))]
    roots = verified_roots(world.host.lab_indexes, world.host.reports)
    return world, first, second, roots, scopes


def test_preview_then_decided_apply_cleans_indexes_and_shows_the_report_loss(tmp_path):
    async def scenario():
        world, first, second, roots, scopes = await stale_world(tmp_path)
        out = {"db": digest(world.host.database), "secrets": tree(world.host.secrets)}
        out["stale"] = [n for n in residue(world, A) if not n.endswith(".json")]
        plan = await plan_restore(roots, world.ledger, scopes)
        out["plan"] = (
            [f.blocked_sources for f in plan.indexes],
            plan.ledger_sources,
            plan.unproven_reports,
            plan.blocked,
        )
        reports = FileHolds(
            Reports(roots.reports, SQLiteStore(world.host.database), new_registry())
        )
        for decision, expected in (("", "DECISION_REQUIRED"), ("bad id", "DECISION_REQUIRED")):
            with pytest.raises(ArtifactsError) as info:
                await apply_restore(roots, world.ledger, scopes, plan, decision, reports=reports)
            assert info.value.code == expected
        result = await apply_restore(roots, world.ledger, scopes, plan, "dec-1", reports=reports)
        out["result"] = (result.indexes_cleaned, result.sources_removed, result.reports_dropped)
        out["indexes_after"] = [n for n in residue(world, A) if not n.endswith(".json")]
        out["b_index"] = [n for n in residue(world, B) if not n.endswith(".json")]
        out["body_a"] = [n for n in residue(world, A) if n.endswith(".json")]  # honest: still there
        wide = await plan_restore(roots, world.ledger, scopes, drop_reports=True)
        out["wide"] = (wide.report_files, wide.unproven_reports, wide.drop_reports)
        dropped = await apply_restore(roots, world.ledger, scopes, wide, "dec-2", reports=reports)
        out["dropped"] = dropped.reports_dropped
        out["reports_left"] = sorted(p.name for p in world.host.reports.iterdir())
        out["b_index_kept"] = [n for n in residue(world, B) if not n.endswith(".json")]
        out["db_after"] = digest(world.host.database)
        out["secrets_after"] = tree(world.host.secrets)
        again = await plan_restore(roots, world.ledger, scopes, drop_reports=True)
        out["idempotent"] = await apply_restore(
            roots, world.ledger, scopes, again, "dec-3", reports=reports
        )
        store = SQLiteStore(world.host.database, extensions=new_registry())
        try:
            catalogue = Reports(roots.reports, store, new_registry())
            with pytest.raises(ContractError):  # the catalogue record stays; the body is gone
                await catalogue.read(first.scope, first.artifact.artifact_id, CATALOGUE)
        finally:
            await store.close()
        return out

    out = run(scenario())
    assert out["stale"]  # before: the deleted text is still in an index file
    blocked, ledger_sources, unproven, plan_blocked = out["plan"]
    assert sorted(blocked) == [0, 1] and ledger_sources == 1 and unproven == 2 and not plan_blocked
    assert out["result"] == (1, 1, 0)
    assert out["indexes_after"] == [] and out["b_index"]
    assert out["body_a"], "a report body cannot be proven clean without a relation: it stays"
    assert out["wide"][1:] == (0, True) and out["wide"][0] == 4
    assert out["dropped"] == 4 and out["reports_left"] == ["keep.txt"]
    assert out["b_index_kept"]  # another area's index text is never touched
    assert out["db_after"] == out["db"] and out["secrets_after"] == out["secrets"]
    assert out["idempotent"].reports_dropped == 0


def test_a_changed_state_an_open_index_and_a_foreign_file_stop_the_apply(tmp_path):
    async def scenario():
        world, first, second, roots, scopes = await stale_world(tmp_path)
        plan = await plan_restore(roots, world.ledger, scopes)
        (world.host.reports / "late.json").write_text("{}")  # the state moved after the preview
        with pytest.raises(ArtifactsError) as changed:
            await apply_restore(roots, world.ledger, scopes, plan, "dec-1")
        index = next(roots.indexes.glob("*.sqlite"))
        held = await plan_restore(roots, world.ledger, scopes, open_handles=[index])
        with pytest.raises(ArtifactsError) as opened:
            await apply_restore(roots, world.ledger, scopes, held, "dec-1", open_handles=[index])
        sqlite_file = roots.indexes / "foreign.sqlite"
        sqlite_file.write_bytes(b"")
        foreign = await plan_restore(roots, world.ledger, scopes)
        with pytest.raises(ArtifactsError) as unknown:
            await apply_restore(roots, world.ledger, scopes, foreign, "dec-1")
        return (
            changed.value.code,
            held.open_conflict,
            opened.value.code,
            foreign.blocked,
            (
                unknown.value.code,
                [n for n in residue(world, A) if not n.endswith(".json")],
            ),
        )

    changed, conflict, opened, foreign_blocked, (unknown, still) = run(scenario())
    assert changed == "PREVIEW_CHANGED" and conflict and opened == "INDEX_OPEN"
    assert foreign_blocked and unknown == "UNRECOGNISED_INDEX_FILE"
    assert still  # nothing ran: the stale text is untouched until a safe plan is decided


def run_cli(*arguments: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(arguments))
    return code, out.getvalue(), err.getvalue()


def test_cli_needs_the_host_lock_an_existing_ledger_and_the_previewed_digest(tmp_path):
    async def scenario():
        world, *_ = await stale_world(tmp_path)
        world.ledger.close()
        return world

    world = run(scenario())
    base = ["--root", str(world.root), "--ledger"]
    ledger = tmp_path / "ledger" / "l.sqlite"
    db, secrets = digest(world.host.database), tree(world.host.secrets)

    code, out, err = run_cli("preview", *base, str(ledger))
    assert code == 0 and "index: recognised blocked_sources=1" in out
    assert "unproven" not in out and A not in out + err and str(tmp_path) not in out + err
    previewed = next(line.split()[1] for line in out.splitlines() if line.startswith("digest:"))

    with exclusive_host(world.root):  # a running serve holds this lock
        code, _, err = run_cli("preview", *base, str(ledger))
    assert code != 0 and "HOST_ALREADY_RUNNING" in err

    missing = tmp_path / "ledger" / "gone.sqlite"
    code, _, err = run_cli("preview", *base, str(missing))
    assert code != 0 and "LEDGER_UNAVAILABLE" in err and not missing.exists()
    empty = tmp_path / "ledger" / "empty.sqlite"
    empty.write_bytes(b"")
    code, _, err = run_cli("preview", *base, str(empty))
    assert code != 0 and "LEDGER_UNAVAILABLE" in err and empty.stat().st_size == 0

    code, _, err = run_cli(
        "apply", *base, str(ledger), "--decision-id", "dec-1", "--digest", "0" * 64
    )
    assert code != 0 and "PREVIEW_CHANGED" in err
    code, out, err = run_cli(
        "apply", *base, str(ledger), "--decision-id", "dec-1", "--digest", previewed
    )
    assert code == 0, err
    assert "indexes_cleaned=1 sources_removed=1" in out and A not in out
    assert digest(world.host.database) == db and tree(world.host.secrets) == secrets
    leftovers = [p.name for p in world.host.lab_indexes.glob("*.sqlite")]
    assert leftovers  # indexes are cleaned in place, never unlinked
