"""Explicit offline last restore gate; never starts an agent or silently drops reports."""

import hashlib
import json
import tempfile
from pathlib import Path

from ai_agent_adapters.backup import SQLiteSnapshotter
from ai_agent_adapters.jobs.recovery import check_inventory
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.retention import SQLitePhysicalStore
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_application.memory import MemoryService
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import Scope

from ai_agent_core.local_artifacts import (
    apply_restore,
    plan_restore,
    verified_roots,
)
from ai_agent_core.local_backup.compose import HostLockGate
from ai_agent_core.local_backup.workplace import WorkplaceRestore, verify_restore_binding
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import PRINCIPALS

from .init import test_scope, work_scope
from .layout import HostConfig, HostError, load_config, paths
from .process import exclusive_host
from .recovery import HostLedger


def host_scopes(config: HostConfig) -> list[Scope]:
    work = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    spaces = (
        *config.test_space_ids,
        *([config.settings_space_id] if config.settings_space_id else []),
    )
    return [work, *(test_scope(work, space) for space in spaces)]


async def finish_restore(
    root: Path,
    *,
    drop_reports: bool = False,
    digest: str | None = None,
    decision_id: str | None = None,
) -> None:
    host = paths(root)
    with exclusive_host(host.root):
        config = load_config(host)
        marker = host.root / "RESTORE-PENDING"
        if not marker.is_file() or marker.is_symlink():
            raise HostError("RESTORE_NOT_PENDING")
        ledger = HostLedger(host, config)
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scratch: tempfile.TemporaryDirectory[str] | None = None
        try:
            gate = HostLockGate()
            gate.held = True  # exclusive_host above owns this exact offline host
            workplace = WorkplaceRestore(ledger.place, config, ledger, gate)
            with marker.open(encoding="utf-8") as stream:
                bound_switch = stream.read(1024).lstrip().startswith("{")
            if config.workplace_files is not None or bound_switch:
                await verify_restore_binding(host.root, host.database)
            await ledger.identity()
            if await SQLiteSnapshotter(host.database).integrity(host.database):
                raise HostError("RESTORE_INTEGRITY_FAILED")
            await store.check_schema()
            await check_inventory(store)
            roots = verified_roots(host.lab_indexes, host.reports)
            scopes = host_scopes(config)
            plan = await plan_restore(roots, ledger, scopes, drop_reports=drop_reports)
            blobs = workplace.live
            if blobs is None:
                # No file records are admissible without configured storage.
                # Ticket/session-only hosts still use the same bounded dependency scan.
                scratch = tempfile.TemporaryDirectory(dir=ledger.place.work_dir)
                blobs = DirectoryBlobs.prepare(Path(scratch.name) / "blobs")
            else:
                scratch = None
            await workplace.verify(host.database, blobs)
            count, redactions = await workplace.redactions(store, blobs)
            combined_digest = hashlib.sha256(
                json.dumps([plan.digest, redactions]).encode()
            ).hexdigest()
            if digest is None:
                print(f"report_files={plan.report_files} drop_reports={str(drop_reports).lower()}")
                print(
                    f"unproven_reports={plan.unproven_reports} blocked={str(plan.blocked).lower()}"
                )
                print(f"workplace_records_to_invalidate={count}")
                print(f"digest: {combined_digest}")
                return
            if plan.blocked or plan.unproven_reports:
                raise HostError("RESTORE_DERIVED_UNPROVEN")
            if digest != combined_digest or decision_id is None:
                raise HostError("PREVIEW_CHANGED")
            await apply_restore(
                roots,
                ledger,
                scopes,
                plan,
                decision_id,
                reports=Reports(host.reports, store, registry),
            )
            index = SQLiteRecallIndex(host.dialogue_index)
            try:
                physical = SQLitePhysicalStore(store)
                memory = MemoryService(store, PRINCIPALS[Owner.MEMORY], registry, index=index)
                retention = RetentionService(
                    store,
                    PRINCIPALS[Owner.MEMORY],
                    memory,
                    physical,
                    ledger,
                    index=index,
                    purgers=(workplace.purger(store, blobs, finishing=True),),
                )
                for scope in scopes:
                    applied = await retention.apply_ledger(scope)
                    swept = await retention.sweep(scope)
                    if applied.pending or swept.pending:
                        raise HostError("RESTORE_RETENTION_PENDING")
                    await memory.reindex_scope(scope)
                await workplace.verify(host.database, blobs)
            finally:
                index.close()
            final = await plan_restore(roots, ledger, scopes, drop_reports=drop_reports)
            if (
                final.blocked
                or final.unproven_reports
                or any(f.blocked_sources for f in final.indexes)
            ):
                raise HostError("RESTORE_DERIVED_UNPROVEN")
            # The staged DB has passed outbox quarantine; retention/reindex was checked again.
            # Jobs are shape-checked here; JobOwner reconciles their outcomes on the next serve,
            # before any HTTP/start/worker. No command is replayed by this maintenance command.
            marker.unlink()
            print("restore preparation complete; next serve starts STOPPED")
        finally:
            await store.close()
            ledger.close()
            if scratch is not None:
                scratch.cleanup()
