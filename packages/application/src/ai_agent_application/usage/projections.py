"""Safe read projections of the ledger: ids, statuses, counts, token and money totals only.

No prompt, reply, endpoint, header or credential exists in the records, so none can appear here.
Known totals are never presented as complete when some attempts have unknown usage or cost:
``complete`` is False and the unknown/partial counters say how much is missing. Money is summed
per currency only; different currencies are never added.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ai_agent_contracts.common import Scope
from ai_agent_contracts.llm import CostCalculation, LLMAttempt

from .ledger import UsageLedger, decimal_amount
from .pricing import money


@dataclass
class Totals:
    attempts: int = 0
    operations: int = 0
    by_status: Counter[str] = field(default_factory=Counter)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_output_tokens: int = 0
    unknown_usage: int = 0
    partial_usage: int = 0

    @property
    def retries(self) -> int:
        return self.attempts - self.operations

    @property
    def complete(self) -> bool:
        return self.unknown_usage == 0 and self.partial_usage == 0


@dataclass
class CurrencyTotals:
    amount: Decimal = Decimal(0)
    complete: int = 0
    partial: int = 0

    @property
    def amount_text(self) -> str:
        return money(self.amount)


@dataclass
class Summary:
    totals: Totals = field(default_factory=Totals)
    by_mode: dict[str, Totals] = field(default_factory=dict)
    by_role: dict[str, Totals] = field(default_factory=dict)
    by_model: dict[str, Totals] = field(default_factory=dict)
    by_connection: dict[str, Totals] = field(default_factory=dict)
    cost: dict[str, CurrencyTotals] = field(default_factory=dict)
    unknown_cost: int = 0

    @property
    def complete(self) -> bool:
        return (
            self.totals.complete
            and self.unknown_cost == 0
            and all(item.partial == 0 for item in self.cost.values())
        )


@dataclass(frozen=True)
class AttemptView:
    attempt: LLMAttempt
    cost: CostCalculation | None


def _add(totals: Totals, attempt: LLMAttempt, operations: set[str]) -> None:
    totals.attempts += 1
    if attempt.operation_id not in operations:
        operations.add(attempt.operation_id)
        totals.operations += 1
    totals.by_status[attempt.status] += 1
    usage = attempt.usage
    if usage.completeness == "unknown" or usage.source == "unknown":
        totals.unknown_usage += 1
    elif usage.completeness == "partial":
        totals.partial_usage += 1
    # subsets are reported next to their parents, never added into them
    totals.input_tokens += usage.input_tokens or 0
    totals.output_tokens += usage.output_tokens or 0
    totals.cached_input_tokens += usage.cached_input_tokens or 0
    totals.reasoning_output_tokens += usage.reasoning_output_tokens or 0


async def summarise(
    ledger: UsageLedger,
    scopes: list[Scope],
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    role_id: str | None = None,
    model_id: str | None = None,
    connection_id: str | None = None,
) -> Summary:
    """Work and test scopes are listed separately (``by_mode``) and also in the common total."""
    summary = Summary()
    seen: dict[int, set[str]] = {}
    for scope in scopes:
        costs = {item.attempt_id: item for item in await ledger.costs(scope)}
        for attempt in await ledger.attempts(scope):
            model = attempt.reported_model_id or attempt.requested_model_id
            if (
                (since is not None and attempt.started_at < since)
                or (until is not None and attempt.started_at >= until)
                or (role_id is not None and attempt.role_id != role_id)
                or (
                    model_id is not None
                    and (attempt.reported_model_id or attempt.requested_model_id) != model_id
                )
            ):
                continue
            connection = await ledger.connection_of(scope, attempt) or "unknown"
            if connection_id is not None and connection != connection_id:
                continue
            for table, key in (
                (None, ""),
                (summary.by_mode, scope.memory_mode),
                (summary.by_role, attempt.role_id),
                (summary.by_model, model),
                (summary.by_connection, connection),
            ):
                bucket = summary.totals if table is None else table.setdefault(key, Totals())
                _add(bucket, attempt, seen.setdefault(id(bucket), set()))
            cost = costs.get(attempt.attempt_id)
            if cost is None or cost.completeness == "unknown" or cost.currency is None:
                summary.unknown_cost += 1
                continue
            amount = decimal_amount(cost)
            assert amount is not None
            row = summary.cost.setdefault(cost.currency, CurrencyTotals())
            row.amount += amount
            if cost.completeness == "complete":
                row.complete += 1
            else:
                row.partial += 1
    return summary


async def history(ledger: UsageLedger, scope: Scope, *, limit: int = 100) -> list[AttemptView]:
    """Newest first; every attempt, including cancelled and unknown ones."""
    costs = {item.attempt_id: item for item in await ledger.costs(scope)}
    ordered = sorted(await ledger.attempts(scope), key=lambda a: (a.started_at, a.id), reverse=True)
    return [AttemptView(item, costs.get(item.attempt_id)) for item in ordered[:limit]]
