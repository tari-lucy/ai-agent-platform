"""Cost of one attempt from its usage and one tariff version. Pure; exact decimals.

Cached input is a part of input and reasoning output is a part of output, so a priced category is
never added on top of its parent: ``regular = total - subset``. A missing token count, rate or
tariff never becomes zero: the result is partial (a lower bound of what is known) or unknown.
"""

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, localcontext
from typing import Literal

from ai_agent_contracts.llm import Tariff, TokenUsage

SCALE = Decimal(1).scaleb(-10)

type Completeness = Literal["complete", "partial", "unknown"]
type Provenance = Literal["provider_usage", "local_estimate", "unknown"]


@dataclass(frozen=True)
class Cost:
    amount: Decimal | None
    completeness: Completeness
    provenance: Provenance
    missing: tuple[str, ...]


def money(value: Decimal) -> str:
    """Contract Money string: plain decimal, no exponent, no trailing zeros."""
    text = format(value.quantize(SCALE, rounding=ROUND_HALF_UP), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def rate_of(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def compute_cost(usage: TokenUsage, tariff: Tariff | None) -> Cost:
    provenance_of: dict[str, Provenance] = {
        "provider": "provider_usage",
        "local_estimate": "local_estimate",
        "unknown": "unknown",
    }
    provenance = provenance_of[usage.source]
    if tariff is None:
        return Cost(None, "unknown", provenance, ("tariff",))
    if usage.input_tokens is None and usage.output_tokens is None:  # also every unknown usage
        return Cost(None, "unknown", provenance, ("usage",))

    input_rate, output_rate = rate_of(tariff.input_rate), rate_of(tariff.output_rate)
    cached_rate = rate_of(tariff.cached_input_rate) or input_rate
    reasoning_rate = rate_of(tariff.reasoning_output_rate) or output_rate
    cached = usage.cached_input_tokens or 0
    reasoning = usage.reasoning_output_tokens or 0
    parts: list[tuple[str, int | None, Decimal | None]] = []
    parts.append(
        ("input", None if usage.input_tokens is None else usage.input_tokens - cached, input_rate)
    )
    parts.append(("cached_input", cached, cached_rate))
    parts.append(
        (
            "output",
            None if usage.output_tokens is None else usage.output_tokens - reasoning,
            output_rate,
        )
    )
    parts.append(("reasoning_output", reasoning, reasoning_rate))

    total = Decimal(0)
    priced = 0
    missing: list[str] = []
    with localcontext() as context:
        context.prec = 50
        for name, tokens, rate in parts:
            if tokens is None:
                missing.append(f"{name}_tokens")
            elif tokens == 0:
                continue  # nothing to price: a missing rate does not matter for zero tokens
            elif rate is None:
                missing.append(f"{name}_rate")
            else:
                total += Decimal(tokens) * rate / Decimal(tariff.unit_tokens)
                priced += 1
    if missing and not priced:
        return Cost(None, "unknown", provenance, tuple(missing))
    complete = not missing and usage.completeness == "complete" and provenance != "unknown"
    return Cost(total, "complete" if complete else "partial", provenance, tuple(missing))
