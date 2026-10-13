"""LLM attempt, tariff and cost ledger (M2-05). Owner: ``Owner.LLM``.

One attempt is one provider call. Its record id derives from the attempt id, so a repeated
receipt, a retried write or a second process reporting the same attempt finds the existing record
instead of creating a new paid attempt; only a better *late* observation (a final usage after
STOP, a terminal status for an ``unknown`` attempt) refines it. Nothing here guesses a price:
a missing tariff, rate or token count leaves the cost partial or unknown, never zero.
Prompts, replies, endpoints and credentials are not part of any record or projection.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ai_agent_contracts.api import CommandBase, Receipt
from ai_agent_contracts.codec import ContractError, from_wire, to_wire
from ai_agent_contracts.common import Id, Record, Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import CostCalculation, LLMAssignment, LLMAttempt, Tariff, TokenUsage
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter

from ai_agent_application.llm import AttemptReport
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner, Principal, RecordAccess, RecordRepository, Storage

from .pricing import Cost, compute_cost, money

LIST_PAGE = 200
MAX_RETRIES = 3
MAX_TARIFF_VERSIONS = 500

_STATUS_RANK = {
    "running": 0,
    "unknown": 1,
    "failed": 2,
    "cancelled": 2,
    "incomplete": 2,
    "completed": 3,
}


def attempt_record_id(scope: Scope, attempt_id: str) -> str:
    return derived_id("attempt", scope, attempt_id)


def tariff_record_id(scope: Scope, connection_id: str, model_id: str) -> str:
    return derived_id("tariff", scope, connection_id, model_id)


def cost_record_id(scope: Scope, attempt_id: str) -> str:
    return derived_id("cost", scope, attempt_id)


def _usage_rank(usage: TokenUsage) -> int:
    if usage.source == "provider":
        return 3 if usage.completeness == "complete" else 2
    return 1 if usage.source == "local_estimate" else 0


def merge_usage(old: TokenUsage, new: TokenUsage) -> TokenUsage:
    """Keep the more trustworthy observation; never add them together.

    A late provider usage replaces an estimate or a partial report, and nothing replaces a
    complete provider report with something weaker. Two complete provider reports that disagree
    are a conflict, not a revision.
    """
    if _usage_rank(new) > _usage_rank(old):
        return new
    if _usage_rank(new) == _usage_rank(old) == 3 and new != old:
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
    return old


@dataclass(frozen=True)
class TariffDraft:
    connection_id: Id
    model_id: Id
    unit_tokens: int
    currency: str
    effective_at: datetime
    input_rate: str | None = None
    output_rate: str | None = None
    cached_input_rate: str | None = None
    reasoning_output_rate: str | None = None


@dataclass(frozen=True)
class AttemptWritten:
    attempt: LLMAttempt
    created: bool
    changed: bool


class UsageLedger:
    def __init__(
        self,
        storage: Storage,
        principal: Principal,
        extensions: ExtensionRegistry,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reader_access: tuple[RecordAccess, ...] = (),
    ) -> None:
        if principal.role != "core_owner" or principal.owner is not Owner.LLM:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        self.storage, self.principal, self.extensions = storage, principal, extensions
        self.clock, self.reader_access = clock, reader_access

    # ---- plumbing ---------------------------------------------------------------------

    def _repo[R: Record](self, model: type[R], scope: Scope) -> RecordRepository[R]:
        return self.storage.repository(model, self.principal, scope)

    def _record[R: Any](self, model: type[R], payload: dict[str, Any]) -> R:
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        return from_wire(TypeAdapter(model), encoded, extensions=self.extensions)

    def _revised[R: Any](self, record: R, **changes: Any) -> R:
        data = json.loads(to_wire(record, extensions=self.extensions))
        data.update(changes)
        return self._record(type(record), data)

    async def _write(self, command: CommandBase, record: Record, expected: int | None) -> None:
        async with self.storage.transaction(self.principal, command.scope, command) as unit:
            if unit.replay is not None:
                return
            await unit.put(record, expected_version=expected, access=self.reader_access or None)
            version = getattr(record, "version", 1)
            await unit.complete(
                Receipt(
                    request_id=command.request_id,
                    scope=command.scope,
                    status="completed",
                    resource_id=record.id,
                    version=version,
                )
            )

    async def _all[R: Record](self, model: type[R], scope: Scope) -> list[R]:
        repository = self._repo(model, scope)
        found: list[R] = []
        after: str | None = None
        while True:
            page = await repository.list(limit=LIST_PAGE, after_id=after)
            found.extend(page)
            if len(page) < LIST_PAGE:
                return found
            after = page[-1].id

    @staticmethod
    def _iso(value: datetime) -> str:
        return value.isoformat()

    # ---- attempts ---------------------------------------------------------------------

    async def record_attempt(self, command: CommandBase, report: AttemptReport) -> AttemptWritten:
        """Create the attempt once; later receipts of the same attempt only refine it."""
        scope = command.scope
        record_id = attempt_record_id(scope, report.attempt_id)
        for _ in range(MAX_RETRIES):
            existing = await self._repo(LLMAttempt, scope).get(record_id)
            if existing is None:
                created = self._new_attempt(
                    scope, record_id, report, await self._pin(scope, report)
                )
                try:
                    await self._write(command, created, None)
                except ContractError as error:
                    if error.error.code is ErrorCode.VERSION_CONFLICT:
                        continue  # a concurrent writer created it first: merge on the next pass
                    raise
                return AttemptWritten(created, True, True)
            merged = self._merge_attempt(existing, report)
            if merged is None:
                return AttemptWritten(existing, False, False)
            try:
                await self._write(command, merged, existing.version)
            except ContractError as error:
                if error.error.code is ErrorCode.VERSION_CONFLICT:
                    continue
                raise
            return AttemptWritten(merged, False, True)
        raise ContractError(ErrorCode.VERSION_CONFLICT)

    def _new_attempt(
        self,
        scope: Scope,
        record_id: str,
        report: AttemptReport,
        pin: Tariff | None,
    ) -> LLMAttempt:
        now = self.clock()
        return self._record(
            LLMAttempt,
            {
                "id": record_id,
                "scope": scope.model_dump(mode="json"),
                "created_at": self._iso(now),
                "updated_at": self._iso(now),
                "version": 1,
                "operation_id": report.operation_id,
                "attempt_id": report.attempt_id,
                "role_id": report.role_id,
                "assignment_id": report.assignment_id,
                "assignment_version": report.assignment_version,
                "requested_model_id": report.requested_model_id,
                "reported_model_id": report.reported_model_id,
                "status": report.status,
                "started_at": self._iso(report.started_at),
                "completed_at": self._iso(report.completed_at),
                "usage": report.usage.model_dump(mode="json"),
                "tariff_id": None if pin is None else pin.id,
                "tariff_version": None if pin is None else pin.version,
            },
        )

    def _merge_attempt(self, existing: LLMAttempt, report: AttemptReport) -> LLMAttempt | None:
        same = (
            existing.operation_id == report.operation_id
            and existing.role_id == report.role_id
            and existing.assignment_id == report.assignment_id
            and existing.assignment_version == report.assignment_version
            and existing.requested_model_id == report.requested_model_id
            and existing.started_at == report.started_at
        )
        if not same:
            raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
        changes: dict[str, Any] = {}
        usage = merge_usage(existing.usage, report.usage)
        if usage != existing.usage:
            changes["usage"] = usage.model_dump(mode="json")
        if _STATUS_RANK[report.status] > _STATUS_RANK[existing.status]:
            changes["status"] = report.status
            changes["completed_at"] = self._iso(report.completed_at)
        if existing.reported_model_id is None and report.reported_model_id is not None:
            changes["reported_model_id"] = report.reported_model_id
        if not changes:
            return None
        changes["version"] = existing.version + 1
        changes["updated_at"] = self._iso(max(self.clock(), existing.updated_at))
        return self._revised(existing, **changes)

    async def attempt(self, scope: Scope, attempt_id: str) -> LLMAttempt | None:
        return await self._repo(LLMAttempt, scope).get(attempt_record_id(scope, attempt_id))

    # ---- tariffs ----------------------------------------------------------------------

    async def set_tariff(self, command: CommandBase, draft: TariffDraft) -> Tariff:
        """New tariff version for (connection, model); an identical draft changes nothing."""
        scope = command.scope
        record_id = tariff_record_id(scope, draft.connection_id, draft.model_id)
        for _ in range(MAX_RETRIES):
            current = await self._repo(Tariff, scope).get(record_id)
            payload = {
                "connection_id": draft.connection_id,
                "model_id": draft.model_id,
                "unit_tokens": draft.unit_tokens,
                "currency": draft.currency,
                "input_rate": draft.input_rate,
                "output_rate": draft.output_rate,
                "cached_input_rate": draft.cached_input_rate,
                "reasoning_output_rate": draft.reasoning_output_rate,
                "effective_at": self._iso(draft.effective_at),
            }
            now = self.clock()
            if current is None:
                tariff = self._record(
                    Tariff,
                    {
                        "id": record_id,
                        "scope": scope.model_dump(mode="json"),
                        "created_at": self._iso(now),
                        "updated_at": self._iso(now),
                        "version": 1,
                        **payload,
                    },
                )
                expected = None
            else:
                if draft.effective_at < current.effective_at:
                    raise ContractError(ErrorCode.INVALID_ARGUMENTS)  # history never moves back
                tariff = self._revised(
                    current,
                    **payload,
                    version=current.version + 1,
                    updated_at=self._iso(max(now, current.updated_at)),
                )
                if self._same_terms(tariff, current):
                    return current
                expected = current.version
            try:
                await self._write(command, tariff, expected)
            except ContractError as error:
                if error.error.code is ErrorCode.VERSION_CONFLICT:
                    continue
                raise
            return tariff
        raise ContractError(ErrorCode.VERSION_CONFLICT)

    @staticmethod
    def _same_terms(a: Tariff, b: Tariff) -> bool:
        keys = (
            "unit_tokens",
            "currency",
            "input_rate",
            "output_rate",
            "cached_input_rate",
            "reasoning_output_rate",
            "effective_at",
        )
        return all(getattr(a, key) == getattr(b, key) for key in keys)

    async def tariff_at(
        self, scope: Scope, connection_id: str, model_id: str, at: datetime
    ) -> Tariff | None:
        """The newest tariff version already in force at ``at``; later prices do not apply back."""
        repository = self._repo(Tariff, scope)
        record_id = tariff_record_id(scope, connection_id, model_id)
        current = await repository.get(record_id)
        if current is None:
            return None
        for version in range(current.version, max(0, current.version - MAX_TARIFF_VERSIONS), -1):
            candidate = (
                current
                if version == current.version
                else await repository.get(record_id, version=version)
            )
            if candidate is not None and candidate.effective_at <= at:
                return candidate
        return None

    async def _connection_of(self, scope: Scope, attempt: LLMAttempt) -> str | None:
        assignment = await self._repo(LLMAssignment, scope).get(
            attempt.assignment_id, version=attempt.assignment_version
        )
        return None if assignment is None else assignment.connection_id

    async def _pin(self, scope: Scope, report: AttemptReport) -> Tariff | None:
        model = report.reported_model_id or report.requested_model_id
        return await self._priced(
            scope, report.assignment_id, report.assignment_version, model, report.started_at
        )

    async def _priced(
        self, scope: Scope, assignment_id: str, version: int, model_id: str, at: datetime
    ) -> Tariff | None:
        assignment = await self._repo(LLMAssignment, scope).get(assignment_id, version=version)
        if assignment is None:
            return None
        return await self.tariff_at(scope, assignment.connection_id, model_id, at)

    # ---- cost -------------------------------------------------------------------------

    async def calculate(
        self, command: CommandBase, attempt_id: str, *, repin: bool = False
    ) -> CostCalculation:
        """Store the cost of one attempt; recalculation makes a new version, never an overwrite.

        By default the tariff version pinned on the attempt is used (what applied when it ran).
        ``repin=True`` looks up the version in force at the attempt's start again, which is how a
        tariff entered later for a historical period is applied, as an explicit, visible step.
        """
        scope = command.scope
        attempt = await self.attempt(scope, attempt_id)
        if attempt is None:
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        tariff = await self._tariff_for(scope, attempt, repin)
        cost = compute_cost(attempt.usage, tariff)
        record_id = cost_record_id(scope, attempt_id)
        for _ in range(MAX_RETRIES):
            current = await self._repo(CostCalculation, scope).get(record_id)
            now = self.clock()
            payload = self._cost_payload(attempt, tariff, cost)
            if current is not None and self._same_cost(current, payload):
                return current
            record = (
                self._record(
                    CostCalculation,
                    {
                        "id": record_id,
                        "scope": scope.model_dump(mode="json"),
                        "created_at": self._iso(now),
                        "updated_at": self._iso(now),
                        "version": 1,
                        **payload,
                    },
                )
                if current is None
                else self._revised(
                    current,
                    **payload,
                    version=current.version + 1,
                    updated_at=self._iso(max(now, current.updated_at)),
                )
            )
            try:
                await self._write(command, record, None if current is None else current.version)
            except ContractError as error:
                if error.error.code is ErrorCode.VERSION_CONFLICT:
                    continue
                raise
            return record
        raise ContractError(ErrorCode.VERSION_CONFLICT)

    async def _tariff_for(self, scope: Scope, attempt: LLMAttempt, repin: bool) -> Tariff | None:
        if attempt.tariff_id is not None and attempt.tariff_version is not None and not repin:
            pinned = await self._repo(Tariff, scope).get(
                attempt.tariff_id, version=attempt.tariff_version
            )
            # the provider may report another model than the one requested: price what ran
            if pinned is not None and attempt.reported_model_id in (None, pinned.model_id):
                return pinned
        return await self._priced(
            scope,
            attempt.assignment_id,
            attempt.assignment_version,
            attempt.reported_model_id or attempt.requested_model_id,
            attempt.started_at,
        )

    @staticmethod
    def _cost_payload(attempt: LLMAttempt, tariff: Tariff | None, cost: Cost) -> dict[str, Any]:
        return {
            "attempt_id": attempt.attempt_id,
            "tariff_id": None if tariff is None else tariff.id,
            "tariff_version": None if tariff is None else tariff.version,
            "amount": None if cost.amount is None else money(cost.amount),
            "currency": None if tariff is None or cost.amount is None else tariff.currency,
            "completeness": cost.completeness,
            "provenance": cost.provenance,
        }

    @staticmethod
    def _same_cost(current: CostCalculation, payload: dict[str, Any]) -> bool:
        return all(getattr(current, key) == value for key, value in payload.items())

    async def cost_of(self, scope: Scope, attempt_id: str) -> CostCalculation | None:
        return await self._repo(CostCalculation, scope).get(cost_record_id(scope, attempt_id))

    # ---- reads ------------------------------------------------------------------------

    async def attempts(self, scope: Scope) -> list[LLMAttempt]:
        return await self._all(LLMAttempt, scope)

    async def costs(self, scope: Scope) -> list[CostCalculation]:
        return await self._all(CostCalculation, scope)

    async def connection_of(self, scope: Scope, attempt: LLMAttempt) -> str | None:
        return await self._connection_of(scope, attempt)


def decimal_amount(cost: CostCalculation) -> Decimal | None:
    return None if cost.amount is None else Decimal(cost.amount)


__all__ = [
    "AttemptWritten",
    "TariffDraft",
    "UsageLedger",
    "attempt_record_id",
    "cost_record_id",
    "decimal_amount",
    "merge_usage",
    "tariff_record_id",
]
