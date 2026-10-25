"""Canonical owner payloads and projections for the private panel."""

from typing import Annotated, Literal, Self

from pydantic import Field, JsonValue, model_validator

from .api import Receipt
from .common import (
    UTC,
    Confidence,
    Contract,
    Digest,
    Id,
    Money,
    Scope,
    ShortText,
    Version,
    VersionedRecord,
)
from .extensions import ExtensionRegistry
from .memory import Source
from .persona import SettingsTransferOperation
from .states import OperationState

MarkdownText = Annotated[str, Field(strict=True, min_length=1, max_length=30000)]


class PersonaCreateArguments(Contract):
    markdown: MarkdownText


class PersonaValidateArguments(Contract):
    markdown: MarkdownText


class PersonaApplyArguments(Contract):
    markdown: MarkdownText
    revision_digest: Digest


class PersonaCopyArguments(Contract):
    source_scope: Scope
    source_version: Version


class ApiLimits(Contract):
    id: Literal["api-limits"] = "api-limits"
    scope: Scope
    version: Version
    attempt_limit: int = Field(strict=True, ge=1, le=1_000_000)
    minimum_limit: int = Field(strict=True, ge=1)
    period: Literal["lifetime", "utc_day"]


class ApiLimitArguments(Contract):
    attempt_limit: int = Field(strict=True, ge=1, le=1_000_000)


class ApiLimitChange(VersionedRecord):
    operator_id: Id
    request_id: Id
    fingerprint: Digest
    attempt_limit: int = Field(strict=True, ge=1, le=1_000_000)
    period: Literal["lifetime", "utc_day"]
    budget_version: Version
    expires_at: UTC


class TariffCreateArguments(Contract):
    connection_id: Id
    model_id: Id
    previous_version: int = Field(strict=True, ge=0)
    unit_tokens: int = Field(strict=True, ge=1, le=1_000_000_000)
    currency: str = Field(strict=True, pattern=r"^[A-Z]{3}$")
    effective_at: UTC
    input_rate: Money | None = None
    output_rate: Money | None = None
    cached_input_rate: Money | None = None
    reasoning_output_rate: Money | None = None

    @model_validator(mode="after")
    def has_rate(self) -> Self:
        if self.input_rate is None and self.output_rate is None:
            raise ValueError("RATE_REQUIRED")
        return self


class PromiseCloseArguments(Contract):
    """The operator closes an exact promise; the history keeps every earlier version.

    `fulfilled` is grounded by the operator's own stated result (a new operator source), never
    by the character's reply; `withdrawn` needs a reason; `unknown` keeps it owed.
    """

    promise_id: Id
    outcome: Literal["fulfilled", "withdrawn", "unknown"]
    closure_result: str | None = Field(default=None, strict=True, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def explained(self) -> Self:
        if self.closure_result is not None and not self.closure_result.strip():
            raise ValueError("EMPTY_CLOSURE")
        if self.outcome != "unknown" and self.closure_result is None:
            raise ValueError("CLOSURE_REQUIRED")
        return self


class MemoryCorrectArguments(Contract):
    kind: Literal["claim", "summary"]
    record_id: Id
    reason: str = Field(strict=True, min_length=1, max_length=1000)
    content: str | None = Field(default=None, strict=True, min_length=1, max_length=4000)
    confidence: Confidence | None = None

    @model_validator(mode="after")
    def meaningful(self) -> Self:
        if not self.reason.strip() or (self.content is not None and not self.content.strip()):
            raise ValueError("EMPTY_CORRECTION")
        if self.content is None and self.confidence is None:
            raise ValueError("EMPTY_CORRECTION")
        if self.kind == "summary" and self.confidence is not None:
            raise ValueError("CLAIM_CONFIDENCE_ONLY")
        return self


class MemorySelectionArguments(Contract):
    # Explicit bounded source selection, including a conversation selected by its owner.
    source_ids: list[Id] = Field(min_length=1, max_length=200)
    session_ref: tuple[Id, Version] | None = None


class MemorySourceExport(Contract):
    schema_id: Literal["memory.source-export"] = "memory.source-export"
    schema_version: Literal["1.0"] = "1.0"
    exported_at: UTC
    source: Source


class MemoryPlanProof(VersionedRecord):
    """Text-free owner proof; it must remain readable while the source is being purged."""

    operator_id: Id
    operation_type: Literal["memory.delete", "memory.export"]
    source_id: Id
    source_version: Version
    source_versions: list[tuple[Id, Version]] = Field(default_factory=list, max_length=200)
    selected_source_ids: list[Id] = Field(default_factory=list, max_length=200)
    session_ref: tuple[Id, Version] | None = None
    deletion_source_ids: list[Id] = Field(default_factory=list, max_length=200)
    digest: Digest
    dependency_digest: Digest
    erased: list[tuple[Id, Id]] = Field(default_factory=list, max_length=200)
    detached: list[tuple[Id, Id]] = Field(default_factory=list, max_length=200)
    indexed: list[Id] = Field(default_factory=list, max_length=200)
    plan_expires_at: UTC
    expires_at: UTC
    execution_id: Id | None = None


class RuleData(Contract):
    rule_id: Id
    level: Literal["project", "profile", "session"]
    kind: Literal["hard", "soft", "desired"]
    description: Annotated[str, Field(min_length=1, max_length=1500)]
    markers: list[Annotated[str, Field(min_length=1, max_length=100)]] = Field(
        default_factory=list, max_length=30
    )


class RulesData(Contract):
    rules: list[RuleData] = Field(max_length=200)


class ConfigureGM(Contract):
    intensity: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)] | None = None
    rules: RulesData | None = None
    rules_version: Version | None = None
    paused: bool | None = None


class LaboratoryCreateArguments(Contract):
    persona_id: Id


class LaboratoryStartArguments(Contract):
    scenario_id: Id
    scenario_version: Version
    lab_scope: Scope
    seed: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    mode: Literal["mock"] = "mock"

    @model_validator(mode="after")
    def isolated(self) -> Self:
        if self.lab_scope.memory_mode != "test" or self.lab_scope.agent_id is not None:
            raise ValueError("FRESH_TEST_SCOPE_REQUIRED")
        return self


class LaboratoryScenario(Contract):
    scenario_id: Id
    version: Version
    title: ShortText
    tags: list[ShortText] = Field(max_length=30)


class LaboratoryCatalogue(Contract):
    scope: Scope
    scenarios: list[LaboratoryScenario] = Field(max_length=100)
    mode: Literal["mock"] = "mock"


class ProfileMarkdown(Contract):
    scope: Scope
    profile_id: Id
    version: Version
    markdown: MarkdownText


class ProfileIssue(Contract):
    code: ShortText
    message: ShortText
    line: Annotated[int, Field(strict=True, ge=1)] | None = None
    section: ShortText | None = None


class ProfileDiffProjection(Contract):
    canon_changed: bool
    style_changed: bool
    properties_added: list[Id] = Field(max_length=100)
    properties_removed: list[Id] = Field(max_length=100)
    properties_changed: list[Id] = Field(max_length=100)
    initial_values_changed: list[Id] = Field(max_length=100)
    bounds_changed: list[Id] = Field(max_length=100)


class ProfileValidation(Contract):
    request_id: Id
    scope: Scope
    issues: list[ProfileIssue] = Field(max_length=200)
    blockers: list[ProfileIssue] = Field(max_length=200)
    digest: Digest | None
    current_version: Version | None
    version_conflict: bool
    unchanged: bool
    ready: bool
    diff: ProfileDiffProjection | None


def register_panel_extensions(registry: ExtensionRegistry) -> None:
    registry.register("llm.limits-args", ApiLimitArguments)
    registry.register("tariff.create-args", TariffCreateArguments)
    registry.register("memory.correct-args", MemoryCorrectArguments)
    registry.register("promise.close-args", PromiseCloseArguments)
    registry.register("memory.delete-args", MemorySelectionArguments)
    registry.register("memory.export-args", MemorySelectionArguments)
    registry.register("gm.configure-args", ConfigureGM)
    registry.register("change.decision-args", ChangeDecisionArguments)
    registry.register("change.interpretation-args", ChangeInterpretationArguments)
    registry.register("change.rollback-args", ResourceArguments)
    registry.register("opportunity.cancel-args", ResourceArguments)
    registry.register("arc.pause-args", ResourceArguments)
    registry.register("laboratory.start-args", LaboratoryStartArguments)
    registry.register("laboratory.stop-args", LaboratoryStopArguments)
    registry.register("laboratory.event-args", LaboratoryEventArguments)
    registry.register("laboratory.create-args", LaboratoryCreateArguments)
    registry.register("laboratory.transfer-settings-args", SettingsTransferArguments)
    from ai_agent_contracts.agent_workplace import (
        PermissionConditions,
        PermissionDecisionArguments,
        PermissionLimits,
        TicketReadArguments,
        TicketReplyArguments,
        WorkAbandonArguments,
        WorkAssignArguments,
    )

    registry.register("ticket.reply-args", TicketReplyArguments)
    registry.register("ticket.read-args", TicketReadArguments)
    registry.register("work.assign-args", WorkAssignArguments)
    from ai_agent_contracts.agent_sessions import (
        SessionReplyOrigin,
        WorkSessionCloseArguments,
        WorkSessionManageArguments,
        WorkSessionMessageArguments,
        WorkSessionOpenArguments,
        WorkSessionRenameArguments,
    )

    registry.register("work.session.open-args", WorkSessionOpenArguments)
    registry.register("work.session.message-args", WorkSessionMessageArguments)
    registry.register("work.session.close-args", WorkSessionCloseArguments)
    registry.register("work.session.rename-args", WorkSessionRenameArguments)
    registry.register("work.session.manage-args", WorkSessionManageArguments)
    registry.register("work.session-reply", SessionReplyOrigin)
    registry.register("work.abandon-args", WorkAbandonArguments)
    registry.register("permission.decision-args", PermissionDecisionArguments)
    from .workplace_access import InitialAccessArguments

    registry.register("workplace.access-args", InitialAccessArguments)
    registry.register("workplace.permission-conditions", PermissionConditions)
    registry.register("workplace.permission-limits", PermissionLimits)


SettingsTransferKey = Literal["profile", "gm_intensity", "story_rules_version"]


class SettingsTransferArguments(Contract):
    source_scope: Scope
    action: Literal["preview", "apply", "acknowledge"]
    operation_id: Id
    keys: list[SettingsTransferKey] = Field(min_length=1, max_length=3)
    preview_digest: Digest | None = None
    plan_id: Id | None = None
    plan_version: Version | None = None

    @model_validator(mode="after")
    def binding(self) -> Self:
        if len(set(self.keys)) != len(self.keys) or self.source_scope.memory_mode != "test":
            raise ValueError("TRANSFER_SELECTION")
        if self.action != "preview" and self.preview_digest is None:
            raise ValueError("TRANSFER_DIGEST_REQUIRED")
        if self.action == "apply" and (self.plan_id is None or self.plan_version is None):
            raise ValueError("TRANSFER_PLAN_REQUIRED")
        return self


class SettingsTransferItem(Contract):
    key: SettingsTransferKey
    before: JsonValue
    after: JsonValue
    changes: list[ShortText] = Field(max_length=200)
    blockers: list[ShortText] = Field(max_length=200)


class SettingsTransferPreview(VersionedRecord):
    request_id: Id
    operation_id: Id
    operator_id: Id
    source_scope: Scope
    keys: list[SettingsTransferKey] = Field(min_length=1, max_length=3)
    preview_digest: Digest
    target_version: Version
    expires_at: UTC
    items: list[SettingsTransferItem] = Field(min_length=1, max_length=3)
    never_transferred: list[ShortText] = Field(max_length=30)
    blocked: bool

    @model_validator(mode="after")
    def selection(self) -> Self:
        if (
            self.scope.memory_mode != "work"
            or self.source_scope.memory_mode != "test"
            or self.scope.workspace_id != self.source_scope.workspace_id
            or len(set(self.keys)) != len(self.keys)
            or [i.key for i in self.items] != self.keys
            or self.blocked != any(i.blockers for i in self.items)
            or self.expires_at <= self.created_at
        ):
            raise ValueError("TRANSFER_PREVIEW")
        return self


class SettingsTransferStatus(Contract):
    scope: Scope
    operation_id: Id
    record: SettingsTransferOperation | None


class LaboratoryStopArguments(Contract):
    operation_id: Id
    lab_scope: Scope

    @model_validator(mode="after")
    def test_only(self) -> Self:
        if self.lab_scope.memory_mode != "test":
            raise ValueError("TEST_SCOPE_REQUIRED")
        return self


class LaboratoryEventArguments(LaboratoryStopArguments):
    text: Annotated[str, Field(strict=True, min_length=1, max_length=2000)]


class LaboratoryRun(Contract):
    scope: Scope
    lab_scope: Scope
    operation_id: Id
    version: Version
    scenario_id: Id
    scenario_version: Version
    status: OperationState
    ready: bool
    virtual_time: UTC | None
    accepted_events: Annotated[int, Field(strict=True, ge=0, le=32)] | None

    @model_validator(mode="after")
    def partition(self) -> Self:
        if (
            self.lab_scope.memory_mode != "test"
            or self.scope.workspace_id != self.lab_scope.workspace_id
            or self.ready
            and self.status != OperationState.RUNNING
        ):
            raise ValueError("LABORATORY_RUN")
        return self


class LaboratoryRuns(Contract):
    scope: Scope
    items: list[LaboratoryRun] = Field(max_length=200)
    limit: Annotated[int, Field(strict=True, ge=1, le=200)]
    next_cursor: Id | None
    complete: bool


class ChangeDecisionArguments(Contract):
    decision: Literal["accept", "reject"]
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]


class ChangeInterpretationArguments(Contract):
    interpretation: Annotated[str, Field(strict=True, min_length=1, max_length=1500)]
    reason: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]


class ResourceArguments(Contract):
    pass


class RollbackProof(VersionedRecord):
    operator_id: Id
    change_id: Id
    chain_ids: list[Id] = Field(min_length=1, max_length=100)
    digest: Digest
    expires_at: UTC
    # kind:id separates same ID values owned by different entity types.
    pins: dict[ShortText, Version] = Field(max_length=200)


class OwnerOperation(VersionedRecord):
    request_id: Id
    operator_id: Id
    command_type: Id
    fingerprint: Digest
    target_owner: Literal["persona", "gm", "memory", "llm", "workspace"]
    target_id: Id
    state: Literal["planned", "completed", "reconciliation_required"]
    expected_result_ids: list[Id] = Field(default_factory=list, max_length=200)
    result_ids: list[Id] = Field(default_factory=list, max_length=200)
    receipt: Receipt | None = None
    completed_steps: list[ShortText] = Field(default_factory=list, max_length=20)
    pending_steps: list[ShortText] = Field(default_factory=list, max_length=20)
    expires_at: UTC

    @model_validator(mode="after")
    def outcome(self) -> Self:
        if (self.state == "completed") != (self.receipt is not None):
            raise ValueError("OWNER_OPERATION_RECEIPT")
        if self.receipt is not None and (
            self.receipt.scope != self.scope or self.receipt.operation_id != self.id
        ):
            raise ValueError("OWNER_OPERATION_SCOPE")
        return self


class PrivateApiStatus(Contract):
    """Safe private-dialogue admission counter, not a monetary cost or permission grant."""

    scope: Scope
    enabled: bool = Field(strict=True)
    memory_processing: bool = Field(default=False, strict=True)
    memory_daily_calls: int = Field(default=0, strict=True, ge=0, le=32)
    memory_calls_used: int = Field(default=0, strict=True, ge=0)
    # Activity share of the same daily limit (AW-05); 0/0 while the contour is off.
    activity_daily_calls: int = Field(default=0, strict=True, ge=0, le=1000)
    activity_calls_used: int = Field(default=0, strict=True, ge=0)
    dialogue_remaining: int | None = Field(default=None, strict=True, ge=0)
    period: Literal["lifetime", "utc_day"] | None
    attempt_limit: int | None = Field(strict=True, ge=1)
    attempts_used: int = Field(strict=True, ge=0)
    attempts_remaining: int | None = Field(strict=True, ge=0)
    resets_at: UTC | None
    checked_at: UTC

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not self.enabled:
            if (
                any(
                    x is not None
                    for x in (
                        self.period,
                        self.attempt_limit,
                        self.attempts_remaining,
                        self.resets_at,
                    )
                )
                or self.attempts_used != 0
            ):
                raise ValueError("PRIVATE_API_DISABLED")
        elif (
            self.period is None
            or self.attempt_limit is None
            or self.attempts_remaining != max(0, self.attempt_limit - self.attempts_used)
            or (self.period == "utc_day") != (self.resets_at is not None)
            or self.resets_at is not None
            and self.resets_at <= self.checked_at
        ):
            raise ValueError("PRIVATE_API_COUNTER")
        if self.memory_processing:
            if (
                not self.enabled
                or self.period != "utc_day"
                or self.attempt_limit is None
                or not 3 <= self.memory_daily_calls < self.attempt_limit
                or self.memory_calls_used > self.attempts_used
                or self.dialogue_remaining
                != max(
                    0,
                    self.attempt_limit
                    - self.attempts_used
                    - max(0, self.memory_daily_calls - self.memory_calls_used),
                )
            ):
                raise ValueError("PRIVATE_MEMORY_COUNTER")
        elif (
            self.memory_daily_calls
            or self.memory_calls_used
            or (self.dialogue_remaining not in (None, self.attempts_remaining))
        ):
            raise ValueError("PRIVATE_MEMORY_DISABLED")
        return self


class WorkSessionEntry(Contract):
    """One message of a work conversation as the panel may show it (R3).

    ``text`` comes from the memory Source owner through the release gate; ``withheld`` means
    its source is revoked, expired, deleted or blocked, and no text is given.
    """

    sequence: int = Field(strict=True, ge=1)
    speaker: Literal["operator", "character"]
    text: str | None = Field(default=None, max_length=16000)
    withheld: bool = Field(strict=True)
    at: UTC
    reply_to_sequence: int | None = Field(default=None, strict=True, ge=1)
    reply_id: Id | None = None

    @model_validator(mode="after")
    def text_or_withheld(self) -> Self:
        if self.withheld != (self.text is None):
            raise ValueError("WITHHELD_TEXT")
        return self


class WorkSessionDraft(Contract):
    created_at: UTC | None = None
    through_sequence: int | None = Field(default=None, strict=True, ge=0)
    scope: Scope
    utterance_id: Id
    version: Version
    revision: Version
    text: str | None = Field(default=None, max_length=16000)
    withheld: bool = Field(strict=True)
    acknowledgeable: bool = Field(strict=True)

    @model_validator(mode="after")
    def valid_text(self) -> Self:
        if self.withheld != (self.text is None) or (self.withheld and self.acknowledgeable):
            raise ValueError("DRAFT_RELEASE")
        return self


class WorkSessionModelCalls(Contract):
    """Confirmed model calls of the current turn, from the LLM journal: counts and time only."""

    completed: int = Field(strict=True, ge=0)
    failed: int = Field(strict=True, ge=0)
    running: int = Field(strict=True, ge=0)
    last_at: UTC | None = None


class WorkSessionResultFile(Contract):
    """A file the turn actually saved through the file owner (title and logical path only)."""

    file_id: Id
    version: Version
    title: ShortText
    path: ShortText
    current: bool = Field(strict=True)


class WorkSessionProgress(Contract):
    """Read-only state of the conversation's current turn from SessionBook, WorkAttempt and Job.

    ``waiting_start``: a stored message waits for an explicit START; ``queued``: accepted or
    waiting for its turn; ``running``: its job runs; ``reply_ready``: a draft waits for
    delivery; ``done``: the turn succeeded; ``failed``/``cancelled``/``unknown``: it ended
    without a reply (``unknown``: the outcome is not confirmed). ``since`` is a recorded time,
    never a timer. ``error_code`` only when the owner recorded one.
    """

    state: Literal[
        "waiting_start",
        "queued",
        "running",
        "reply_ready",
        "done",
        "failed",
        "cancelled",
        "unknown",
    ]
    since: UTC
    attempt_no: int | None = Field(default=None, strict=True, ge=1)
    model_calls: WorkSessionModelCalls | None = None
    files: list[WorkSessionResultFile] = Field(default_factory=list, max_length=50)
    error_code: ShortText | None = None


class WorkSessionTranscript(Contract):
    """A bounded tail of one work conversation; ``complete`` is false if older ones are cut."""

    scope: Scope
    session_id: Id
    session_version: Version
    created_at: UTC | None = None
    updated_at: UTC | None = None
    state: Literal["open", "waiting", "closed", "invalidated"]
    title: ShortText
    # Messages up to here were taken by the person's session; later ones are stored and wait.
    processed_sequence: int = Field(default=0, strict=True, ge=0)
    pending_reply: WorkSessionDraft | None = None
    # Why stored messages wait, when the platform knows: work conversations are not available on
    # this host (``waiting_detail`` is a fixed code), need a right (``access_ticket_id`` once the
    # technical request is open) or the operator refused it; or a stored message became
    # unavailable (revoked, deleted, expired) and will not be executed.
    waiting_reason: (
        Literal[
            "unavailable",
            "access_required",
            "access_requested",
            "access_refused",
            "message_unavailable",
        ]
        | None
    ) = None
    waiting_detail: ShortText | None = None
    access_ticket_id: Id | None = None
    # Set with state ``invalidated``: an unprocessed operator message became unavailable and was
    # not executed; the operator continues in a new conversation.
    invalidated_reason: Literal["message_unavailable"] | None = None
    # The current turn, when there is one to report (read-only; never resends anything).
    progress: WorkSessionProgress | None = None
    # Tickets linked to this conversation that now wait for the operator's answer (current
    # ticket owner state, this scope only). Answers and decisions are made in the panel.
    open_ticket_ids: list[Id] = Field(default_factory=list, max_length=20)
    entries: list[WorkSessionEntry] = Field(max_length=200)
    complete: bool = Field(strict=True)
    checked_at: UTC


class WorkplaceFileContent(Contract):
    """The text of one catalogued file for the operator, through the file owner's guard.

    ``withheld`` means a basis of the file is revoked, expired, deleted or blocked, or the file
    is not text: no content is given. Long text is cut and marked ``truncated``.
    """

    scope: Scope
    file_id: Id
    version: Version
    title: ShortText
    area: str = Field(max_length=32)
    kind: str = Field(max_length=32)
    provenance: str = Field(max_length=32)
    media_type: str = Field(max_length=128)
    text: str | None = Field(default=None, max_length=20000)
    withheld: bool = Field(strict=True)
    truncated: bool = Field(strict=True)
    checked_at: UTC

    @model_validator(mode="after")
    def text_or_withheld(self) -> Self:
        if self.withheld != (self.text is None) or (self.withheld and self.truncated):
            raise ValueError("WITHHELD_TEXT")
        return self


class WorkSessionSources(Contract):
    """Explicit conversation-owned source identities for operator retention previews."""

    scope: Scope
    session_id: Id
    session_version: Version
    sources: list[tuple[Id, Version]] = Field(default_factory=list, max_length=2000)
    complete: bool = Field(default=True, strict=True)
    unavailable_count: int = Field(default=0, strict=True, ge=0, le=2000)
