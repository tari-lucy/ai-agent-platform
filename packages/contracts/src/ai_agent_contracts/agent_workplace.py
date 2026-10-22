"""Agent workplace records (AW-00): manifests, tasks, managed files, skills and tickets.

Records describe state; none of them grants a right, starts work or names a host path.
Execution attempts stay Job/ExecutionCommand/ExecutionResult of the jobs owner; rights stay
api.Permission of the workspace owner. A WorkAttempt only links a semantic task to that job.
"""

import re
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, Field, model_validator

from ai_agent_contracts.agent_sessions import SessionTicketRef
from ai_agent_contracts.common import (
    UTC,
    ArtifactRef,
    Contract,
    Counter,
    Digest,
    Id,
    Record,
    Scope,
    ShortText,
    SourceRef,
    Text,
    Version,
    VersionedRecord,
)
from ai_agent_contracts.control import Freshness

MAX_DEPENDENCIES = 200

type WorkplaceArea = Literal[
    "inbox", "knowledge", "skills", "projects", "scratch", "exports", "reference", "guide"
]
# Areas the agent may write through the file owner; reference/guide are read-only views.
WRITABLE_AREAS: frozenset[str] = frozenset(
    {"inbox", "knowledge", "skills", "projects", "scratch", "exports"}
)

_SEGMENT = r"[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)*"
_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?$")


def logical_path(value: str) -> str:
    """Catalogue name inside an area, never a host path: lowercase ASCII, no dot segments."""
    segments = value.split("/")
    if (
        not 1 <= len(value) <= 256
        or not 1 <= len(segments) <= 8
        or any(
            not re.fullmatch(_SEGMENT, part)
            or len(part) > 64
            or _WINDOWS_RESERVED.match(part) is not None
            for part in segments
        )
    ):
        raise ValueError("LOGICAL_PATH")
    return value


LogicalPath = Annotated[str, Field(strict=True), AfterValidator(logical_path)]


def _stable_area(record: VersionedRecord) -> None:
    # Personal areas belong to workspace/persona/space/mode, not to a run or session.
    if record.scope.run_id is not None or record.scope.agent_id is not None:
        raise ValueError("STABLE_SCOPE_REQUIRED")


# --- Capabilities ---------------------------------------------------------------------------


class CapabilityManifest(VersionedRecord):
    """Discoverable description of an existing workspace.Capability (owner: workspace).

    ``instructions`` is data for the agent's choice of method, never an operator command.
    """

    capability_id: Id
    capability_version: Version
    summary: ShortText
    instructions: Text
    input_schema_id: Id
    output_schema_id: Id
    effect: Literal["none", "local_write", "external_effect", "public_output"]
    areas: list[WorkplaceArea] = Field(max_length=8)
    network: Literal["none", "gateway"]
    runner_required: bool = Field(strict=True)


class CapabilityBinding(VersionedRecord):
    """Concrete adapter connection of a capability (owner: workspace).

    An approved ticket does not make a binding ready: ``ready`` needs a verification time.
    """

    binding_id: Id
    capability_id: Id
    capability_version: Version
    adapter_id: Id
    adapter_version: Id
    state: Literal["pending", "ready", "disabled", "revoked"]
    verified_at: UTC | None = None

    @model_validator(mode="after")
    def verified_ready(self) -> Self:
        if self.binding_id != self.id:
            raise ValueError("BINDING_ID_MISMATCH")
        if (self.state == "ready") != (self.verified_at is not None):
            raise ValueError("BINDING_VERIFICATION")
        return self


# --- Tasks and attempts (owner: activity) ---------------------------------------------------

type WorkOrigin = Literal["assignment", "own_goal", "commitment", "ticket_followup"]
type WorkTaskState = Literal["open", "active", "waiting", "done", "abandoned"]


class WorkStep(Contract):
    """One finished tool step of a task, as the activity owner recorded it.

    ``read_*`` is what the step actually read: later attempts of the task carry it as inputs,
    so a file written from it keeps the lineage. ``written`` are the files it produced.
    """

    at: UTC
    attempt_id: Id
    capability_id: Id
    status: Literal["succeeded", "failed", "unknown"]
    summary: Text = Field(max_length=4000)
    read_sources: list[SourceRef] = Field(default_factory=list, max_length=50)
    read_files: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    written: list[ArtifactRef] = Field(default_factory=list, max_length=20)


class WorkTask(VersionedRecord):
    """Semantic goal, separate from any execution attempt. Never runs on its own."""

    task_id: Id
    origin: WorkOrigin
    goal: Text = Field(min_length=1)
    reason: Text
    state: WorkTaskState
    parent_task_id: Id | None = None
    waiting_ticket_ids: list[Id] = Field(default_factory=list, max_length=20)
    outcome: Text | None = None
    result_files: list[ArtifactRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    progress: list[WorkStep] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.task_id != self.id:
            raise ValueError("TASK_ID_MISMATCH")
        if self.state == "waiting" and not self.waiting_ticket_ids:
            raise ValueError("WAIT_CONDITION_REQUIRED")
        if self.state in ("done", "abandoned") and self.outcome is None:
            raise ValueError("OUTCOME_REQUIRED")
        return self


type AttemptState = Literal["prepared", "submitted", "succeeded", "failed", "cancelled", "unknown"]


class WorkAttempt(VersionedRecord):
    """One task step or conversation turn, bound to exactly one job operation.

    ``inputs``/``input_files`` are recorded by the trusted context service when the attempt is
    prepared; result dependencies are derived from them and cannot be removed by the executor.
    ``state`` reconciles from the job's ExecutionResult; ``unknown`` needs reconciliation
    before any retry with an external effect.
    """

    task_id: Id | None = None
    session_id: Id | None = None
    session_sequence: int | None = Field(default=None, strict=True, ge=1)
    attempt_no: int = Field(strict=True, ge=1, le=1000)
    operation_id: Id
    capability_id: Id
    capability_version: Version
    freshness: Freshness
    state: AttemptState
    inputs: list[SourceRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    input_files: list[ArtifactRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    result_files: list[ArtifactRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    result_id: Id | None = None
    session_tickets: list[SessionTicketRef] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def run_bound(self) -> Self:
        if self.session_tickets and (
            self.session_id is None
            or len({ref.ticket_id for ref in self.session_tickets}) != len(self.session_tickets)
        ):
            raise ValueError("ATTEMPT_SESSION_TICKETS")
        if (self.task_id is None) == (self.session_id is None) or (
            (self.session_id is None) != (self.session_sequence is None)
        ):
            raise ValueError("ATTEMPT_SUBJECT")
        if self.scope.run_id is None or self.scope.run_id != self.freshness.run_id:
            raise ValueError("RUN_MISMATCH")
        if self.state == "succeeded" and self.result_id is None:
            raise ValueError("RESULT_REQUIRED")
        return self


# --- Managed files (owner: catalogue) -------------------------------------------------------

type FileKind = Literal[
    "material", "note", "hypothesis", "method", "script", "plan", "draft", "result"
]
type FileProvenance = Literal["read", "generated", "assumption", "verified"]


class FileAccess(Contract):
    api_transfer: bool = Field(strict=True)
    public_disclosure: bool = Field(strict=True)


class ManagedFile(VersionedRecord):
    """Catalogue entry of one version of a workplace file; content is addressed by reference.

    ``dependencies`` is the complete, conservative source set assigned by the trusted file or
    context service. Expired/deleted/unavailable sources block the file, they are not dropped.
    """

    reference: ArtifactRef
    area: WorkplaceArea
    path: LogicalPath
    title: ShortText
    kind: FileKind
    provenance: FileProvenance
    dependencies: list[SourceRef] = Field(max_length=MAX_DEPENDENCIES)
    derived_from: list[ArtifactRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    access: FileAccess
    state: Literal["active", "blocked", "deleted"]
    expires_at: UTC | None = None
    produced_by_attempt: Id | None = None
    change_reason: ShortText | None = None

    @model_validator(mode="after")
    def same_file(self) -> Self:
        _stable_area(self)
        if self.reference.artifact_id != self.id or self.reference.version != self.version:
            raise ValueError("ARTIFACT_REFERENCE_MISMATCH")
        if self.state == "active" and any(
            item.availability != "available" for item in self.dependencies
        ):
            raise ValueError("DEPENDENCY_UNAVAILABLE")
        if self.provenance == "verified" and not self.dependencies and not self.derived_from:
            raise ValueError("GROUNDS_REQUIRED")
        return self


class FileWrite(Contract):
    """Request to the file owner. Content bytes travel beside it, checked by digest/size.

    ``expected_version`` None creates; otherwise it is the CAS precondition.
    ``declared_sources`` can only add dependencies to those of the attempt.
    """

    request_id: Id
    file_id: Id
    expected_version: Version | None
    area: WorkplaceArea
    path: LogicalPath
    title: ShortText
    kind: FileKind
    provenance: Literal["generated", "assumption"]
    media_type: ShortText
    checksum: Digest
    size_bytes: Counter
    attempt_id: Id | None = None
    declared_sources: list[SourceRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    change_reason: ShortText | None = None

    @model_validator(mode="after")
    def writable(self) -> Self:
        if self.area not in WRITABLE_AREAS:
            raise ValueError("AREA_READ_ONLY")
        return self


# --- Skills (owner: skills) -----------------------------------------------------------------


class SkillVersion(VersionedRecord):
    """A method of using existing capabilities; it never adds tools, rights or schedules.

    ``uses_capabilities`` is descriptive; execution is still checked against permissions.
    ``verified`` needs evidence of a confirmed result, not the model's confidence.
    """

    skill_id: Id
    title: ShortText
    applicability: Text
    limitations: Text
    method: ArtifactRef
    helper_files: list[ArtifactRef] = Field(default_factory=list, max_length=20)
    uses_capabilities: list[Id] = Field(default_factory=list, max_length=50)
    status: Literal["experimental", "verified", "retired"]
    evidence: list[SourceRef] = Field(default_factory=list, max_length=MAX_DEPENDENCIES)
    change_reason: ShortText

    @model_validator(mode="after")
    def evidence_for_verified(self) -> Self:
        _stable_area(self)
        if self.skill_id != self.id:
            raise ValueError("SKILL_ID_MISMATCH")
        if self.status == "verified" and not self.evidence:
            raise ValueError("EVIDENCE_REQUIRED")
        return self


# --- Tickets (owner: tickets) ---------------------------------------------------------------

type TicketKind = Literal["problem", "question", "idea", "capability_request"]
type TicketState = Literal["waiting_reply", "in_progress", "resolved", "closed_unfulfilled"]
type TicketParty = Literal["operator", "agent", "service"]


class CapabilityRequest(Contract):
    """What a capability ticket asks for; text and approval never grant it by themselves."""

    capability_id: Id
    areas: list[WorkplaceArea] = Field(default_factory=list, max_length=8)
    duration_seconds: int | None = Field(strict=True, ge=60, le=31_536_000, default=None)
    constraints: Text


class Ticket(VersionedRecord):
    """Durable conversation about one question; not a Job, ResourceTicket or person."""

    ticket_id: Id
    kind: TicketKind
    title: ShortText = Field(min_length=1)
    summary: Text
    attempted: Text
    decision_needed: Text
    state: TicketState
    next_turn: TicketParty | None
    task_ids: list[Id] = Field(default_factory=list, max_length=20)
    materials: list[ArtifactRef] = Field(default_factory=list, max_length=50)
    sources: list[SourceRef] = Field(default_factory=list, max_length=50)
    agent_priority: int = Field(strict=True, ge=0, le=3)
    technical_refusal: bool = Field(strict=True, default=False)
    duplicate_key: Digest
    occurrences: int = Field(strict=True, ge=1, le=1_000_000, default=1)
    message_count: Counter
    operator_read_version: Counter
    request: CapabilityRequest | None = None
    resolution: Text | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.ticket_id != self.id:
            raise ValueError("TICKET_ID_MISMATCH")
        if (self.kind == "capability_request") != (self.request is not None):
            raise ValueError("CAPABILITY_REQUEST_SHAPE")
        terminal = self.state in ("resolved", "closed_unfulfilled")
        if terminal != (self.next_turn is None):
            raise ValueError("NEXT_TURN")
        if self.state == "waiting_reply" and self.next_turn != "operator":
            raise ValueError("NEXT_TURN")
        if terminal and self.resolution is None:
            raise ValueError("RESOLUTION_REQUIRED")
        if self.operator_read_version > self.version:
            raise ValueError("READ_VERSION")
        return self


class TicketMessage(Record):
    """Immutable message; ``ticket_version`` is the ticket revision it produced."""

    ticket_id: Id
    sequence: int = Field(strict=True, ge=1, le=100_000)
    author: TicketParty
    author_id: Id
    body: Text = Field(min_length=1)
    ticket_version: Version


# --- Operator ticket commands (AW-01) -------------------------------------------------------


class TicketReplyArguments(Contract):
    """Operator message on an exact ticket version; ``answer`` hands the turn to the agent.

    ``resolve``/``close`` end the ticket with this text as the resolution; a later answer or
    agent recurrence reopens it. Nothing here grants a right: that is a separate decision.
    """

    ticket_id: Id
    body: Text = Field(min_length=1, max_length=4000)
    outcome: Literal["answer", "resolve", "close"]

    @model_validator(mode="after")
    def not_blank(self) -> Self:
        if not self.body.strip():
            raise ValueError("EMPTY_MESSAGE")
        return self


class TicketReadArguments(Contract):
    ticket_id: Id


# --- Operator permission decision (AW-01, second delivery) ----------------------------------

PermissionLevel = Literal["prepare_only", "operator_decision", "automatic"]


class PermissionDecisionArguments(Contract):
    """A structured operator decision on one capability right; never parsed from ticket text.

    ``grant`` answers an exact ticket version and creates or replaces the right; ``revoke``
    names the exact current permission version. Both go preview → execute.
    """

    action: Literal["grant", "revoke"]
    capability_id: Id
    binding_id: Id
    ticket_id: Id | None = None
    ticket_version: Version | None = None
    level: PermissionLevel | None = None
    duration_seconds: int | None = Field(strict=True, ge=60, le=31_536_000, default=None)
    areas: list[WorkplaceArea] = Field(default_factory=list, max_length=8)
    constraints: Text = ""
    permission_id: Id | None = None
    permission_version: Version | None = None

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.action == "grant" and (
            self.ticket_id is None
            or self.ticket_version is None
            or self.level is None
            or self.duration_seconds is None
        ):
            raise ValueError("GRANT_SHAPE")
        if self.action == "revoke" and (
            self.permission_id is None or self.permission_version is None
        ):
            raise ValueError("REVOKE_SHAPE")
        if len(set(self.areas)) != len(self.areas):
            raise ValueError("AREAS_DUPLICATE")
        return self


class PermissionConditions(Contract):
    """``workplace.permission-conditions`` of a workplace Permission: scope of the decision."""

    areas: list[WorkplaceArea] = Field(max_length=8)
    constraints: Text
    ticket_id: Id | None
    ticket_version: Version | None


class PermissionLimits(Contract):
    """``workplace.permission-limits``: the granted duration, bounded by the host ceiling."""

    duration_seconds: int = Field(strict=True, ge=60, le=31_536_000)


class PermissionDecisionProof(VersionedRecord):
    """Workspace-owner proof of a previewed decision; execute rechecks every pinned version."""

    operator_id: Id
    decision: PermissionDecisionArguments
    permission_id: Id
    permission_version: Version | None
    capability_id: Id
    manifest_version: Version
    binding_id: Id
    binding_version: Version
    ticket_id: Id | None
    ticket_version: Version | None
    policy_version: Version
    plan_expires_at: UTC
    expires_at: UTC
    executed_by: Id | None = None


# --- Operator assignment (AW-05) -------------------------------------------------------------


class WorkAssignArguments(Contract):
    """An operator's assignment; it is stored as a task and runs only after an explicit START."""

    goal: Text = Field(min_length=1, max_length=4000)
    reason: Text = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def not_blank(self) -> Self:
        if not self.goal.strip():
            raise ValueError("EMPTY_GOAL")
        return self


class WorkAbandonArguments(Contract):
    """The operator stops pursuing one task; the exact task version is the command's pin."""

    task_id: Id
    reason: Text = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def not_blank(self) -> Self:
        if not self.reason.strip():
            raise ValueError("EMPTY_REASON")
        return self


class WorkplaceLibrary(Contract):
    """Operator view of skills and catalogued files, read through the file owner's guard.

    A skill whose method is no longer releasable (revoked source, expired, hidden) is left out;
    ``complete`` is false when a listing reached its bound.
    """

    scope: Scope
    skills: list[SkillVersion] = Field(max_length=200)
    files: list[ManagedFile] = Field(max_length=800)
    complete: bool = Field(strict=True)
    checked_at: UTC
