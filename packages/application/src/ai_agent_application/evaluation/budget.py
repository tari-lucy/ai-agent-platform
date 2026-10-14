"""Budget guard and the explicit authorisation a real-model run needs.

A real paid call is possible only with an ``Authorisation`` the operator filled in on purpose: the
switch, a hard ceiling in one currency, the exact tariff and the labels of the models. The guard
refuses a call *before* it is made when the worst case would cross the ceiling, and it never turns
an unknown usage into zero: those calls are counted apart and stop the run after a small limit,
because a ceiling cannot be enforced blind.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ai_agent_contracts.llm import TokenUsage

CHARS_PER_TOKEN = 3  # deliberately pessimistic for Russian text: more tokens than a 4-char guess
MAX_UNKNOWN_CALLS = 3


class RealRunRefused(Exception):
    """A real-model run was requested without a complete authorisation."""


@dataclass(frozen=True)
class Price:
    """Tariff of one model; rates are per ``unit_tokens`` in ``currency`` (decimal strings)."""

    currency: str
    input_rate: Decimal
    output_rate: Decimal
    unit_tokens: int = 1_000_000

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper() or not self.currency.isalpha():
            raise ValueError("CURRENCY_INVALID")
        if self.unit_tokens < 1 or self.input_rate < 0 or self.output_rate < 0:
            raise ValueError("PRICE_INVALID")

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        unit = Decimal(self.unit_tokens)
        return (
            Decimal(input_tokens) / unit * self.input_rate
            + Decimal(output_tokens) / unit * self.output_rate
        )


@dataclass(frozen=True)
class Authorisation:
    """What the operator permits. Nothing here is a secret; the key stays in the local ``.env``."""

    real_calls: bool
    ceiling: Decimal
    price: Price
    model_label: str
    max_output_tokens: int = 800
    note: str = ""

    def validate(self) -> None:
        if not self.real_calls:
            raise RealRunRefused("REAL_CALLS_NOT_ALLOWED")
        if self.ceiling <= 0:
            raise RealRunRefused("CEILING_REQUIRED")
        if not self.model_label.strip():
            raise RealRunRefused("MODEL_LABEL_REQUIRED")
        if self.max_output_tokens < 1:
            raise RealRunRefused("OUTPUT_LIMIT_REQUIRED")


@dataclass
class BudgetGuard:
    """Tracks spend by provenance. ``ceiling is None`` means dry-run: nothing is ever charged."""

    price: Price | None = None
    ceiling: Decimal | None = None
    max_output_tokens: int = 800
    spent_provider: Decimal = Decimal(0)
    spent_estimate: Decimal = Decimal(0)
    calls: int = 0
    unknown_calls: int = 0
    refused_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    reason: str = ""
    _reserved: Decimal = field(default=Decimal(0), repr=False)

    @property
    def spent(self) -> Decimal:
        return self.spent_provider + self.spent_estimate

    @property
    def exhausted(self) -> bool:
        return bool(self.reason)

    def worst_case(self, prompt_chars: int) -> Decimal:
        """Upper bound of one call: the whole prompt as input and the full output limit."""
        assert self.price is not None
        return self.price.cost(-(-prompt_chars // CHARS_PER_TOKEN), self.max_output_tokens)

    def admit(self, prompt_chars: int) -> bool:
        """True when the call may be made. A refusal is sticky: the run stops, no retry games."""
        if self.exhausted:
            self.refused_calls += 1
            return False
        if self.ceiling is None:
            return True
        if self.unknown_calls >= MAX_UNKNOWN_CALLS:
            return self._refuse("UNKNOWN_USAGE_LIMIT")
        if self.spent + self.worst_case(prompt_chars) > self.ceiling:
            return self._refuse("CEILING_WOULD_BE_EXCEEDED")
        return True

    def _refuse(self, reason: str) -> bool:
        self.reason = reason
        self.refused_calls += 1
        return False

    def charge(self, usage: TokenUsage | None, prompt_chars: int, output_chars: int) -> None:
        """Record one finished call. A missing or unknown usage is never priced as zero."""
        self.calls += 1
        if usage is None or usage.input_tokens is None or usage.output_tokens is None:
            self.unknown_calls += 1
            if self.price is not None and self.ceiling is not None:
                # keep the ceiling honest: book the worst case for a call we cannot measure
                self.spent_estimate += self.worst_case(prompt_chars)
            return
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        if self.price is None:
            return
        amount = self.price.cost(usage.input_tokens, usage.output_tokens)
        if usage.source == "provider":
            self.spent_provider += amount
        else:
            self.spent_estimate += amount

    def as_dict(self) -> dict[str, Any]:
        return {
            "currency": None if self.price is None else self.price.currency,
            "ceiling": None if self.ceiling is None else str(self.ceiling),
            "spent_provider_usage": str(self.spent_provider),
            "spent_estimate_or_worst_case": str(self.spent_estimate),
            "calls": self.calls,
            "unknown_usage_calls": self.unknown_calls,
            "refused_calls": self.refused_calls,
            "input_tokens_known": self.input_tokens,
            "output_tokens_known": self.output_tokens,
            "stopped_reason": self.reason or None,
        }
