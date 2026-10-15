"""Host-owned retention and daily snapshots, outside the LLM/agent watchdog."""

import asyncio
import json
import os
import sqlite3
import tempfile
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ai_agent_adapters.backup import DirectoryBackupStore
from ai_agent_adapters.retention import SQLitePhysicalStore
from ai_agent_application.memory import MemoryService
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Owner

from ai_agent_core.local_artifacts import (
    ReportBinding,
    artifact_catalogue,
    derived_purger,
    verified_roots,
)
from ai_agent_core.local_backup.compose import create_backup_callback
from ai_agent_core.m3_composition.state import PRINCIPALS

from .layout import HostConfig, HostError, HostPaths, restrict
from .process import exclusive_host
from .recovery import HostLedger

if TYPE_CHECKING:
    from ai_agent_core.m3_composition.service import Runtime


class HostMaintenance:
    def __init__(
        self,
        host: HostPaths,
        config: HostConfig,
        runtime: Runtime,
        indexes: Mapping[Path, RecallIndex],
    ) -> None:
        self.host, self.config, self.runtime, self.indexes = host, config, runtime, indexes
        self.ledger = HostLedger(host, config, fail=runtime.supervisor.critical_failure)
        self.task: asyncio.Task[None] | None = None
        self.stop = asyncio.Event()
        self.attempted_day = ""
        self.pending = 0
        self.retention: RetentionService | None = None

    async def open(self) -> None:
        await self.ledger.identity()  # a missing/rolled-back ledger closes startup before HTTP

        def installed() -> bool:
            with closing(sqlite3.connect(f"{self.host.database.as_uri()}?mode=ro", uri=True)) as db:
                return (
                    db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='retention_usage_aggregates'"
                    ).fetchone()
                    is not None
                )

        if not await asyncio.to_thread(installed):
            raise HostError("RETENTION_NOT_INSTALLED")
        rt = self.runtime
        assert rt.reports is not None
        roots = verified_roots(self.host.lab_indexes, self.host.reports)
        purger = derived_purger(
            roots,
            lambda: self.indexes,
            ReportBinding(
                rt.reports,
                artifact_catalogue(rt.store, PRINCIPALS[Owner.CATALOGUE]),
            ),
        )
        memory = MemoryService(
            rt.store, PRINCIPALS[Owner.MEMORY], rt.registry, index=rt.dialogue_index
        )
        self.retention = RetentionService(
            rt.store,
            PRINCIPALS[Owner.MEMORY],
            memory,
            SQLitePhysicalStore(rt.store),
            self.ledger,
            index=rt.dialogue_index,
            purgers=(purger,),
        )
        for scope in rt.scopes:
            result = await self.retention.apply_ledger(scope)
            self.pending += len(result.pending)

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def close(self) -> None:
        self.stop.set()
        if self.task is not None:
            await self.task  # do not close a ledger while a snapshot is still using it
        self.ledger.close()

    async def once(self, now: datetime) -> None:
        assert self.retention is not None
        pending = 0
        for scope in self.runtime.scopes:
            resumed = await self.retention.resume(scope)
            swept = await self.retention.sweep(scope)
            pending += sum(o.state != "purged" for o in resumed.outcomes) + len(swept.pending)
        self.pending = pending
        day = now.astimezone(UTC).date().isoformat()
        if self.attempted_day != day:
            # Backup adapters do synchronous hashing/copying. The complete callback runs on
            # its own event loop/thread, with a separate read connection to this SAME ledger.
            # STOP and the control watchdog never wait for those file operations.
            await asyncio.to_thread(self._copy, day)
            self.attempted_day = day
        self._status("RETENTION_PENDING" if pending else "READY")

    def _copy(self, day: str) -> None:
        async def work() -> None:
            place = self.ledger.place
            place.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            restrict(place.work_dir, directory=True)
            with exclusive_host(place.work_dir):
                manifests = await DirectoryBackupStore(place.store_dir).manifests()
                if any(
                    m.kind == "daily" and m.created_at.astimezone(UTC).date().isoformat() == day
                    for m in manifests
                ):
                    return
                await create_backup_callback(place)()

        asyncio.run(work())

    async def _run(self) -> None:
        assert self.config.recovery is not None
        while not self.stop.is_set():
            try:
                await self.once(datetime.now(UTC))
            except Exception as error:
                # Fixed class/code only. Never stringify IO/provider exceptions or memory text.
                code = error.code if isinstance(error, HostError) else "MAINTENANCE_FAILED"
                self._status(code)
            try:
                await asyncio.wait_for(self.stop.wait(), self.config.recovery.interval_seconds)
            except TimeoutError:
                pass

    def _status(self, code: str) -> None:
        descriptor, name = tempfile.mkstemp(dir=self.host.root, prefix=".maintenance-")
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "status": code,
                        "updated_at": datetime.now(UTC).isoformat(),
                        "pending": self.pending,
                    },
                    handle,
                )
            temporary.replace(self.host.root / "maintenance.json")
        finally:
            temporary.unlink(missing_ok=True)
