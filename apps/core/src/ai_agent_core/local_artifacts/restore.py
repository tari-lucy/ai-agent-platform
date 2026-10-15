"""Offline preparation of the derived directories for a restored database.

A restore brings back the database as of a copy. Derived files made later (lab indexes, report
bodies) are not rebuilt from it and nothing restores the old ones: this step only removes what
must not stay readable. The independent deletion ledger names the sources whose text must be gone;
`drop_reports` additionally makes every report body unavailable (an explicit, previewed loss: the
catalogue records stay and read answers STALE_CONTEXT). No LLM call, no live database access.
"""

import hashlib
import json
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ai_agent_adapters.backup.directory import wipe
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_application.retention import LedgerEntry
from ai_agent_contracts.common import Scope

from .indexes import held, purge_closed, recognised
from .purger import DECISION, ReportPurge
from .roots import (
    INDEX_SUFFIX,
    REPORT_SUFFIXES,
    ArtifactsError,
    DerivedRoots,
    companions,
    regular_files,
)


class LedgerEntries(Protocol):
    async def entries(self, scope: Scope) -> list[LedgerEntry]: ...


@dataclass(frozen=True)
class IndexFinding:
    name: str
    recognised: bool
    blocked_sources: int  # ledger-blocked sources whose text this index still holds


@dataclass(frozen=True)
class RestorePlan:
    indexes: tuple[IndexFinding, ...]
    ledger_sources: int  # sources the ledger names, across the scopes given
    report_files: int  # regular report files (bodies and markers) in the reports root
    report_bytes: int
    drop_reports: bool  # True: every report body becomes unavailable
    unproven_reports: int  # bodies left in place that cannot be shown free of deleted text
    open_conflict: bool  # an index file the owner still holds open: nothing may run
    digest: str

    @property
    def blocked(self) -> bool:
        return self.open_conflict or any(not f.recognised for f in self.indexes)


@dataclass(frozen=True)
class RestoreResult:
    indexes_cleaned: int
    sources_removed: int
    reports_dropped: int


async def _blocked(
    ledger: LedgerEntries, scopes: Sequence[Scope]
) -> tuple[dict[str, Scope], dict[str, tuple[str, ...]]]:
    by_key: dict[str, Scope] = {}
    ids: dict[str, tuple[str, ...]] = {}
    for scope in scopes:
        key = scope_key(scope)
        by_key[key] = scope
        ids[key] = tuple(sorted(e.source_id for e in await ledger.entries(scope)))
    return by_key, ids


async def plan_restore(
    roots: DerivedRoots,
    ledger: LedgerEntries,
    scopes: Sequence[Scope],
    *,
    drop_reports: bool = False,
    open_handles: Collection[Path] = (),
) -> RestorePlan:
    _, ids = await _blocked(ledger, scopes)
    pairs = {(key, sid) for key, values in ids.items() for sid in values}
    opened = {p.resolve() for p in open_handles}
    findings: list[IndexFinding] = []
    conflict = False
    for path in regular_files(roots.indexes, INDEX_SUFFIX):
        companions(path)  # a link next to the file closes the operation
        conflict = conflict or path.resolve() in opened
        if not recognised(path):
            findings.append(IndexFinding(path.name, False, 0))
            continue
        findings.append(IndexFinding(path.name, True, len(held(path) & pairs)))
    files = regular_files(roots.reports, REPORT_SUFFIXES)
    size = sum(f.stat().st_size for f in files)
    body = json.dumps(
        [[(f.name, f.recognised, f.blocked_sources) for f in findings], sorted(pairs)]
        + [len(files), size, drop_reports, conflict],
        sort_keys=True,
    )
    return RestorePlan(
        tuple(findings),
        len(pairs),
        len(files),
        size,
        drop_reports,
        0 if drop_reports else sum(f.suffix == ".json" for f in files),
        conflict,
        hashlib.sha256(body.encode()).hexdigest(),
    )


async def apply_restore(
    roots: DerivedRoots,
    ledger: LedgerEntries,
    scopes: Sequence[Scope],
    plan: RestorePlan,
    decision_id: str,
    *,
    reports: ReportPurge | None = None,
    open_handles: Collection[Path] = (),
) -> RestoreResult:
    """Only the previewed plan, only with a decision. Retry-safe; raises instead of claiming."""
    if not DECISION.fullmatch(decision_id):
        raise ArtifactsError("DECISION_REQUIRED")
    current = await plan_restore(
        roots, ledger, scopes, drop_reports=plan.drop_reports, open_handles=open_handles
    )
    if current != plan:
        raise ArtifactsError("PREVIEW_CHANGED")
    if current.open_conflict:
        raise ArtifactsError("INDEX_OPEN")  # an open file is never rewritten or unlinked
    if any(not f.recognised for f in current.indexes):
        raise ArtifactsError("UNRECOGNISED_INDEX_FILE")
    by_key, ids = await _blocked(ledger, scopes)
    cleaned = removed = 0
    for path in regular_files(roots.indexes, INDEX_SUFFIX):
        before = held(path)
        hits = {(k, s) for k, values in ids.items() for s in values} & before
        for key, scope in by_key.items():
            mine = [s for k, s in hits if k == key]
            if mine:
                await purge_closed(path, scope, mine)
        removed += len(hits)
        cleaned += bool(hits)
    if reports is not None:
        for key, scope in by_key.items():
            for source_id in ids[key]:
                await reports.purge(scope, source_id)  # owner-defined, idempotent
    dropped = 0
    if plan.drop_reports:
        for path in regular_files(roots.reports, REPORT_SUFFIXES):
            wipe(path)
            dropped += 1
        if regular_files(roots.reports, REPORT_SUFFIXES):
            raise ArtifactsError("PURGE_INCOMPLETE")
    after = await plan_restore(roots, ledger, scopes, drop_reports=plan.drop_reports)
    if any(f.blocked_sources for f in after.indexes):
        raise ArtifactsError("PURGE_INCOMPLETE")  # a marker is still readable: not "done"
    return RestoreResult(cleaned, removed, dropped)
