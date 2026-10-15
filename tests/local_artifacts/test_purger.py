"""DerivedPurger on real lab indexes and reports, driven by the real RetentionService."""

import hashlib
import sqlite3
from pathlib import Path

from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.workspace import Artifact
from la_support import CATALOGUE, FileHolds, make_world, residue, run

from ai_agent_core.local_artifacts import (
    ArtifactsError,
    ReportBinding,
    artifact_catalogue,
    derived_purger,
    invalidate_scope_reports,
    preview_scope_reports,
    verified_roots,
)

A = "Лазурный-якорь-Т7"
B = "Серебряный-маяк-Т9"


def tree(base: Path) -> str:
    digest = hashlib.sha256()
    for item in sorted(base.rglob("*")):
        digest.update(str(item.relative_to(base)).encode())
        if item.is_file():
            digest.update(item.read_bytes())
    return digest.hexdigest()


def report_files(world):
    return sorted(p.name for p in world.host.reports.glob("*.json"))


async def delete(world, lab, purgers):
    retention = world.retention(purgers=purgers)
    preview = await retention.preview_delete(lab.scope, [lab.source_id])
    return retention, await retention.delete(lab.scope, preview)


def test_unlinked_report_body_keeps_the_source_blocked_until_a_decided_invalidation(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        first, second = await world.lab(0, A), await world.lab(1, B)
        (world.host.reports / "keep.txt").write_text("neighbour file")
        secrets = tree(world.host.secrets)
        assert A.encode() in await world.download(first)
        retention, outcome = await delete(world, first, (world.purger(),))
        steps = {
            "outcome": [(o.state, o.failed_step) for o in outcome.outcomes],
            "after_delete": residue(world, A),
        }
        stub = await world.retention(purgers=()).physical.source(first.scope, first.source_id)
        steps["stub_has_text"] = stub.has_content  # not erased: nothing was claimed purged
        again = await retention.resume(first.scope)  # idempotent: still honest
        steps["resume_state"] = [o.state for o in again.outcomes]

        catalogue = artifact_catalogue(world.store, CATALOGUE)
        preview = await preview_scope_reports(first.scope, catalogue)
        steps["preview"] = len(preview.source_ids)
        files = FileHolds(world.reports)
        await invalidate_scope_reports(preview, "decision-1", files, catalogue)
        done = await retention.resume(first.scope)
        steps["final"] = [o.state for o in done.outcomes]
        steps["residue"] = residue(world, A)
        try:
            await world.download(first)
            steps["download"] = "readable"
        except ContractError:
            steps["download"] = "closed"
        steps["other_kept"] = (residue(world, B), B.encode() in await world.download(second))
        steps["other_recall"] = bool(await world.hears(second, "костра"))
        steps["neighbours"] = (
            (world.host.reports / "keep.txt").read_text() == "neighbour file",
            tree(world.host.secrets) == secrets,
        )
        await world.close()
        return steps

    steps = run(scenario())
    assert steps["outcome"] == [("blocked_pending_purge", "derived")]
    assert (
        len(steps["after_delete"]) == 1
    )  # the indexes were cleaned; only the report body holds it
    assert steps["after_delete"][0].endswith(".json") and steps["stub_has_text"]
    assert steps["resume_state"] == ["blocked_pending_purge"] and steps["preview"] == 1
    assert steps["final"] == ["purged"] and steps["residue"] == []
    assert steps["download"] == "closed"
    other, readable = steps["other_kept"]
    assert other and readable and steps["other_recall"]  # another area keeps indexes and report
    assert steps["neighbours"] == (True, True)


def test_a_closed_index_file_is_cleaned_in_place_and_never_unlinked(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        first = await world.lab(0, A)
        await world.lab(1, B)
        world.close_indexes()  # a restart: the files are on disk, no one holds them
        before = sorted(p.name for p in world.host.lab_indexes.iterdir())
        purger = derived_purger(
            verified_roots(world.host.lab_indexes, world.host.reports), world.open_indexes
        )
        await purger.purge_sources(first.scope, [first.source_id])
        await purger.purge_sources(first.scope, [first.source_id])  # idempotent
        await purger.purge_sources(first.scope, ["no-such-source"])
        after = sorted(p.name for p in world.host.lab_indexes.iterdir())
        found = [n for n in residue(world, A) if not n.endswith(".json")]
        kept = [n for n in residue(world, B) if not n.endswith(".json")]
        await world.close()
        return before, after, found, kept

    before, after, found, kept = run(scenario())
    assert found == [] and kept  # the other area's index text is untouched
    assert [n for n in after if not n.endswith(("-wal", "-shm"))] == [
        n for n in before if not n.endswith(("-wal", "-shm"))
    ]


def test_a_busy_or_closed_index_and_a_failing_file_operation_are_not_called_purged(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        first = await world.lab(0, A)
        roots = verified_roots(world.host.lab_indexes, world.host.reports)
        outcome: dict = {}

        # an index the owner closed but still lists
        index = next(iter(world.opened.values()))
        index.close()
        purger = derived_purger(roots, world.open_indexes)
        try:
            await purger.purge_sources(first.scope, [first.source_id])
        except ArtifactsError as error:
            outcome["closed"] = (error.code, error.reasons)
        world.opened.clear()

        # a closed file that another connection holds under an exclusive lock
        path = next(roots.indexes.glob("*.sqlite"))
        locker = sqlite3.connect(path, isolation_level=None)
        locker.execute("BEGIN EXCLUSIVE")
        try:
            await purger.purge_sources(first.scope, [first.source_id])
        except ArtifactsError as error:
            outcome["busy"] = error.reasons
        locker.execute("ROLLBACK")
        locker.close()
        outcome["marker_while_busy"] = bool(residue(world, A))

        # a refused file operation on a report body
        class Refusing(FileHolds):
            async def purge(self, scope, source_id):
                raise PermissionError("denied")

        binding = ReportBinding(Refusing(world.reports), artifact_catalogue(world.store, CATALOGUE))
        failing = derived_purger(roots, world.open_indexes, binding)
        try:
            await failing.purge_sources(first.scope, [first.source_id])
        except ArtifactsError as error:
            outcome["refused"] = error.reasons
        outcome["retry"] = "ok"
        await world.close()
        return outcome

    outcome = run(scenario())
    assert outcome["closed"][0] == "PURGE_INCOMPLETE" and "ProgrammingError" in outcome["closed"][1]
    assert "INDEX_BUSY" in outcome["busy"] and outcome["marker_while_busy"]
    assert "PermissionError" in outcome["refused"]
    assert "denied" not in repr(outcome)  # fixed codes only


def test_a_retry_after_a_transient_failure_completes_the_purge(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        first = await world.lab(0, A)
        roots = verified_roots(world.host.lab_indexes, world.host.reports)
        calls = {"n": 0}

        class Flaky(FileHolds):
            async def purge(self, scope, source_id):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise OSError("disk")
                await super().purge(scope, source_id)

        catalogue = artifact_catalogue(world.store, CATALOGUE)
        purger = derived_purger(
            roots, world.open_indexes, ReportBinding(Flaky(world.reports), catalogue)
        )
        retention = world.retention(purgers=(purger,))
        preview = await retention.preview_delete(first.scope, [first.source_id])
        first_try = await retention.delete(first.scope, preview)
        # invalidate the unlinked body as the operator would, then retry
        decided = await preview_scope_reports(first.scope, catalogue)
        await invalidate_scope_reports(decided, "d-1", FileHolds(world.reports), catalogue)
        retry = await retention.resume(first.scope)
        left = residue(world, A)
        await world.close()
        return first_try, retry, left

    first_try, retry, left = run(scenario())
    assert not first_try.complete and retry.complete and left == []


def test_links_foreign_files_and_unsafe_roots_are_refused_without_touching_outside(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        first = await world.lab(0, A)
        world.close_indexes()
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "precious.sqlite"
        target.write_bytes(b"not an index")
        before = target.read_bytes()
        secrets = tree(world.host.secrets)
        roots = verified_roots(world.host.lab_indexes, world.host.reports)
        purger = derived_purger(roots, world.open_indexes)

        link = world.host.lab_indexes / "link.sqlite"
        link.symlink_to(target)
        refused = None
        try:
            await purger.purge_sources(first.scope, [first.source_id])
        except ArtifactsError as error:
            refused = error.code
        link.unlink()

        foreign = world.host.lab_indexes / "foreign.sqlite"
        sqlite3.connect(foreign).execute("CREATE TABLE t(x)").connection.close()
        tables_before = (
            sqlite3.connect(foreign).execute("SELECT name FROM sqlite_master").fetchall()
        )
        reasons = ()
        try:
            await purger.purge_sources(first.scope, [first.source_id])
        except ArtifactsError as error:
            reasons = error.reasons
        tables_after = sqlite3.connect(foreign).execute("SELECT name FROM sqlite_master").fetchall()

        bad = {}
        for name, root in (("relative", Path("rel")), ("link", tmp_path / "rootlink")):
            if name == "link":
                root.symlink_to(world.host.lab_indexes)
            try:
                verified_roots(root, world.host.reports)
            except ArtifactsError as error:
                bad[name] = error.code
        inner = world.host.lab_indexes / "inner"
        inner.mkdir()
        try:
            verified_roots(world.host.lab_indexes, inner)
        except ArtifactsError as error:
            bad["overlap"] = error.code
        out = (refused, reasons, tables_before == tables_after, target.read_bytes() == before)
        out += (tree(world.host.secrets) == secrets, bad)
        await world.close()
        return out

    refused, reasons, untouched, outside, secrets, bad = run(scenario())
    assert refused == "DERIVED_PATH_UNSAFE" and outside
    assert "UNRECOGNISED_INDEX_FILE" in reasons and untouched  # a foreign file gains no tables
    assert secrets
    assert bad == {
        "relative": "ABSOLUTE_PATH_REQUIRED",
        "link": "DERIVED_ROOT_UNSAFE",
        "overlap": "DERIVED_ROOTS_OVERLAP",
    }


def test_exact_report_id_removes_the_body_and_its_marker(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        first = await world.lab(0, A)
        artifact = await world.store.repository(Artifact, CATALOGUE, first.scope).get(
            first.artifact.artifact_id
        )
        own = artifact.provenance_source.source_id
        binding = ReportBinding(
            FileHolds(world.reports), artifact_catalogue(world.store, CATALOGUE)
        )
        purger = derived_purger(
            verified_roots(world.host.lab_indexes, world.host.reports), world.open_indexes, binding
        )
        await purger.purge_sources(first.scope, [own])  # the report's own source: exact relation
        left = sorted(p.name for p in world.host.reports.iterdir())
        await world.close()
        return left

    assert run(scenario()) == []
