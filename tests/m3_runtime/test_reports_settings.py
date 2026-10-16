"""Full reports and cross-owner settings recovery use real durable storage."""

import json
from datetime import timedelta

import pytest
from ai_agent_application.laboratory.report import Finding, ScenarioReport
from ai_agent_application.laboratory.transfer import SettingsTransfer
from ai_agent_application.persona import PersonaService
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.api import EditCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.persona import GmRuntimeState
from composition_support import run, setup

from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.schemas import ConfigureGM
from ai_agent_core.m3_composition.settings import DurableSettings, TransferJournal
from ai_agent_core.m3_composition.state import PRINCIPALS, State, extension, put, record


async def prepare_test_scope(env, *, rules_version=2):
    scope = env.world.scope.model_copy(
        update={"memory_mode": "test", "memory_space_id": "settings-test"}
    )
    await env.world.store.provision(scope, env.grants)
    now = env.world.clock()
    gm = record(
        env.world.registry,
        GmRuntimeState,
        id="gm-runtime",
        scope=scope,
        version=1,
        created_at=now,
        updated_at=now,
        intensity=0.9,
        active_seconds=0,
        rules_version=rules_version,
        rules=extension(env.world.registry, "m3.rules", {"rules": []}),
    )
    await put(env.world.store, env.world.registry, Owner.GM, scope, "seed-gm", gm, None)
    return scope


def test_full_report_is_durable_and_ttl_and_delete_close_file_access(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        scope = await prepare_test_scope(env)
        reports = Reports(
            tmp_path / "reports",
            env.world.store,
            env.world.registry,
            clock=env.world.clock,
            ttl=timedelta(hours=1),
            readers=(RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),),
        )
        now = env.world.clock()
        report = ScenarioReport(
            "synthetic",
            1,
            "seed",
            now,
            now,
            "passed",
            (Finding("STOP", True),),
            ({"kind": "synthetic", "detail": "full-report-body"},),
            0,
            source_refs=(),
        )
        ref = await reports.save(scope, report)
        stored = json.loads(await reports.read(scope, ref.artifact_id, PRINCIPALS[Owner.CATALOGUE]))
        assert stored["log"][0]["detail"] == "full-report-body" and stored["mock"] is True
        assert await reports.save(scope, report) == ref
        restarted = Reports(
            tmp_path / "reports", env.world.store, env.world.registry, clock=env.world.clock
        )
        assert (
            json.loads(await restarted.read(scope, ref.artifact_id, PRINCIPALS[Owner.CATALOGUE]))[
                "digest"
            ]
            == report.digest
        )
        env.world.clock.advance(hours=2)
        with pytest.raises(ContractError):
            await restarted.read(scope, ref.artifact_id, PRINCIPALS[Owner.CATALOGUE])
        assert restarted.sweep() == 1
        assert not list((tmp_path / "reports").glob("*.json"))
        await env.runtime.close()

    run(scenario())


def test_report_purger_matches_existing_m5_port(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        scope = await prepare_test_scope(env)
        reports = Reports(
            tmp_path / "reports",
            env.world.store,
            env.world.registry,
            clock=env.world.clock,
            readers=(RecordAccess(PRINCIPALS[Owner.CATALOGUE].id),),
        )
        now = env.world.clock()
        report = ScenarioReport(
            "delete", 1, "seed", now, now, "passed", (), ({"text": "synthetic"},), 0, source_refs=()
        )
        ref = await reports.save(scope, report)
        from ai_agent_contracts.workspace import Artifact

        artifact = await env.world.store.repository(
            Artifact, PRINCIPALS[Owner.CATALOGUE], scope
        ).get(ref.artifact_id)
        await reports.purge_sources(scope, [artifact.provenance_source.source_id])
        with pytest.raises((ContractError, OSError)):
            await reports.read(scope, ref.artifact_id, PRINCIPALS[Owner.CATALOGUE])
        assert not list((tmp_path / "reports").glob("*.json"))
        await env.runtime.close()

    run(scenario())


def test_selected_settings_transfer_persists_actual_rules_and_replays_without_second_write(
    tmp_path,
):
    async def scenario():
        env = await setup(tmp_path, start=False)
        source = await prepare_test_scope(env)
        state = env.runtime.state
        transfer = SettingsTransfer(
            PersonaService(env.world.store, PRINCIPALS[Owner.PERSONA], env.world.registry),
            DurableSettings(state),
        )
        target = env.runtime.supervisor.snapshot.scope
        preview = await transfer.preview(source, target, ["gm_intensity", "story_rules_version"])
        journal = TransferJournal(state, clock=env.world.clock)
        result = await journal.apply(
            transfer,
            source,
            target,
            ["gm_intensity", "story_rules_version"],
            "decision",
            preview.digest,
            "operator",
        )
        current = await state.gm(target)
        assert current.intensity == 0.9 and current.rules_version == 2
        before = current.version
        assert (await journal.get(target, "decision")).status == "completed"
        assert not await journal.pending(target)
        replay = await journal.apply(
            transfer,
            source,
            target,
            ["gm_intensity", "story_rules_version"],
            "decision",
            preview.digest,
            "operator",
        )
        assert replay.applied == result.applied and (await state.gm(target)).version == before
        await env.runtime.close()

    run(scenario())


def test_partial_transfer_requires_reconciliation_and_cannot_silently_repeat(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        source = await prepare_test_scope(env)
        state = env.runtime.state

        class FailingSettings(DurableSettings):
            async def set(self, scope, key, value):
                if key == "story_rules_version":
                    raise RuntimeError("synthetic second owner failure")
                await super().set(scope, key, value)

        transfer = SettingsTransfer(
            PersonaService(env.world.store, PRINCIPALS[Owner.PERSONA], env.world.registry),
            FailingSettings(state),
        )
        target = env.runtime.supervisor.snapshot.scope
        keys = ["gm_intensity", "story_rules_version"]
        preview = await transfer.preview(source, target, keys)
        journal = TransferJournal(state, clock=env.world.clock)
        with pytest.raises(RuntimeError):
            await journal.apply(
                transfer, source, target, keys, "partial", preview.digest, "operator"
            )
        row = await journal.get(target, "partial")
        assert row.status == "reconciliation_required" and row.applied_keys == ["gm_intensity"]
        assert await journal.pending(target)
        with pytest.raises(ContractError):
            await journal.apply(
                transfer, source, target, keys, "partial", preview.digest, "operator"
            )
        await journal.acknowledge(target, "partial", "operator")
        assert not await journal.pending(target)
        await env.runtime.close()

    run(scenario())


def test_gm_configuration_cas_and_durable_active_time(tmp_path):
    async def scenario():
        env = await setup(tmp_path, start=False)
        rt = env.runtime
        scope = rt.supervisor.snapshot.scope
        edit = record(
            rt.registry,
            EditCommand,
            request_id="configure",
            scope=scope,
            target="gm-runtime",
            type="gm.configure",
            expected_version=1,
            arguments=extension(rt.registry, "gm.configure-args", {"intensity": 0.3}),
        )
        first = await rt.state.configure(edit, ConfigureGM(intensity=0.3))
        assert await rt.state.configure(edit, ConfigureGM(intensity=0.3)) == first
        with pytest.raises(ContractError):
            await rt.state.configure(
                edit.model_copy(update={"request_id": "stale"}), ConfigureGM(intensity=0.8)
            )
        await rt.state.checkpoint_time(scope, 12.0)
        stored = await State(rt.store, rt.registry).gm(scope)
        assert stored.intensity == 0.3 and stored.active_seconds == 12
        await rt.close()

    run(scenario())
