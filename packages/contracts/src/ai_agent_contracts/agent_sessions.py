"""Logical workplace sessions (R1), independent of a run or an OpenCode process.

The activity owner stores links and progress, never a second message body store.
Message content stays with the existing memory Source owner. None of these
records grants permission or authorises execution after restart.
"""

from typing import Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    ArtifactRef,
    Contract,
    Counter,
    Id,
    ShortText,
    SourceRef,
    Text,
    Version,
    VersionedRecord,
)


class WorkSession(VersionedRecord):
    title: ShortText = Field(min_length=1)
    state: Literal["open", "waiting", "closed", "invalidated"] = "open"
    # Changes on context invalidation. A checkpoint from another generation is unusable.
    generation: Version = 1
    last_sequence: Counter = 0
    processed_sequence: Counter = 0
    task_ids: list[Id] = Field(default_factory=list, max_length=50)
    waiting_ticket_ids: list[Id] = Field(default_factory=list, max_length=20)
    context_sources: list[SourceRef] = Field(default_factory=list, max_length=200)
    context_files: list[ArtifactRef] = Field(default_factory=list, max_length=200)
    checkpoint: ArtifactRef | None = None

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
        if self.state == "invalidated" and self.checkpoint is not None:
            raise ValueError("INVALIDATED_CHECKPOINT")
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
    speaker: Literal["operator", "character"]
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


class WorkSessionCloseArguments(Contract):
    session_id: Id
