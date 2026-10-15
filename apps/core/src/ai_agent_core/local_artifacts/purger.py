"""DerivedPurger for derived laboratory files: recall indexes and report bodies.

Removal goes through the owners' own public operations (`RecallIndex.remove_source`/`compact`,
`Reports.purge`); relations are never guessed from file names or file content. Every failure is
raised to the retention owner, which leaves the source blocked and pending; a retry is safe.
"""

import hashlib
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.retention import DerivedPurger
from ai_agent_application.storage import Principal, Storage
from ai_agent_contracts.common import Scope
from ai_agent_contracts.workspace import Artifact

from .indexes import purge_closed, recognised
from .roots import INDEX_SUFFIX, ArtifactsError, DerivedRoots, regular_files

DECISION = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PAGE = 200


class ReportPurge(Protocol):
    """`m3_composition.artifacts.Reports` as it is: purge removes a body and its expiry marker."""

    async def purge(self, scope: Scope, source_id: str) -> None: ...


class ReportFiles(ReportPurge, Protocol):
    """Reports plus the one port it lacks: whether a report body file still exists.

    `holds` is NOT yet public on `Reports` (requested from the coordinator, see the report).
    """

    def holds(self, scope: Scope, source_id: str) -> bool: ...


# Owner-supplied inventory of the indexes it holds open: file -> handle. The purger removes rows
# through these handles and never opens, closes or unlinks a file the owner holds.
OpenIndexes = Callable[[], Mapping[Path, RecallIndex]]
# Owner-supplied: source ids of the reports (catalogue Artifact records) that exist in a scope.
ReportCatalogue = Callable[[Scope], Awaitable[Sequence[str]]]


@dataclass(frozen=True)
class ReportBinding:
    files: ReportFiles
    catalogue: ReportCatalogue


class DerivedArtifactPurger:
    def __init__(
        self,
        roots: DerivedRoots,
        open_indexes: OpenIndexes,
        reports: ReportBinding | None = None,
    ) -> None:
        self.roots, self.open_indexes, self.reports = roots, open_indexes, reports

    async def purge_sources(self, scope: Scope, source_ids: Sequence[str]) -> None:
        ids = tuple(sorted(dict.fromkeys(source_ids)))
        failures: list[str] = []

        async def attempt(work: Awaitable[None]) -> None:
            try:
                await work
            except ArtifactsError as error:
                failures.append(error.code)
            except Exception as error:  # the type only: a message may echo a path or text
                failures.append(type(error).__name__)

        handles = {path.resolve(): index for path, index in self.open_indexes().items()}
        for handle in handles.values():
            await attempt(self._open(handle, scope, ids))
        for path in regular_files(self.roots.indexes, INDEX_SUFFIX):
            if path.resolve() in handles:
                continue
            if not recognised(path):
                failures.append("UNRECOGNISED_INDEX_FILE")  # cannot be shown clean: fail closed
                continue
            await attempt(purge_closed(path, scope, ids))
        if self.reports is not None:
            for source_id in ids:
                await attempt(self.reports.files.purge(scope, source_id))
            # A body holds the text of lab sources, but no record says which sources. Exact ids
            # above cannot prove the other bodies clean, so any that remain keep the source
            # blocked_pending_purge until the operator invalidates them (previewed, decided).
            try:
                known = await self.reports.catalogue(scope)
                if any(self.reports.files.holds(scope, r) for r in known):
                    failures.append("REPORT_BODIES_UNLINKED")
            except Exception as error:
                failures.append(type(error).__name__)
        if failures:
            raise ArtifactsError("PURGE_INCOMPLETE", tuple(sorted(set(failures))))

    @staticmethod
    async def _open(index: RecallIndex, scope: Scope, ids: tuple[str, ...]) -> None:
        present = await index.inventory(scope)
        for source_id in ids:
            if source_id in present:
                await index.remove_source(scope, source_id)
        await index.compact()
        if set(ids) & set(await index.inventory(scope)):
            raise ArtifactsError("PURGE_INCOMPLETE")


def derived_purger(
    roots: DerivedRoots,
    open_indexes: OpenIndexes,
    reports: ReportBinding | None = None,
) -> DerivedPurger:
    """Factory for `RetentionService(..., purgers=(derived_purger(...),))`."""
    return DerivedArtifactPurger(roots, open_indexes, reports)


# ---- reports: the source -> report relation is not recorded --------------------------------


@dataclass(frozen=True)
class ScopeReportsPreview:
    """A report body holds the text of lab sources, but nothing records which sources.

    Until the owner records that relation, the only honest invalidation is all reports of the
    lab scope. It is shown first and runs only on an explicit operator decision.
    """

    scope: Scope
    source_ids: tuple[str, ...]  # the report sources whose bodies would become unavailable
    digest: str


def _digest(scope: Scope, ids: tuple[str, ...]) -> str:
    return hashlib.sha256("\0".join((*scope.partition, *ids)).encode()).hexdigest()


async def preview_scope_reports(scope: Scope, catalogue: ReportCatalogue) -> ScopeReportsPreview:
    ids = tuple(sorted(dict.fromkeys(await catalogue(scope))))
    return ScopeReportsPreview(scope, ids, _digest(scope, ids))


async def invalidate_scope_reports(
    preview: ScopeReportsPreview,
    decision_id: str,
    reports: ReportPurge,
    catalogue: ReportCatalogue,
) -> int:
    """Remove the bodies of the previewed reports. Catalogue records stay: reading them then
    answers STALE_CONTEXT, so the loss of availability is explicit. Retry-safe."""
    if not DECISION.fullmatch(decision_id):
        raise ArtifactsError("DECISION_REQUIRED")
    if await preview_scope_reports(preview.scope, catalogue) != preview:
        raise ArtifactsError("PREVIEW_CHANGED")
    for source_id in preview.source_ids:
        await reports.purge(preview.scope, source_id)
    return len(preview.source_ids)


def artifact_catalogue(storage: Storage, principal: Principal) -> ReportCatalogue:
    """Report source ids of a scope from the catalogue owner's own records (public repository)."""

    async def catalogue(scope: Scope) -> Sequence[str]:
        found: list[str] = []
        after: str | None = None
        while True:
            page = await storage.repository(Artifact, principal, scope).list(
                limit=PAGE, after_id=after
            )
            found.extend(item.provenance_source.source_id for item in page)
            if len(page) < PAGE:
                return found
            after = page[-1].id

    return catalogue
