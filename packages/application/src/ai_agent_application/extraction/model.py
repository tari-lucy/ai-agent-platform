"""Closed extraction schemas and reports (M3-01). The model proposes; the validator decides."""

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
