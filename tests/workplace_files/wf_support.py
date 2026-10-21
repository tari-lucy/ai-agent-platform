"""Synthetic real SQLite/ledger/files world. No production host or provider."""

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.retention.ledger import SQLiteDeletionLedger
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_adapters.workspace_files.usage import CatalogueUsage
from ai_agent_application.agent_workplace.ports import WorkAdmission
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_application.workspace_files import FileLimits, FileReleaseGuard, ManagedFiles
from ai_agent_contracts.agent_workplace import FileAccess, FileWrite, WorkAttempt
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Freshness
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.states import ErrorCode, Provenance

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 21, tzinfo=UTC)
AREA = Scope(
    workspace_id="sample", persona_id="fictional", memory_space_id="work", memory_mode="work"
)
RUN = AREA.model_copy(update={"agent_id": "agent", "run_id": "run"})
FRESH = Freshness(run_id="run", control_epoch=1, profile_version=1, policy_version=1)
CAT = Principal("catalogue", "core_owner", Owner.CATALOGUE)
MEM = Principal("memory", "core_owner", Owner.MEMORY)
ACT = Principal("activity", "core_owner", Owner.ACTIVITY)


class World:
    @classmethod
    async def create(cls, directory, *, limits=None):
        self = cls()
        self.now = NOW
        self.clock = lambda: self.now
        self.active = True
        self.allowed = True
        self.serial = 0
        self.store = SQLiteStore(directory / "records.sqlite", clock=self.clock)
        await self.store.upgrade(ROOT / "migrations")
        await self.store.provision(
            AREA,
            (
                Grant(CAT, Owner.CATALOGUE, read=True, write=True, api_transfer=True),
                Grant(CAT, Owner.MEMORY, read=True, api_transfer=True),
                Grant(CAT, Owner.ACTIVITY, read=True),
                Grant(MEM, Owner.MEMORY, read=True, write=True),
                Grant(ACT, Owner.ACTIVITY, read=True, write=True),
                Grant(ACT, Owner.MEMORY, read=True),
            ),
        )
        self.ledger = SQLiteDeletionLedger(directory / "ledger.sqlite")
        self.registry = ExtensionRegistry()
        self.memory = MemoryService(
            self.store,
            MEM,
            self.registry,
            clock=self.clock,
            reader_access=(RecordAccess(CAT.id, api_transfer=True), RecordAccess(ACT.id)),
        )
        blob_root = directory / "blobs"
        blob_root.mkdir(mode=0o700)
        self.blobs = DirectoryBlobs(blob_root)
        self.guard = FileReleaseGuard(self.store, CAT, self.ledger, clock=self.clock)
        self.files = ManagedFiles(
            self.store,
            CAT,
            self.blobs,
            self.guard,
            self,
            CatalogueUsage(self.store).counts,
            limits or FileLimits(4096, 16384, 20, 100),
            clock=self.clock,
            authorize=self.authorize,
            default_access=FileAccess(api_transfer=True, public_disclosure=False),
        )
        return self

    async def authorize(self, admission, operation, area):
        if not self.allowed or admission.scope.partition != AREA.partition or area != "knowledge":
            raise ContractError(ErrorCode.ACCESS_DENIED)

    def command(self, scope, request):
        return MessageCommand(
            request_id=request,
            scope=scope,
            target="sample",
            type="dialogue.message",
            arguments=MessageArguments(text=request),
        )

    async def source(self, *, api=True):
        self.serial += 1
        result = await self.memory.register_source(
            self.command(AREA, f"source-{self.serial}"),
            SourceDraft(
                provenance=Provenance.OBSERVATION,
                source_type="sample",
                adapter_id="sample",
                adapter_version="1",
                occurred_at=self.now,
                content="Synthetic observation about a lighthouse.",
                read_allowed=True,
                api_transfer_allowed=api,
                public_disclosure_allowed=False,
            ),
        )
        return SourceRef(
            source_id=result.source_id, version=1, scope=AREA, availability="available"
        )

    async def attempt(self, scope, attempt_id):
        return await self.store.repository(WorkAttempt, CAT, scope).get(attempt_id)

    async def admit(self, sources=(), files=()):
        self.serial += 1
        identifier = f"attempt-{self.serial}"
        record = WorkAttempt(
            id=identifier,
            version=1,
            scope=RUN,
            created_at=self.now,
            updated_at=self.now,
            task_id="task",
            attempt_no=self.serial,
            operation_id="operation-" + identifier,
            capability_id="files.write",
            capability_version=1,
            freshness=FRESH,
            state="prepared",
            inputs=list(sources),
            input_files=list(files),
        )
        command = self.command(RUN, "create-" + identifier)
        async with self.store.transaction(ACT, RUN, command) as unit:
            await unit.put(record, access=(RecordAccess(CAT.id),))
            await unit.complete(
                Receipt(
                    request_id=command.request_id,
                    scope=RUN,
                    status="completed",
                    resource_id=identifier,
                    version=1,
                )
            )
        return WorkAdmission(RUN, FRESH, record.operation_id, identifier, 1, lambda: self.active)

    def change(
        self,
        data=b"A synthetic lighthouse note.",
        *,
        file_id="note",
        request="write-1",
        expected=None,
    ):
        return FileWrite(
            request_id=request,
            file_id=file_id,
            expected_version=expected,
            area="knowledge",
            path=file_id + ".md",
            title="A field note",
            kind="note",
            provenance="generated",
            media_type="text/markdown",
            checksum=hashlib.sha256(data).hexdigest(),
            size_bytes=len(data),
        ), data

    async def close(self):
        self.ledger.close()
        await self.store.close()


def run(tmp_path, scenario, *, limits=None):
    async def execute():
        world = await World.create(tmp_path, limits=limits)
        try:
            await scenario(world)
        finally:
            await world.close()

    asyncio.run(execute())
