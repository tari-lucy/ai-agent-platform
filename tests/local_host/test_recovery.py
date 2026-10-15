"""Real prepared host, anchored ledger and daily snapshots. Synthetic data, no network API."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from ai_agent_adapters.backup import DirectoryBackupStore
from ai_agent_contracts.states import RunState
from lb_support import make_host

from ai_agent_core.local_host.layout import HostError, load_config
from ai_agent_core.local_host.recovery import HostLedger, enable_recovery
from ai_agent_core.local_host.serve import build


async def prepared(base):
    host = await make_host(base, retention=False, ledger=False)
    await enable_recovery(host.root, store_dir=host.store_dir, ledger_path=host.ledger)
    host.config = load_config(host.paths)
    return host


def test_prepared_host_takes_one_daily_copy_while_stopped_and_restarts_stopped(tmp_path):
    async def scenario():
        host = await prepared(tmp_path)
        for _restart in range(2):
            (host.root / "maintenance.json").unlink(missing_ok=True)
            app, close = build(host.paths, host.config, provider="mock")
            try:
                async with app.router.lifespan_context(app):
                    rt = app.state.runtime
                    assert rt.supervisor.snapshot.state == RunState.STOPPED
                    maintenance = rt.maintenance

                    async def ready():
                        status_file = host.root / "maintenance.json"
                        for _ in range(200):
                            if status_file.is_file():
                                status = json.loads(status_file.read_text())
                                if status["status"] == "READY":
                                    return
                                raise AssertionError(status)
                            await asyncio.sleep(0.05)
                        raise AssertionError("daily maintenance did not finish")

                    await asyncio.wait_for(ready(), 15)
                    maintenance.stop.set()
                    await maintenance.task
                    copies = await DirectoryBackupStore(host.store_dir).manifests()
                    assert len([m for m in copies if m.kind == "daily"]) == 1
                    assert copies[0].ledger_id == host.config.recovery.ledger_id
                    status = json.loads((host.root / "maintenance.json").read_text())
                    assert status["status"] == "READY"
                    assert rt.supervisor.snapshot.state == RunState.STOPPED
            finally:
                await app.state.runtime.store.close()
                close()
        return host

    host = asyncio.run(scenario())
    assert not list(host.store_dir.rglob("operator-password*"))
    assert not list(host.store_dir.rglob(".env"))


def test_ledger_rollback_and_lost_file_are_never_recreated(tmp_path):
    async def scenario():
        host = await prepared(tmp_path)
        ledger = HostLedger(host.paths, host.config)
        before = tmp_path / "old-ledger.sqlite"
        await ledger.snapshot_to(before)
        await ledger.block(host.scope, "synthetic-source", 1, "operator_delete", datetime.now(UTC))
        assert load_config(host.paths).recovery.ledger_floor == 1
        ledger.close()
        # Restore an older image with the SAME identity, not just an unrelated empty file.
        with (
            closing(sqlite3.connect(before)) as old,
            closing(sqlite3.connect(host.ledger)) as target,
        ):
            old.backup(target)
        rolled = HostLedger(host.paths, load_config(host.paths))
        try:
            with pytest.raises(HostError, match="LEDGER_ROLLED_BACK"):
                await rolled.identity()
        finally:
            rolled.close()
        host.ledger.unlink()
        with pytest.raises(HostError):
            HostLedger(host.paths, load_config(host.paths))
        assert not host.ledger.exists()

    asyncio.run(scenario())


def test_failed_ledger_write_reserves_floor_and_faults_closed(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path)
        failures = []
        ledger = HostLedger(host.paths, host.config, fail=lambda: failures.append(True))

        async def broken(*args):
            raise OSError("synthetic failure; must not enter diagnostics")

        monkeypatch.setattr(ledger.raw, "block", broken)
        try:
            with pytest.raises(OSError):
                await ledger.block(host.scope, "synthetic-source", 1, "expired", datetime.now(UTC))
            assert failures and load_config(host.paths).recovery.ledger_floor == 1
            with pytest.raises(HostError):
                await ledger.identity()
        finally:
            ledger.close()

    asyncio.run(scenario())


def test_recovery_preparation_refuses_reuse_and_interruption(tmp_path):
    async def scenario():
        host = await prepared(tmp_path)
        with pytest.raises(HostError, match="RECOVERY_ALREADY_ENABLED"):
            await enable_recovery(host.root, store_dir=host.store_dir, ledger_path=host.ledger)
        (host.root / "RECOVERY-ENABLE-PENDING").write_text("synthetic marker")
        with pytest.raises(HostError, match="RECOVERY_ENABLE_INCOMPLETE"):
            load_config(host.paths)

    asyncio.run(scenario())


def test_restore_requires_completion_and_reconciles_real_owner_jobs(tmp_path, capsys):
    from ai_agent_application.control.ports import ControlAuthority
    from ai_agent_contracts.api import ControlArguments, ControlCommand, ServiceClaimCommand
    from ai_agent_contracts.states import OperationState
    from lb_support import Live, cli, copy_id, digest_of

    from ai_agent_core.local_host.finish_restore import finish_restore
    from ai_agent_core.m3_composition.schemas import TaskInput

    async def scenario():
        host = await prepared(tmp_path)
        live = Live(host)
        from ai_agent_core.local_host.rights import record_readers

        live.memory.reader_access = record_readers()
        source_id = await live.add("Полностью вымышленный маркер-парус.", "synthetic-kept")
        await live.close()
        app, close = build(host.paths, host.config, provider="mock")
        rt = app.state.runtime
        await rt.open()
        rt.maintenance.stop.set()
        await rt.maintenance.task
        authority = ControlAuthority(
            "operator", frozenset({host.scope.partition}), rt.supervisor.clock.elapsed() + 600
        )
        await rt.supervisor.control(
            authority,
            ControlCommand(
                type="agent.start",
                request_id="start-fixture",
                target="primary",
                scope=rt.supervisor.snapshot.scope,
                expected_version=rt.supervisor.snapshot.version,
                arguments=ControlArguments(autonomy="autonomous"),
            ),
        )
        request = TaskInput(kind="dialogue", trigger_id="synthetic", source_ids=[source_id])
        running = await rt.submit(authority, "running-fixture", request)
        queued = await rt.submit(authority, "queued-fixture", request)
        claim = await rt.jobs.claim(
            rt.worker,
            ServiceClaimCommand(
                request_id="claim-fixture",
                type="job.claim",
                scope=running.scope,
                target=running.id,
                operation_id=running.operation_id,
                expected_version=running.version,
                freshness=running.freshness,
            ),
        )
        assert claim and claim.acquired
        code, out, err = await asyncio.to_thread(cli, "create", *host.args())
        assert code == 0, err
        backup_id = copy_id(out)
        later = await rt.submit(authority, "after-snapshot-fixture", request)
        from ai_agent_core.local_backup.compat import HostCompatibility

        manifest = (await DirectoryBackupStore(host.store_dir).manifests())[0]
        problems = await HostCompatibility(host.paths, load_config(host.paths))(
            manifest, host.store_dir / backup_id
        )
        assert "LIVE_OPERATIONS_WOULD_BE_LOST" in {issue.code for issue in problems}
        from ai_agent_contracts.api import EditCommand

        from ai_agent_core.m3_composition.state import extension, record

        await rt.jobs.cancel(
            authority,
            record(
                rt.registry,
                EditCommand,
                type="operation.cancel",
                request_id="cancel-after-snapshot",
                scope=later.scope,
                target=later.id,
                expected_version=later.version,
                arguments=extension(rt.registry, "m3.empty", {}),
            ),
            later.operation_id,
        )
        # Deletion AFTER this snapshot must survive restoring that snapshot.
        retention = rt.maintenance.retention
        result = await retention.delete(
            host.scope, await retention.preview_delete(host.scope, [source_id])
        )
        assert result.complete
        await rt.close()
        await rt.store.close()
        close()
        code, out, err = await asyncio.to_thread(
            cli, "preview-restore", *host.args("--backup-id", backup_id)
        )
        assert code == 0, (out, err)
        code, out, err = await asyncio.to_thread(
            cli,
            "restore",
            *host.args(
                "--backup-id",
                backup_id,
                "--digest",
                digest_of(out),
                "--decision-id",
                "synthetic-decision",
            ),
        )
        assert code == 0, (out, err)
        assert (host.root / "RESTORE-PENDING").is_file()
        with pytest.raises(HostError, match="RESTORE_INCOMPLETE"):
            build(host.paths, load_config(host.paths), provider="mock")
        await finish_restore(host.root)
        preview = capsys.readouterr().out
        await finish_restore(host.root, digest=digest_of(preview), decision_id="finish-synthetic")
        assert not (host.root / "RESTORE-PENDING").exists()
        app, close = build(host.paths, load_config(host.paths), provider="mock")
        rt = app.state.runtime
        try:
            await rt.open()
            rt.maintenance.stop.set()
            await rt.maintenance.task
            assert rt.supervisor.snapshot.state == RunState.STOPPED
            a = await rt.jobs.store.load(running.operation_id, running.scope)
            b = await rt.jobs.store.load(queued.operation_id, queued.scope)
            assert a[0].status == OperationState.OUTCOME_UNKNOWN
            assert b[0].status == OperationState.CANCELLED
            from ai_agent_application.storage import Owner
            from ai_agent_contracts.llm import LLMAttempt

            from ai_agent_core.m3_composition.state import PRINCIPALS

            assert not await rt.store.repository(
                LLMAttempt, PRINCIPALS[Owner.LLM], host.scope
            ).list()
            stub = await rt.maintenance.retention.physical.source(host.scope, source_id)
            assert stub.availability == "deleted" and not stub.has_content
        finally:
            await rt.close()
            await rt.store.close()
            close()

    asyncio.run(scenario())
