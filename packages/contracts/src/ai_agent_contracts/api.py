"""Panel and service wire API. Commands, data messages and worker results are distinct."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from ai_agent_contracts.common import (
    UTC,
    Contract,
    Counter,
    ExtensionPayload,
    Id,
    Record,
    Scope,
    ShortText,
    Text,
    TrueFlag,
    Version,
    VersionedRecord,
)
from ai_agent_contracts.control import ExecutionResult, Freshness, Lease
from ai_agent_contracts.states import ErrorCode, RunState

ERROR_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.ACCESS_DENIED: "Доступ к области или операции запрещён.",
    ErrorCode.VERSION_CONFLICT: "Версия изменилась; перечитайте запись.",
    ErrorCode.IDEMPOTENCY_CONFLICT: "ID уже использован с другим содержимым.",
    ErrorCode.STALE_CONTEXT: "Контекст или допуск устарел.",
    ErrorCode.EXPIRED: "Срок действия истёк.",
    ErrorCode.INVALID_ARGUMENTS: "Вход не соответствует контракту или лимиту.",
    ErrorCode.CAPABILITY_UNAVAILABLE: "Возможность недоступна.",
    ErrorCode.MODERATION_UNAVAILABLE: "Обязательная модерация недоступна.",
    ErrorCode.RESOURCE_BUSY: "Ресурс занят.",
    ErrorCode.SERVICE_UNAVAILABLE: "Служба недоступна.",
    ErrorCode.OUTCOME_UNKNOWN: "Исход неизвестен; требуется сверка.",
}


class ApiError(Contract):
    code: ErrorCode
    message: ShortText
    request_id: Id | None
    retryable: bool = Field(strict=True, default=False)
    current_version: Version | None = None
    state: RunState | None = None

    @model_validator(mode="after")
    def safe_message(self) -> Self:
        if self.message != ERROR_MESSAGES[self.code]:
            raise ValueError("SAFE_ERROR_REQUIRED")
        return self


class CommandBase(Contract):
    request_id: Id
    schema_version: Literal["1.0"] = "1.0"
    scope: Scope
    target: Id


class StopCommand(CommandBase):
    type: Literal["agent.stop"]
    # STOP deliberately has no profile/expected version or queued job dependency.


class ControlArguments(Contract):
    autonomy: Literal["observation", "autonomous"] | None = None


class ControlCommand(CommandBase):
    type: Literal["agent.start", "agent.pause", "agent.resume", "session.end"]
    arguments: ControlArguments
    expected_version: Version


class MessageArguments(Contract):
    text: Text = Field(min_length=1)


class MessageCommand(CommandBase):
    type: Literal["dialogue.message"]
    arguments: MessageArguments


class AcknowledgeArguments(Contract):
    revision: Version


class AcknowledgeCommand(CommandBase):
    type: Literal["dialogue.acknowledge"]
    arguments: AcknowledgeArguments
    expected_version: Version


class ReflectArguments(Contract):
    episode_ids: list[Id] = Field(min_length=1, max_length=20)


class ReflectCommand(CommandBase):
    type: Literal["session.reflect"]
    arguments: ReflectArguments
    expected_version: Version


class CreateCommand(CommandBase):
    type: Literal[
        "persona.create",
        "session.create",
        "briefing.create",
        "laboratory.create",
        "tariff.create",
        "persona.copy",
    ]
    arguments: ExtensionPayload


class EditCommand(CommandBase):
    type: Literal[
        "persona.edit",
        "persona.validate",
        "persona.apply",
        "briefing.edit",
        "briefing.cancel",
        "memory.correct",
        "person.link",
        "person.unlink",
        "gm.configure",
        "opportunity.cancel",
        "arc.pause",
        "change.decision",
        "change.interpretation",
        "laboratory.event",
        "laboratory.start",
        "laboratory.stop",
        "laboratory.transfer_settings",
        "permission.edit",
        "binding.edit",
        "operation.cancel",
        "llm.assignment",
        "llm.limits",
    ]
    arguments: ExtensionPayload
    expected_version: Version


class PlanCommand(CommandBase):
    type: Literal[
        "memory.delete.preview",
        "memory.export.preview",
        "change.rollback.preview",
        "workspace.backup.preview",
        "workspace.restore.preview",
        "llm.recalculate.preview",
    ]
    arguments: ExtensionPayload
    expected_version: Version


class ExecutePlanArguments(Contract):
    plan_id: Id
    plan_version: Version


class ExecutePlanCommand(CommandBase):
    type: Literal[
        "memory.delete.execute",
        "memory.export.execute",
        "change.rollback.execute",
        "workspace.backup.execute",
        "workspace.restore.execute",
        "llm.recalculate.execute",
    ]
    arguments: ExecutePlanArguments
    expected_version: Version


type PanelCommand = Annotated[
    StopCommand
    | ControlCommand
    | MessageCommand
    | AcknowledgeCommand
    | ReflectCommand
    | CreateCommand
    | EditCommand
    | PlanCommand
    | ExecutePlanCommand,
    Field(discriminator="type"),
]


class Receipt(Contract):
    request_id: Id
    scope: Scope
    status: Literal["accepted", "completed", "waiting", "rejected"]
    resource_id: Id | None = None
    operation_id: Id | None = None
    version: Version | None = None

    @model_validator(mode="after")
    def identity_required(self) -> Self:
        if self.resource_id is None and self.operation_id is None:
            raise ValueError("RECEIPT_ID_REQUIRED")
        return self


class PlanDependency(Contract):
    resource_id: Id
    version: Version


class ConsequencePlan(VersionedRecord):
    operation_type: Id
    consequences: Text
    dependencies: list[PlanDependency] = Field(max_length=200)
    expires_at: UTC


class Page[T](Contract):
    items: list[T] = Field(max_length=200)
    limit: int = Field(strict=True, ge=1, le=200, default=50)
    next_cursor: ShortText | None
    complete: bool = Field(strict=True)
    projection_lag_ms: Counter | None = None

    @model_validator(mode="after")
    def limited(self) -> Self:
        if len(self.items) > self.limit:
            raise ValueError("PAGE_LIMIT")
        return self


class PanelEvent(Record):
    event_id: Id
    type: Literal[
        "run.changed",
        "job.changed",
        "utterance.changed",
        "delivery.changed",
        "memory.changed",
        "relationship.changed",
        "arc.changed",
        "change.changed",
        "check.changed",
        "connection.changed",
        "usage.changed",
    ]
    resource_id: Id
    resource_version: Version | None
    occurred_at: UTC
    causal_id: Id | None
    operation_id: Id | None


class StreamChunk(Record):
    type: Literal["generation.chunk"]
    generation_id: Id
    utterance_id: Id
    attempt_id: Id
    sequence: Counter
    data_type: Literal["text", "json_fragment"]
    data: Text
    preliminary: TrueFlag = True


class StreamTerminal(Record):
    type: Literal["generation.terminal"]
    generation_id: Id
    utterance_id: Id
    attempt_id: Id
    sequence: Counter
    status: Literal["completed", "rejected", "cancelled", "incomplete"]
    saved_result_id: Id | None

    @model_validator(mode="after")
    def saved_complete_result(self) -> Self:
        if self.status == "completed" and self.saved_result_id is None:
            raise ValueError("RESULT_REQUIRED")
        return self


class ServiceClaimCommand(CommandBase):
    type: Literal["job.claim"]
    operation_id: Id
    expected_version: Version
    freshness: Freshness

    @model_validator(mode="after")
    def claim_context(self) -> Self:
        if self.target != self.operation_id or self.scope.run_id != self.freshness.run_id:
            raise ValueError("CLAIM_CONTEXT_MISMATCH")
        return self


class ServiceLeaseCommand(CommandBase):
    type: Literal["job.renew", "job.progress", "job.cancel_receipt"]
    operation_id: Id
    lease: Lease
    progress: int | None = Field(strict=True, ge=0, le=100, default=None)

    @model_validator(mode="after")
    def lease_context(self) -> Self:
        if (
            self.target != self.operation_id
            or self.lease.operation_id != self.operation_id
            or self.lease.scope != self.scope
        ):
            raise ValueError("LEASE_CONTEXT_MISMATCH")
        return self


class ServiceResultCommand(CommandBase):
    type: Literal["job.result"]
    result: ExecutionResult

    @model_validator(mode="after")
    def result_context(self) -> Self:
        if self.target != self.result.operation_id or self.scope != self.result.scope:
            raise ValueError("RESULT_CONTEXT_MISMATCH")
        return self


type ServiceCommand = Annotated[
    ServiceClaimCommand | ServiceLeaseCommand | ServiceResultCommand,
    Field(discriminator="type"),
]


class Permission(VersionedRecord):
    principal_id: Id
    capability_id: Id
    operation_type: Id
    binding_id: Id
    level: Literal["denied", "prepare_only", "operator_decision", "automatic"]
    conditions: ExtensionPayload
    limits: ExtensionPayload
    policy_version: Version
    author_id: Id
    expires_at: UTC
    revoked_at: UTC | None = None


class StopReceipt(Receipt):
    state: RunState
    control_epoch: Counter
    run_id: Id | None
    unconfirmed_resource_ids: list[Id] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def confirmed_stop(self) -> Self:
        if self.state not in {RunState.STOPPING, RunState.STOPPED, RunState.FAULTED}:
            raise ValueError("STOP_STATE_REQUIRED")
        if self.state == RunState.STOPPED and self.unconfirmed_resource_ids:
            raise ValueError("STOP_UNCONFIRMED")
        return self


class EventSubscription(Contract):
    scope: Scope
    resource_ids: list[Id] = Field(default_factory=list, max_length=200)
    after_event_id: Id | None = None
    replay_limit: int = Field(strict=True, ge=1, le=256, default=50)
