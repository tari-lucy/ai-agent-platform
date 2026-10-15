"""Closed extraction schemas and reports (M3-01). The model proposes; the validator decides."""

from collections import Counter
from dataclasses import dataclass, field
from typing import Annotated, Literal

from ai_agent_contracts.common import Contract, Id
from pydantic import Field

MAX_PROPOSALS = 20
MAX_EVIDENCE = 5
MAX_CLAIM_CHARS = 500
MIN_QUOTE_CHARS = 3
MAX_QUOTE_CHARS = 500

type KnowledgeType = Literal["observed", "reported", "canon", "hypothesis", "interpretation"]


class Evidence(Contract):
    source_id: Id
    # verbatim text of that source that supports the claim; checked against the stored content
    quote: Annotated[
        str, Field(strict=True, min_length=MIN_QUOTE_CHARS, max_length=MAX_QUOTE_CHARS)
    ]


class Proposal(Contract):
    subject_id: Id
    claim: Annotated[str, Field(strict=True, min_length=1, max_length=MAX_CLAIM_CHARS)]
    proposed_type: KnowledgeType
    confidence: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    evidence: list[Evidence] = Field(min_length=1, max_length=MAX_EVIDENCE)
    contradicts: list[Id] = Field(default_factory=list, max_length=MAX_EVIDENCE)


class ExtractionResult(Contract):
    proposals: list[Proposal] = Field(default_factory=list, max_length=MAX_PROPOSALS)


class SummaryResult(Contract):
    content: Annotated[str, Field(strict=True, min_length=1, max_length=4000)]


@dataclass(frozen=True)
class Accepted:
    claim_id: str
    subject_id: str
    knowledge_type: str
    confidence: float
    downgraded_from: str | None
    independent_groups: int
    source_ids: tuple[str, ...]
    duplicate: bool = False


@dataclass(frozen=True)
class Rejected:
    index: int
    reason: str


@dataclass(frozen=True)
class Skipped:
    source_id: str
    reason: str


@dataclass(frozen=True)
class ExtractionReport:
    status: Literal["completed", "nothing", "failed", "cancelled", "not_admitted"]
    accepted: tuple[Accepted, ...] = ()
    rejected: tuple[Rejected, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    failure: str | None = None
    attempts: int = 0
    notes: tuple[str, ...] = field(default=())
    offered: int = 0  # sources that actually reached the model
    proposed: int = 0  # proposals the model returned before validation

    @property
    def outcome(self) -> str:
        """Why the result looks the way it does; the empty answer is told from a rejection."""
        if self.status in ("failed", "cancelled", "not_admitted"):
            return self.status
        if self.offered == 0:
            return "no_usable_sources"
        if self.proposed == 0:
            return "model_returned_nothing"
        return "accepted" if self.accepted else "all_rejected"

    def diagnosis(self) -> str:
        """Counts and fixed reason codes only: no prompt, answer, quote, id or endpoint."""
        reasons = Counter(r.reason for r in self.rejected)
        skipped = Counter(s.reason for s in self.skipped)
        parts = [
            f"outcome={self.outcome}",
            f"offered={self.offered}",
            f"proposed={self.proposed}",
            f"accepted={sum(not a.duplicate for a in self.accepted)}",
            f"duplicate={sum(a.duplicate for a in self.accepted)}",
            f"rejected={_codes(reasons)}",
            f"skipped={_codes(skipped)}",
            f"attempts={self.attempts}",
        ]
        if self.failure is not None:
            parts.append(f"failure={self.failure}")
        return " ".join(parts)


def _codes(counts: Counter[str]) -> str:
    return ",".join(f"{code}:{counts[code]}" for code in sorted(counts)) or "-"
