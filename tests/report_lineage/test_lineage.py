"""Real host/Reports/LabRunner/retention: selective deletion and guarded reads, no live API."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ai_agent_application.laboratory.report import ScenarioReport
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import Provenance
from ai_agent_contracts.workspace import Artifact
from la_support import CATALOGUE, MEMORY, make_world, residue, run

from ai_agent_core.local_artifacts import (
    ReportBinding,
    artifact_catalogue,
    derived_purger,
    invalidate_scope_reports,
    preview_scope_reports,
    verified_roots,
)
from ai_agent_core.local_host.rights import OPERATOR, report_readers
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import command, put


def purger(world):
    return derived_purger(
        verified_roots(world.host.lab_indexes, world.host.reports),
        world.open_indexes,
        ReportBinding(world.reports, artifact_catalogue(world.store, CATALOGUE)),
    )


async def second_report(world, scope, marker):
    memory = MemoryService(world.store, MEMORY, world.registry, reader_access=report_readers())
    now = datetime.now(UTC)
    created = await memory.register_source(
        command(scope, "second-source"),
        SourceDraft(
            provenance=Provenance.SIMULATION,
            source_type="lab-event",
            adapter_id="laboratory",
            adapter_version="1",
            occurred_at=now,
            content=marker,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
            external_id="second",
        ),
    )
    ref = SourceRef(
        source_id=created.source_id, version=created.version, scope=scope, availability="available"
    )
    report = ScenarioReport(
        "second", 1, "second", now, now, "passed", (), ({"text": marker},), 0, source_refs=(ref,)
    )
    return await world.reports.save(scope, report)


def test_real_lab_source_purge_removes_only_linked_report_in_the_same_scope(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        try:
            first = await world.lab(0, "fictional-origin-alpha")
            second = await second_report(world, first.scope, "fictional-origin-beta")
            other = await world.lab(1, "fictional-origin-gamma")
            body = json.loads(await world.download(first))
            assert first.source_id in body["source_ids"]
            assert body["log"][0]["source_id"] == first.source_id
            retention = world.retention(purgers=(purger(world),))
            preview = await retention.preview_delete(first.scope, [first.source_id])
            outcome = await retention.delete(first.scope, preview)
            assert [r.state for r in outcome.outcomes] == ["purged"]
            assert not residue(world, "fictional-origin-alpha")
            with pytest.raises(ContractError):
                await world.download(first)
            assert b"fictional-origin-beta" in await world.reports.read(
                first.scope, second.artifact_id, CATALOGUE
            )
            assert b"fictional-origin-gamma" in await world.download(other)
            await purger(world).purge_sources(first.scope, [first.source_id])
            assert not residue(world, "fictional-origin-alpha")
        finally:
            await world.close()

    run(scenario())


def test_tampered_report_stays_blocked_and_repair_allows_idempotent_resume(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        try:
            first = await world.lab(0, "fictional-integrity-marker")
            path = next(
                p
                for p in world.host.reports.glob("*.json")
                if b"fictional-integrity-marker" in p.read_bytes()
            )
            original = path.read_bytes()
            value = json.loads(original)
            value["source_refs"] = []
            value["source_ids"] = []
            path.write_text(json.dumps(value), encoding="utf-8")
            with pytest.raises(ContractError):
                await world.download(first)
            retention = world.retention(purgers=(purger(world),))
            preview = await retention.preview_delete(first.scope, [first.source_id])
            result = await retention.delete(first.scope, preview)
            assert [r.state for r in result.outcomes] == ["blocked_pending_purge"]
            path.write_bytes(original)
            done = await retention.resume(first.scope)
            assert [r.state for r in done.outcomes] == ["purged"]
            assert not residue(world, "fictional-integrity-marker")
        finally:
            await world.close()

    run(scenario())


def test_revocation_between_io_guards_closes_download_and_restart_does_not_reopen_it(
    tmp_path, monkeypatch
):
    async def scenario():
        world = await make_world(tmp_path)
        try:
            first = await world.lab(0, "fictional-permission-marker")
            original = world.reports._check_sources
            checks = 0

            async def checked(scope, refs, principal, purpose="private"):
                nonlocal checks
                await original(scope, refs, principal, purpose)
                checks += 1
                if checks == 1:
                    source = await world.store.repository(Source, MEMORY, scope).get(
                        first.source_id
                    )
                    try:
                        await MemoryService(
                            world.store, MEMORY, world.registry, clock=lambda: source.updated_at
                        ).set_rights(
                            command(source.scope, "revoke-original"),
                            first.source_id,
                            source.version,
                            read_allowed=True,
                            api_transfer_allowed=False,
                            public_disclosure_allowed=False,
                        )
                    except ContractError as error:
                        raise AssertionError(
                            "REVOCATION_FIXTURE_FAILED:" + error.error.code.value
                        ) from error
                    again = await world.store.repository(Source, MEMORY, scope).get(first.source_id)
                    assert again.version == source.version + 1

            monkeypatch.setattr(world.reports, "_check_sources", checked)
            with pytest.raises(ContractError):
                await world.reports.read(
                    first.scope, first.artifact.artifact_id, OPERATOR, purpose="api_transfer"
                )
            current = await world.store.repository(Source, MEMORY, first.scope).get(first.source_id)
            assert current.version == 2 and current.api_transfer_allowed is False
            assert checks > 0, "The read must reach the original-source guard"
            restarted = Reports(world.host.reports, world.store, world.registry)
            with pytest.raises(ContractError):
                await restarted.read(
                    first.scope, first.artifact.artifact_id, OPERATOR, purpose="api_transfer"
                )
        finally:
            await world.close()

    run(scenario())


def test_legacy_lineage_is_unknown_not_an_empty_list_and_new_saves_require_it(tmp_path):
    async def scenario():
        world = await make_world(tmp_path)
        try:
            now = datetime.now(UTC)
            report = ScenarioReport("legacy", 1, "s", now, now, "passed", (), (), 0)
            assert "source_ids" not in report.as_dict()
            with pytest.raises(ContractError):
                await world.reports.save(world.scope(0), report)
            with pytest.raises(ContractError):
                world.reports._references(world.scope(0), report.to_json().encode())
            first = await world.lab(0, "fictional-legacy-dependent")
            scope = first.scope
            report.source_refs = ()
            reference = await world.reports.save(scope, report)
            sid = derived_id("labsource", scope, report.digest)
            path = world.reports._path(scope, sid)
            legacy = json.loads(path.read_bytes())
            for key in ("source_refs", "source_ids", "scope"):
                legacy.pop(key, None)
            body = json.dumps(legacy, ensure_ascii=False).encode()
            reference = reference.model_copy(
                update=dict(
                    version=2, checksum=hashlib.sha256(body).hexdigest(), size_bytes=len(body)
                )
            )
            source = await world.store.repository(Source, MEMORY, scope).get(sid)
            await put(
                world.store,
                world.registry,
                Owner.MEMORY,
                scope,
                "historical-source",
                source.model_copy(update=dict(version=2, artifact=reference)),
                1,
            )
            artifact = await world.store.repository(Artifact, CATALOGUE, scope).get(
                reference.artifact_id
            )
            await put(
                world.store,
                world.registry,
                Owner.CATALOGUE,
                scope,
                "historical-artifact",
                artifact.model_copy(
                    update=dict(
                        version=2,
                        reference=reference,
                        provenance_source=artifact.provenance_source.model_copy(
                            update=dict(version=2)
                        ),
                    )
                ),
                1,
            )
            path.write_bytes(body)  # A historical body with its authoritative matching checksum.
            retention = world.retention(purgers=(purger(world),))
            preview = await retention.preview_delete(scope, [first.source_id])
            outcome = await retention.delete(scope, preview)
            assert [o.state for o in outcome.outcomes] == ["blocked_pending_purge"]
            assert path.exists()
            catalogue = artifact_catalogue(world.store, CATALOGUE)
            decision = await preview_scope_reports(scope, catalogue)
            await invalidate_scope_reports(
                decision, "fictional-operator-decision", world.reports, catalogue
            )
            done = await retention.resume(scope)
            assert [o.state for o in done.outcomes] == ["purged"]
            assert not residue(world, "fictional-legacy-dependent")
        finally:
            await world.close()

    run(scenario())


def test_inventory_stat_error_is_not_evidence_that_a_body_is_absent(tmp_path, monkeypatch):
    reports = Reports(tmp_path, object(), object())
    from ai_agent_contracts.common import Scope

    scope = Scope(
        workspace_id="synthetic", persona_id="alpha", memory_space_id="test", memory_mode="test"
    )
    target = reports._path(scope, "report-source")
    original = Path.lstat

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("synthetic")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(ContractError):
        reports.holds(scope, "report-source")
