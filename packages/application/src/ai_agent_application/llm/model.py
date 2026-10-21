"""Provider-neutral request, event and result shapes of the LLM gateway (M2-04)."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from ai_agent_contracts.common import Contract, Id, Version
from ai_agent_contracts.llm import TokenUsage
from pydantic import BaseModel, Field

MAX_MESSAGES = 200
MAX_PROMPT_CHARS = 400_000
MAX_OUTPUT_CHARS = 200_000

type Role = Literal["system", "user", "assistant"]
type AttemptStatus = Literal["completed", "failed", "cancelled", "incomplete", "unknown"]

UNKNOWN_USAGE = TokenUsage(
    input_tokens=None, output_tokens=None, source="unknown", completeness="unknown"
)


class GenerationParameters(Contract):
    """Closed ``llm.generation-parameters`` payload of an LLMAssignment."""

    max_output_tokens: Annotated[int, Field(strict=True, ge=1, le=65_536)]
    timeout_seconds: Annotated[float, Field(gt=0, le=300)] = 60.0
    temperature: Annotated[float, Field(ge=0, le=2)] | None = None
    top_p: Annotated[float, Field(gt=0, le=1)] | None = None


@dataclass(frozen=True)
class ChatMessage:
    role: Role
    content: str


@dataclass(frozen=True)
class ResolvedAssignment:
    """A role bound to a verified connection and exact model; built by ``resolve_assignment``."""

    assignment_id: str
    assignment_version: Version
    role_id: str
    connection_id: str
    adapter_id: str
    model_id: str
    endpoint_setting: str
    credential_setting: str
    parameters: GenerationParameters
    capability_verification: Literal["unverified", "verified", "failed"]
    fallback: ResolvedAssignment | None = None


class Failure(StrEnum):
    """Why an attempt did not complete. Carries no remote text, headers or endpoint."""

    NOT_CONFIGURED = "NOT_CONFIGURED"
    AUTH = "AUTH"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    TRANSIENT = "TRANSIENT"
    REJECTED = "REJECTED"
    PROTOCOL = "PROTOCOL"
    TRUNCATED = "TRUNCATED"
    FILTERED = "FILTERED"
    UNSUPPORTED = "UNSUPPORTED"
    MALFORMED_RESULT = "MALFORMED_RESULT"
    CANCELLED = "CANCELLED"
    NOT_ADMITTED = "NOT_ADMITTED"


RETRYABLE = frozenset({Failure.RATE_LIMITED, Failure.TIMEOUT, Failure.TRANSIENT})
FALLBACK_ELIGIBLE = RETRYABLE | {Failure.NOT_CONFIGURED, Failure.AUTH, Failure.UNSUPPORTED}


class ProviderError(Exception):
    """Provider failure in neutral form. Never built from remote error text."""

    def __init__(self, failure: Failure, retry_after: float | None = None) -> None:
        super().__init__(failure.value)
        self.failure = failure
        self.retry_after = retry_after


@dataclass(frozen=True)
class Started:
    reported_model: str | None


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class Finished:
    reason: Literal["stop", "length", "content_filter", "tool_calls", "other"]


@dataclass(frozen=True)
class UsageReported:
    """Latest usage of the provider; replaces, never adds to, earlier reports."""

    usage: TokenUsage


type ProviderEvent = Started | Delta | Finished | UsageReported


@dataclass(frozen=True)
class ProviderCall:
    assignment: ResolvedAssignment
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True)
class GenerationRequest:
    operation_id: Id
    role_id: Id
    messages: tuple[ChatMessage, ...]
    # Optional closed schema: the final text must be one JSON document matching it.
    result_model: type[BaseModel] | None = None
    # Optional per-call ceiling. May only lower the configured role assignment.
    max_output_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.max_output_tokens is not None and (
            type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 65536
        ):
            raise ValueError("OUTPUT_TOKEN_LIMIT")
        if not 1 <= len(self.messages) <= MAX_MESSAGES:
            raise ValueError("MESSAGES_BOUNDS")
        if sum(len(item.content) for item in self.messages) > MAX_PROMPT_CHARS:
            raise ValueError("PROMPT_TOO_LARGE")


@dataclass(frozen=True)
class AttemptReport:
    """One paid-or-attempted provider call; the input of the M2-05 ledger."""

    operation_id: str
    attempt_id: str
    role_id: str
    assignment_id: str
    assignment_version: int
    connection_id: str
    requested_model_id: str
    reported_model_id: str | None
    status: AttemptStatus
    failure: Failure | None
    started_at: datetime
    completed_at: datetime
    usage: TokenUsage
    delivered_chars: int


@dataclass(frozen=True)
class Generation:
    """Terminal result. ``text``/``parsed`` exist only for a completed generation."""

    operation_id: str
    status: Literal["completed", "failed", "cancelled", "incomplete"]
    failure: Failure | None
    text: str | None
    parsed: BaseModel | None
    preview: str
    attempts: tuple[AttemptReport, ...]
    used_fallback: bool
    assignment_id: str | None
