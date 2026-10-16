"""Signals, the model's closed decision schema and the controller's results (M2-06)."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Literal

from ai_agent_contracts.common import Contract, Id, ShortText, SourceRef, Text
from ai_agent_contracts.control import ActionProposal
from ai_agent_contracts.llm import TokenUsage
from ai_agent_contracts.states import ActivityState, RunState
from pydantic import Field

MAX_REPLY_CHARS = 2000
MAX_SIGNAL_TEXT = 4000

type SignalKind = Literal["operator_message", "viewer_message", "obligation_due"]
type Autonomy = Literal["observation", "autonomous"]


@dataclass(frozen=True)
class SourceRights:
    """Decided by the adapter/policy that received the message, never by the model."""

    read: bool
    api_transfer: bool
    public_disclosure: bool = False


@dataclass(frozen=True)
class Signal:
    """One thing that may deserve attention. Text is data, never an instruction."""

    event_id: Id
    kind: SignalKind
    occurred_at: datetime
    text: str
    platform_id: Id | None = None
    account_id: str | None = None
    display_name: str | None = None
    directed: bool = False  # addressed to the character
    rights: SourceRights = SourceRights(read=True, api_transfer=False)
    dedup_key: str | None = None
    ttl_seconds: float | None = None
    # obligation signals refer to an exact promise, not to a search result
    promise_id: Id | None = None
    person_id: Id | None = None
    attempts: int = 0

    @property
    def key(self) -> str:
        return self.dedup_key or self.event_id


class QuestionIntent(Contract):
    topic_key: Annotated[ShortText, Field(min_length=1)]
    text: Annotated[Text, Field(min_length=1, max_length=MAX_REPLY_CHARS)]


class PromiseIntent(Contract):
    promise_key: Id
    content: Annotated[Text, Field(min_length=1)]
    completion_criterion: Annotated[Text, Field(min_length=1)]
    topic_key: Annotated[ShortText, Field(min_length=1)]
    deadline_days: Annotated[int, Field(strict=True, ge=1, le=365)] | None = None


class ActionIntent(Contract):
    capability_id: Id
    goal: Annotated[Text, Field(min_length=1)]
    expected_outcome: Annotated[Text, Field(min_length=1)]
    applicability: Annotated[Text, Field(min_length=1)]
    priority: Annotated[int, Field(strict=True, ge=0, le=100)] = 50


class OfferDecision(Contract):
    """The character's explicit choice on one shown internal offer, by exact id and version."""

    opportunity_id: Id
    version: Annotated[int, Field(strict=True, ge=1)]
    decision: Literal["accept", "decline"]


class DialogueDecision(Contract):
    """What the model may propose. Everything is validated and executed by the controller."""

    reply: Annotated[Text, Field(max_length=MAX_REPLY_CHARS)] | None = None
    question: QuestionIntent | None = None
    promise: PromiseIntent | None = None
    action: ActionIntent | None = None
    silence_reason: ShortText | None = None
    offer: OfferDecision | None = None  # optional: older answers stay valid


@dataclass(frozen=True)
class ControlSnapshot:
    """Supervisor view at one instant; ``admitted`` is the trusted epoch/lease verification."""

    run_state: RunState
    activity: ActivityState
    autonomy: Autonomy
    admitted: bool


@dataclass(frozen=True)
class SectionInfo:
    name: str
    included: int
    omitted: int
    truncated: bool = False


@dataclass(frozen=True)
class ContextManifest:
    """Why the model saw what it saw: panel-safe, no text."""

    sections: tuple[SectionInfo, ...]
    sources: tuple[SourceRef, ...]
    estimated_tokens: int
    omitted_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DraftReply:
    """A proposed utterance. Becoming public is the gateway's job after moderation and delivery."""

    utterance_id: Id
    text: str
    content_hash: str
    audience_id: Id
    person_id: Id | None
    question_topic: str | None = None
    promise: PromiseIntent | None = None
    evidence: tuple[SourceRef, ...] = ()
    initiative: bool = False


type OutcomeStatus = Literal["drafted", "silent", "held", "blocked", "cancelled", "failed"]


@dataclass(frozen=True)
class DialogueOutcome:
    event_id: str
    status: OutcomeStatus
    reason: str | None = None
    draft: DraftReply | None = None
    proposals: tuple[ActionProposal, ...] = ()
    dropped: tuple[str, ...] = ()  # parts of the decision refused by validation, with reason codes
    manifest: ContextManifest | None = None
    attempts: int = 0
    usage: tuple[TokenUsage, ...] = ()
    person_id: str | None = None


@dataclass(frozen=True)
class Goal:
    """The controller's current goal; a new signal does not replace it by itself."""

    kind: str
    priority: float
    reason: str
    since: datetime
    person_id: str | None = None


@dataclass
class AttentionStats:
    accepted: int = 0
    coalesced: int = 0
    dropped_budget: int = 0
    dropped_full: int = 0
    expired: int = 0
    deferred: int = 0
    selected: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
