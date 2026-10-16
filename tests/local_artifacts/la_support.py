"""Temporary host, real lab runs (index + report), real retention with an independent ledger."""

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.laboratory import LabRunner, Scenario
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from ai_agent_application.memory import IndexHealth, MemoryService, RecallService
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.common import ArtifactRef, Scope
from ai_agent_contracts.memory import RecallRequest, Source
from ai_agent_contracts.workspace import Artifact

from ai_agent_core.local_artifacts import (
    ReportBinding,
    artifact_catalogue,
    derived_purger,
    verified_roots,
)
from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import initialise, test_scope, work_scope
from ai_agent_core.local_host.layout import HostConfig, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.state import OPERATOR, PRINCIPALS

MEMORY = PRINCIPALS[Owner.MEMORY]
CATALOGUE = PRINCIPALS[Owner.CATALOGUE]


def run(coro):
    return asyncio.run(coro)


class FileHolds:
    """Legacy port adapter without selective lineage, for explicit fallback acceptance."""

    def __init__(self, reports: Reports) -> None:
        self.reports = reports

    async def purge(self, scope, source_id) -> None:
        await self.reports.purge(scope, source_id)

    def holds(self, scope, source_id) -> bool:
        return self.reports.holds(scope, source_id)


@dataclass
class Lab:
    scope: Scope
    source_id: str
    artifact: ArtifactRef


@dataclass
class World:
    root: Path
    config: HostConfig
    store: SQLiteStore
    registry: object
    reports: Reports
    ledger: SQLiteDeletionLedger
    opened: dict[Path, SQLiteRecallIndex] = field(default_factory=dict)
    failing: set[str] = field(default_factory=set)

    @property
    def host(self):
        return paths(self.root)

    @property
    def work(self) -> Scope:
        c = self.config
        return work_scope(c.workspace_id, c.persona_id, c.work_space_id)

    def scope(self, number: int) -> Scope:
        return test_scope(self.work, self.config.test_space_ids[number])

    def open_indexes(self):
        return dict(self.opened)

    def new_index(self) -> SQLiteRecallIndex:
        path = self.host.lab_indexes / f"{len(list(self.host.lab_indexes.iterdir())):04d}.sqlite"
        index = SQLiteRecallIndex(path)
        self.opened[path] = index
        return index

    def close_indexes(self) -> None:
        for index in self.opened.values():
            index.close()
        self.opened.clear()

    async def lab(self, number: int, marker: str) -> Lab:
        """One real lab run in a TEST scope: marker in a source, the index and the report body."""
        scope = self.scope(number)

        async def text_scenario(lab) -> None:
            await lab.episode(f"Беседа у костра: {marker}, и больше ничего.")

        runner = LabRunner(
            self.store,
            lambda _label: _constant(scope),
            self.new_index,
            report_access=(RecordAccess(OPERATOR.id, api_transfer=True),),
            artifact_writer=self.reports.save,
        )
        report = await runner.run(Scenario("fire", 1, "Fire", text_scenario), seed=f"s{number}")
        assert report.status == "passed", report.error
        sources = await self.store.repository(Source, MEMORY, scope).list(limit=50)
        source = next(s for s in sources if marker in (s.content or ""))
        artifacts = await self.store.repository(Artifact, CATALOGUE, scope).list(limit=50)
        return Lab(scope, source.source_id, artifacts[-1].reference)

    def retention(self, *, purgers) -> RetentionService:
        memory = MemoryService(self.store, MEMORY, self.registry)
        return RetentionService(
            self.store,
            MEMORY,
            memory,
            SQLitePhysicalStore(self.store),
            self.ledger,
            purgers=purgers,
        )

    def purger(self):
        binding = ReportBinding(FileHolds(self.reports), artifact_catalogue(self.store, CATALOGUE))
        return derived_purger(
            verified_roots(self.host.lab_indexes, self.host.reports), self.open_indexes, binding
        )

    async def hears(self, lab: Lab, query: str) -> list[str]:
        """Recall through every lab index file for this scope (open handles, read as is)."""
        found: list[str] = []
        health = IndexHealth()
        for index in self.opened.values():
            recall = RecallService(self.store, MEMORY, index, health=health)
            result = await recall.recall(
                RecallRequest(scope=lab.scope, query=query, purpose="private_context")
            )
            found.extend(f.content for f in result.fragments)
        return found

    async def download(self, lab: Lab) -> bytes:
        return await self.reports.read(lab.scope, lab.artifact.artifact_id, OPERATOR)

    async def close(self) -> None:
        self.close_indexes()
        self.ledger.close()
        await self.store.close()


async def _constant(scope: Scope) -> Scope:
    return scope


async def make_world(base: Path) -> World:
    root = base / "host"
    previous = os.umask(0o077)
    try:
        config = await initialise(
            root, profile_markdown=REFERENCE_PROFILE, port=1024, test_scopes=3
        )
    finally:
        os.umask(previous)
    host = paths(root)
    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    await SQLitePhysicalStore(store).install()
    reports = Reports(
        host.reports, store, registry, readers=rights.report_readers(), api_transfer_allowed=True
    )
    ledger_dir = base / "ledger"
    ledger_dir.mkdir(mode=0o700)
    return World(
        root, config, store, registry, reports, SQLiteDeletionLedger(ledger_dir / "l.sqlite")
    )


def residue(world: World, needle: str, extra: tuple[Path, ...] = ()) -> list[str]:
    """Names of derived files (index + companions, report files) still holding the text."""
    raw = needle.encode()
    found = []
    for folder in (world.host.lab_indexes, world.host.reports, *extra):
        for path in sorted(folder.iterdir()):
            if path.is_file() and raw in path.read_bytes():
                found.append(path.name)
    return found
