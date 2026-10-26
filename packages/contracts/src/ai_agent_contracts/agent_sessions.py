"""Logical workplace sessions (R1), independent of a run or an OpenCode process.

The activity owner stores links and progress, never a second message body store.
Message content stays with the existing memory Source owner. None of these
records grants permission or authorises execution after restart.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    ArtifactRef,
    Contract,
    Counter,
    Id,
    Scope,
    ShortText,
    SourceRef,
    Text,
    Version,
    VersionedRecord,
)


class SessionTicketRef(Contract):
    ticket_id: Id
    version: Version


class SessionReplyOrigin(Contract):
    session_id: Id
    generation: Version
    through_sequence: Counter
    after_sequence: Counter = 0
    context_files: list[ArtifactRef] = Field(default_factory=list, max_length=200)
    tickets: list[SessionTicketRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.after_sequence > self.through_sequence or (
            self.after_sequence == self.through_sequence and not self.tickets
        ):
            raise ValueError("SESSION_REPLY_PREFIX")
        if len({ref.ticket_id for ref in self.tickets}) != len(self.tickets):
            raise ValueError("SESSION_TICKET_DUPLICATE")
        return self


class SessionReplyRef(Contract):
    """An undelivered draft owned by Gateway/Channel, never a remembered message."""

    utterance_id: Id
    version: Version
    revision: Version
    scope: Scope
    generation: Version
    through_sequence: Counter


class WorkSession(VersionedRecord):
    title: ShortText = Field(min_length=1)
    state: Literal["open", "waiting", "closed", "invalidated"] = "open"
    # Changes on context invalidation. A checkpoint from another generation is unusable.
    generation: Version = 1
    last_sequence: Counter = 0
    processed_sequence: Counter = 0
    task_ids: list[Id] = Field(default_factory=list, max_length=50)
    waiting_ticket_ids: list[Id] = Field(default_factory=list, max_length=20)
    # Revisions actually considered in an acknowledged answer, not merely scheduled.
    ticket_revisions: list[SessionTicketRef] = Field(default_factory=list, max_length=20)
    context_sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    context_files: list[ArtifactRef] = Field(default_factory=list, max_length=200)
    checkpoint: ArtifactRef | None = None
    pending_reply: SessionReplyRef | None = None
    # Why the owner stopped the conversation: an unprocessed operator message was revoked,
    # expired or deleted, so it is never executed. Continue in a new conversation.
    invalidated_reason: Literal["message_unavailable"] | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.scope.agent_id is not None or self.scope.run_id is not None:
            raise ValueError("STABLE_SCOPE_REQUIRED")
        if not self.title.strip() or self.processed_sequence > self.last_sequence:
            raise ValueError("SESSION_PROGRESS")
        if len(set(self.task_ids)) != len(self.task_ids) or len(
            set(self.waiting_ticket_ids)
        ) != len(self.waiting_ticket_ids):
            raise ValueError("SESSION_LINK_DUPLICATE")
        if self.state == "waiting" and not self.waiting_ticket_ids:
            raise ValueError("SESSION_WAIT_REQUIRED")
        if len({ref.ticket_id for ref in self.ticket_revisions}) != len(self.ticket_revisions):
            raise ValueError("SESSION_TICKET_DUPLICATE")
        if self.state == "invalidated" and self.checkpoint is not None:
            raise ValueError("INVALIDATED_CHECKPOINT")
        if self.invalidated_reason is not None and self.state != "invalidated":
            raise ValueError("INVALIDATED_REASON")
        if self.pending_reply is not None and (
            self.state in ("closed", "invalidated")
            or self.pending_reply.scope.partition != self.scope.partition
            or self.pending_reply.generation != self.generation
            or not self.processed_sequence
            <= self.pending_reply.through_sequence
            <= self.last_sequence
        ):
            raise ValueError("SESSION_PENDING_REPLY")
        refs: list[SourceRef | ArtifactRef] = [*self.context_sources, *self.context_files]
        if self.checkpoint is not None:
            refs.append(self.checkpoint)
        if any(ref.scope.partition != self.scope.partition for ref in refs):
            raise ValueError("SESSION_REFERENCE_SCOPE")
        return self


class WorkSessionMessage(VersionedRecord):
    """One ordered reference to a Source, not another private text/history copy."""

    session_id: Id
    sequence: int = Field(strict=True, ge=1)
    # "wake": the agent's own wake-up (W7), an input like an operator message, never one.
    speaker: Literal["operator", "character", "wake"]
    source: SourceRef
    reply_to_sequence: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.scope.agent_id is not None or self.scope.run_id is not None:
            raise ValueError("STABLE_SCOPE_REQUIRED")
        if self.source.scope.partition != self.scope.partition:
            raise ValueError("SESSION_REFERENCE_SCOPE")
        if self.reply_to_sequence is not None and self.reply_to_sequence >= self.sequence:
            raise ValueError("SESSION_REPLY_ORDER")
        return self


class WakeTimer(VersionedRecord):
    """A wake-up the agent set for itself (W7): data, no right, no start after restart.

    The platform fires it only while the run is active and autonomous, outside quiet hours
    and within the hourly cap; firing appends a ``wake`` message to its session.
    """

    session_id: Id
    due_at: UTC
    reason: ShortText = Field(min_length=1)
    state: Literal["pending", "fired", "cancelled"] = "pending"
    fired_sequence: int | None = Field(default=None, strict=True, ge=1)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.scope.agent_id is not None or self.scope.run_id is not None:
            raise ValueError("STABLE_SCOPE_REQUIRED")
        if (self.state == "fired") != (self.fired_sequence is not None):
            raise ValueError("WAKE_FIRED_SEQUENCE")
        return self


class WorkSessionOpenArguments(Contract):
    title: ShortText = Field(min_length=1)
    task_id: Id | None = None

    @model_validator(mode="after")
    def not_blank(self) -> Self:
        if not self.title.strip():
            raise ValueError("EMPTY_TITLE")
        return self


class WorkSessionMessageArguments(Contract):
    """Operator text only. Identity, source and sequence are assigned by trusted owners."""

    session_id: Id
    text: Text = Field(min_length=1, max_length=16000)

    @model_validator(mode="after")
    def not_blank(self) -> Self:
        if not self.text.strip():
            raise ValueError("EMPTY_MESSAGE")
        return self


class WorkSessionRenameArguments(Contract):
    session_id: Id
    title: ShortText = Field(min_length=1)

    @model_validator(mode="after")
    def not_blank(self) -> Self:
        if not self.title.strip():
            raise ValueError("EMPTY_TITLE")
        return self


class WorkSessionCloseArguments(Contract):
    session_id: Id
