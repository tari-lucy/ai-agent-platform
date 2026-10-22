"""Production backup/staging/switch/finish/startup, synthetic provider and data only."""

import asyncio
from pathlib import Path

import pytest
from ai_agent_adapters.backup import SQLiteRestoreTarget
from ai_agent_adapters.workspace_files.bundle import BUNDLE_NAME
from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_sessions import WorkSession
from ai_agent_contracts.agent_workplace import WorkTask
from ai_agent_contracts.memory import Source
from lb_support import cli, copy_id, digest_of
from session_support import ack, install
from test_activity import WorkModel, assign, fast, prepared, until
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.finish_restore import finish_restore
from ai_agent_core.local_host.layout import HostError, load_config, paths
from ai_agent_core.local_host.recovery import HostLedger, enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.state import PRINCIPALS
from ai_agent_core.m3_composition.work_session_workspace import Workspace

MARKER = "SyntheticRestoredLighthouseSecret"


def test_workplace_staged_restore_redacts_history_and_blobs_before_switch(
    tmp_path, monkeypatch, capsys
):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root,
            store_dir=tmp_path / "backups",
            ledger_path=tmp_path / "ledger/history.sqlite",
        )
        await enable_workplace_files(host.root)
        host_paths = paths(host.root)
        config = load_config(host_paths)
        args = [
            "--root",
            str(host.root),
            "--store",
            config.recovery.store_dir,
            "--ledger",
            config.recovery.ledger_path,
        ]
        failures = []

        async with host.open():
            rt = host.runtime
            fast(rt)
            await install(rt, host.scope, areas=["skills"])
            original = rt.session_harness

            async def factory(ex, gateway, gate):
                harness = await original(ex, gateway, gate)
                turn = harness.turn

                async def create(admission, instruction):
                    try:
                        bridge = Workspace(ex, gate)
                        await bridge.call(
                            gate,
                            WorkspaceRequest(
                                "method",
                                "save_skill",
                                dict(
                                    skill_id="restored-method",
                                    title="Synthetic method",
                                    applicability=MARKER,
                                    method=MARKER,
                                ),
                            ),
                        )
                        await bridge.call(
                            gate,
                            WorkspaceRequest(
                                "ticket",
                                "raise_ticket",
                                dict(
                                    title="Synthetic restore question",
                                    body=MARKER,
                                    capability_id="synthetic.missing",
                                ),
                            ),
                        )
                    except Exception as error:
                        failures.append(error)
                        raise
                    return await turn(admission, instruction)

                harness.turn = create
                return harness

            rt.session_harness = factory
            identifier = (await open_session(host, "restore-conversation")).json()["resource_id"]
            assert (await say(host, "restore-input", identifier, 1, MARKER)).status_code == 200
            model.queues["activity"].append({"reply": "Synthetic restore draft"})
            await host.start()

            async def view():
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            draft = await until(
                view, lambda v: v.get("pending_reply") is not None, "draft", seconds=60
            )
            active = rt._active
            assert active is not None
            await active
            run = rt.supervisor.snapshot
            response = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                headers=host.env.headers,
                json=dict(
                    type="agent.stop",
                    request_id="restore-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
            )
            assert response.status_code == 200
            kept_task = await assign(host, "independent-goal", "Independent synthetic goal")
            session = await rt.store.repository(
                WorkSession, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).get(identifier)
            source = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            doomed = next(s for s in source if s.source_type == "work-session-message")
            skill = await rt.workplace.skills.get(host.scope, "restored-method")
            assert skill is not None
            file_id = skill.method.artifact_id
            generation = session.generation

        await rt.store.close()  # in-process ASGI fixture; real serve exits its process
        code, out, err = await asyncio.to_thread(cli, "create", *args)
        assert code == 0, (out, err)
        backup_id = copy_id(out)
        copy_dir = Path(config.recovery.store_dir) / backup_id
        bundle = copy_dir / BUNDLE_NAME
        assert bundle.is_file()
        before = host_paths.database.read_bytes()

        async def restore(digest=None):
            code, preview, err = await asyncio.to_thread(
                cli, "preview-restore", *args, "--backup-id", backup_id
            )
            assert code == 0, (preview, err)
            return await asyncio.to_thread(
                cli,
                "restore",
                *args,
                "--backup-id",
                backup_id,
                "--decision-id",
                "synthetic-restore",
                "--digest",
                digest or digest_of(preview),
            )

        # A stage failure changes neither active DB nor blobs and cannot admit serve.
        async def fail_stage(*unused):
            raise OSError("synthetic-stage-failure")

        with monkeypatch.context() as patch:
            patch.setattr(SQLiteRestoreTarget, "rebuild_index", fail_stage)
            code, _, _ = await restore()
            assert code != 0 and host_paths.database.read_bytes() == before
            assert not (host.root / "RESTORE-PENDING").exists()

        code, _, err = await restore()
        assert code == 0, err
        assert (host.root / "RESTORE-PENDING").is_file()
        with pytest.raises(HostError, match="RESTORE_INCOMPLETE"):
            async with host.open():
                pytest.fail("pending restore must not admit serve")

        async def finish():
            await finish_restore(host.root)
            preview = capsys.readouterr().out
            await finish_restore(host.root, digest=digest_of(preview), decision_id="finish-restore")

        await finish()
        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count("activity") == 1
            restored = await rt.store.repository(
                WorkSession, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).get(identifier)
            assert restored.generation == generation + 1
            assert restored.pending_reply is None and restored.checkpoint is None
            assert not restored.context_sources and not restored.context_files
            assert (await ack(host, draft["pending_reply"], "restored-old-ack")).status_code != 200
            assert (await rt.workplace.skills.get(host.scope, "restored-method")) is not None
            _, body = await rt.workplace.files.released_content(host.scope, file_id, "api_transfer")
            assert body.decode() == MARKER

        await rt.store.close()
        # Same old copy, but deletion after its snapshot: old preview must become stale.
        code, preview, err = await asyncio.to_thread(
            cli, "preview-restore", *args, "--backup-id", backup_id
        )
        assert code == 0, err
        ledger = HostLedger(host_paths, load_config(host_paths))
        try:
            from datetime import UTC, datetime

            await ledger.block(
                host.scope, doomed.source_id, doomed.version, "operator_delete", datetime.now(UTC)
            )
        finally:
            ledger.close()
        code, _, err = await restore(digest_of(preview))
        assert code != 0 and "PREVIEW_CHANGED" in err
        code, _, err = await restore()
        assert code == 0, err
        await finish()
        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            assert model.count("activity") == 1
            assert await rt.workplace.skills.get(host.scope, "restored-method") is None
            assert (
                await rt.store.repository(WorkTask, PRINCIPALS[Owner.ACTIVITY], host.scope).get(
                    kept_task
                )
                is not None
            )
        await rt.store.close()
        active_paths = [host_paths.database, host_paths.dialogue_index]
        active_paths += list(Path(config.workplace_files.blob_dir).rglob("*"))
        for candidate in active_paths:
            if candidate.is_file():
                assert MARKER.encode() not in candidate.read_bytes(), candidate.name

    asyncio.run(scenario())
