"""Retention owner (M5-01): expiry, deletion with tombstone, restore gate, metadata lifecycle.

Order of every removal: (1) independent ledger + logical block, so nothing is served from now on;
(2) derived copies (index, caches, exports); (3) physical erasure; (4) ledger/tombstone say purged.
A failure after (1) leaves the source blocked and pending: it is never served and ``resume`` or
``apply_ledger`` finishes the job. Nothing here starts an agent or calls a model.
"""

import hashlib
import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import DeletionTombstone

from ai_agent_application.memory import MemoryService, RecallIndex, derived_id
from ai_agent_application.storage import Owner, Principal, Storage

from .model import (
    DeletionPreview,
    DeletionReport,
    LedgerApplication,
    LedgerEntry,
    SourceOutcome,
    SweepReport,
)
from .policy import RetentionPolicy
from .ports import DeletionLedger, DerivedPurger, PhysicalStore

MAX_BATCH = 500


class RetentionService:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        memory: MemoryService,
        physical: PhysicalStore,
        ledger: DeletionLedger,
        *,
        index: RecallIndex | None = None,
        purgers: Sequence[DerivedPurger] = (),
        policy: RetentionPolicy | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if principal.role != "core_owner" or principal.owner is not Owner.MEMORY:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.storage, self.principal, self.memory = storage, principal, memory
        self.physical, self.ledger, self.index = physical, ledger, index
        self.purgers, self.policy, self.clock = tuple(purgers), policy or RetentionPolicy(), clock

    # ---- deletion ------------------------------------------------------------------------

    async def preview_delete(self, scope: Scope, source_ids: Sequence[str]) -> DeletionPreview:
        ids = tuple(sorted(dict.fromkeys(source_ids)))
        if not ids or len(ids) > MAX_BATCH:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        for source_id in ids:
            if await self.physical.source(scope, source_id) is None:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        erased, detached = await self.physical.dependents(scope, ids)
        indexed: tuple[str, ...] = ()
        if self.index is not None:
            inventory = await self.index.inventory(scope)
            indexed = tuple(i for i in ids if i in inventory)
        digest = _digest(scope, ids, erased, detached, indexed)
        return DeletionPreview(ids, erased, detached, indexed, digest)

    async def delete(self, scope: Scope, preview: DeletionPreview) -> DeletionReport:
        """Operator-confirmed. A changed area is a new decision, never silently widened."""
        if await self.preview_delete(scope, preview.source_ids) != preview:
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        now = self.clock()
        for source_id in preview.source_ids:
            stub = await self.physical.source(scope, source_id)
            assert stub is not None
            await self.ledger.block(scope, source_id, stub.version, "operator_delete", now)
        return await self._purge(scope, preview.source_ids, "deleted", preview.erased_with)

    async def resume(self, scope: Scope) -> DeletionReport:
        """Finish removals whose ledger line has no purge time (crash, failed step)."""
        pending = [e for e in await self.ledger.entries(scope) if e.purged_at is None]
        if not pending:
            return DeletionReport((), (), True)
        erased, _ = await self.physical.dependents(scope, [e.source_id for e in pending])
        outcomes: list[SourceOutcome] = []
        for entry in pending:
            availability = "deleted" if entry.reason != "expired" else "expired"
            report = await self._purge(scope, (entry.source_id,), availability, erased, entry)
            outcomes.extend(report.outcomes)
        return DeletionReport(tuple(outcomes), erased, all(o.state == "purged" for o in outcomes))

    async def _purge(
        self,
        scope: Scope,
        source_ids: Sequence[str],
        availability: str,
        erased: Sequence[tuple[str, str]],
        entry: LedgerEntry | None = None,
    ) -> DeletionReport:
        outcomes: list[SourceOutcome] = []
        for source_id in source_ids:
            step = "block"
            try:
                await self.physical.revoke(scope, source_id, availability)
                await self._tombstone(scope, source_id, entry, purged=False)
                step = "derived"
                await self._derived(scope, [source_id])
                step = "physical"
                await self.physical.erase_source(scope, source_id, availability)
                outcomes.append(SourceOutcome(source_id, "purged"))
            except Exception:  # fail closed: stays blocked, the type is not echoed
                outcomes.append(SourceOutcome(source_id, "blocked_pending_purge", step))
        done = [o.source_id for o in outcomes if o.state == "purged"]
        erased_now: tuple[tuple[str, str], ...] = ()
        if done and len(done) == len(source_ids):
            try:
                if erased:
                    await self.physical.erase_records(scope, erased)
                    erased_now = tuple(erased)
                await self.physical.finalize()
            except Exception:
                outcomes = [
                    SourceOutcome(o.source_id, "blocked_pending_purge", "physical")
                    for o in outcomes
                ]
                erased_now = ()
        elif done:
            await self.physical.finalize()
        at = self.clock()
        for outcome in outcomes:
            if outcome.state == "purged":
                await self.ledger.mark_purged(scope, outcome.source_id, at)
                await self._tombstone(scope, outcome.source_id, entry, purged=True)
        return DeletionReport(
            tuple(outcomes), erased_now, all(o.state == "purged" for o in outcomes)
        )

    async def _derived(self, scope: Scope, source_ids: Sequence[str]) -> None:
        if self.index is not None:
            for source_id in source_ids:
                await self.index.remove_source(scope, source_id)
            await self.index.compact()
        for purger in self.purgers:
            await purger.purge_sources(scope, source_ids)

    async def _tombstone(
        self, scope: Scope, source_id: str, entry: LedgerEntry | None, *, purged: bool
    ) -> None:
        """Immutable events (the record has no version): one when blocked, one when purged."""
        ledger = {e.source_id: e for e in await self.ledger.entries(scope)}
        line = entry or ledger[source_id]
        suffix = "purged" if purged else "blocked"
        tombstone_id = derived_id("tomb", scope, source_id, suffix)
        at = self.clock()
        record = DeletionTombstone.model_validate(
            {
                "id": tombstone_id,
                "scope": scope.model_dump(),
                "created_at": at,
                "source_id": source_id,
                "source_version": line.version,
                "blocked_at": line.blocked_at,
                "purged_at": at if purged else None,
                "reason": line.reason,
            }
        )
        command = MessageCommand(
            request_id=tombstone_id,
            scope=scope,
            target="retention",
            type="dialogue.message",
            arguments=MessageArguments(text="tombstone"),
        )
        async with self.storage.transaction(self.principal, scope, command) as unit:
            if unit.replay is not None:
                return
            await unit.put(record)
            await unit.complete(
                Receipt(
                    request_id=tombstone_id,
                    scope=scope,
                    status="completed",
                    resource_id=tombstone_id,
                    version=1,
                )
            )

    # ---- expiry ---------------------------------------------------------------------------

    async def sweep(self, scope: Scope) -> SweepReport:
        """Retire raw text past its term. The term is never extended; gaps are reported."""
        now = self.clock()
        cutoff = now - self.policy.test_source if scope.memory_mode == "test" else None
        due = await self.physical.due_sources(scope, now=now, test_cutoff=cutoff)
        expired: list[str] = []
        lost: list[str] = []
        pending: list[str] = []
        for stub in due[:MAX_BATCH]:
            if stub.protected:
                continue
            if await self.physical.durable_derivatives(scope, stub.source_id) == 0:
                lost.append(stub.source_id)
            await self.ledger.block(scope, stub.source_id, stub.version, "expired", now)
        ids = [s.source_id for s in due[:MAX_BATCH] if not s.protected]
        for source_id in ids:
            report = await self._purge(scope, (source_id,), "expired", ())
            (expired if report.complete else pending).append(source_id)
        metadata = await self.physical.metadata_sweep(
            scope,
            llm_before=now - self.policy.llm_metadata,
            aggregate_before=now - self.policy.aggregate,
            job_before=now - self.policy.job,
            now=now,
        )
        return SweepReport(tuple(expired), tuple(lost), tuple(pending), metadata)

    # ---- restore gate ---------------------------------------------------------------------

    async def apply_ledger(self, scope: Scope) -> LedgerApplication:
        """After a restore: the ledger outranks the restored database. Idempotent."""
        reapplied: list[str] = []
        missing: list[str] = []
        pending: list[str] = []
        for entry in await self.ledger.entries(scope):
            stub = await self.physical.source(scope, entry.source_id)
            if stub is None:
                missing.append(entry.source_id)
                continue
            clean = stub.availability != "available" and not stub.has_content
            if clean and entry.purged_at is not None:
                continue
            availability = "expired" if entry.reason == "expired" else "deleted"
            report = await self._purge(scope, (entry.source_id,), availability, (), entry)
            (reapplied if report.complete else pending).append(entry.source_id)
        return LedgerApplication(tuple(reapplied), tuple(missing), tuple(pending))


def _digest(scope: Scope, *parts: Any) -> str:
    body = json.dumps([scope.partition, *parts], sort_keys=True, default=list)
    return hashlib.sha256(body.encode()).hexdigest()
