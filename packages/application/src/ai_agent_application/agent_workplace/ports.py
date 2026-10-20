"""AW-00 ports of the agent workplace. Trusted composition implements and wires them.

Each port has one state owner; none is reachable with agent/runner credentials.
Execution attempts use the existing JobOwner; rights use the workspace Permission owner;
every model call goes through the existing LLMGateway admission and usage journal.
"""

import builtins
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from ai_agent_contracts.agent_workplace import (
    CapabilityManifest,
    FileWrite,
    ManagedFile,
    Ticket,
    TicketMessage,
    WorkAttempt,
)
from ai_agent_contracts.common import ArtifactRef, Scope, SourceRef
from ai_agent_contracts.control import Freshness

from ai_agent_application.llm.model import ChatMessage


@dataclass(frozen=True)
class WorkAdmission:
    """Trusted proof that one attempt may do IO now; never built from agent output.

    ``is_current`` re-checks supervisor epoch, job lease and cancellation. Every owner calls it
    immediately before its own commit (file save, ticket post, model call, runner start).
    """

    scope: Scope
    freshness: Freshness
    operation_id: str
    attempt_id: str
    lease_version: int
    is_current: Callable[[], bool]


class ReleaseGuard(Protocol):
    """Atomic check of sources, TTL and current rights before a result is saved or released.

    Raises ContractError(STALE_CONTEXT/ACCESS_DENIED/EXPIRED). It reads the deletion ledger and
    retention owner; it is the same check for files, skills, tickets and harness output.
    """

    async def verify(
        self,
        admission: WorkAdmission,
        sources: Sequence[SourceRef],
        files: Sequence[ArtifactRef],
        purpose: Literal["private", "api_transfer", "public_disclosure"],
    ) -> None: ...


class AttemptContext(Protocol):
    """Records attempt inputs; the file owner derives result dependencies from them."""

    async def attempt(self, scope: Scope, attempt_id: str) -> WorkAttempt | None: ...


class WorkspaceFiles(Protocol):
    """Single owner of managed files (catalogue). AW-02 implements; consumers call only this.

    ``write`` validates checksum/size against ``content``, path and quota, unions the attempt's
    recorded inputs with ``declared_sources``, calls ReleaseGuard and commits atomically with
    CAS on ``expected_version``. A repeated ``request_id`` with the same content returns the
    stored version; different content is IDEMPOTENCY_CONFLICT.
    """

    async def read(
        self, admission: WorkAdmission, file_id: str, version: int | None = None
    ) -> tuple[ManagedFile, bytes]: ...

    async def list(
        self, scope: Scope, area: str, *, prefix: str | None = None, limit: int = 50
    ) -> list[ManagedFile]: ...

    async def search(
        self, scope: Scope, query: str, *, limit: int = 20
    ) -> builtins.list[ManagedFile]: ...

    async def write(
        self, admission: WorkAdmission, change: FileWrite, content: bytes
    ) -> ManagedFile: ...


class CapabilityCatalogue(Protocol):
    """Manifests the agent may discover in its scope, with their current binding state."""

    async def manifests(self, scope: Scope) -> list[CapabilityManifest]: ...


class TicketDesk(Protocol):
    """Agent/service side of the tickets owner. Operator replies arrive through the panel API."""

    async def open_or_append(
        self, admission: WorkAdmission, request_id: str, ticket: Ticket, message: str
    ) -> tuple[Ticket, TicketMessage]: ...

    async def pending_for_agent(self, scope: Scope, *, limit: int = 20) -> list[Ticket]: ...


@dataclass(frozen=True)
class ModelRequest:
    """One model call of the activity contour or a harness, under a WorkAdmission."""

    role_id: str
    messages: tuple[ChatMessage, ...]
    max_output_tokens: int


@dataclass(frozen=True)
class ModelReply:
    status: Literal["completed", "failed", "cancelled", "incomplete", "unknown", "not_admitted"]
    text: str
    attempt_id: str


class ModelGateway(Protocol):
    """The only model route for workplace code and harness adapters.

    Implemented by composition over LLMGateway + the shared daily budget + AttemptJournal.
    Each call (including retries and harness-internal calls) is admitted and journaled before
    send; exhaustion returns ``not_admitted`` without calling the provider.
    """

    async def generate(self, admission: WorkAdmission, request: ModelRequest) -> ModelReply: ...


@dataclass(frozen=True)
class RunnerLimits:
    cpu_seconds: int
    memory_mb: int
    pids: int
    wall_seconds: int
    output_bytes: int
    disk_bytes: int


@dataclass(frozen=True)
class RunnerTask:
    """Staged inputs by opaque reference; the runner never sees host paths or secrets."""

    template_id: str
    command: tuple[str, ...]
    inputs: tuple[ArtifactRef, ...]
    limits: RunnerLimits
    network: Literal["none", "gateway"] = "none"


@dataclass(frozen=True)
class RunnerOutput:
    name: str
    checksum: str
    size_bytes: int


@dataclass(frozen=True)
class RunnerOutcome:
    """Exit report only. Outputs are staged; the file owner decides whether they are saved."""

    status: Literal["succeeded", "failed", "cancelled", "timeout", "limit", "unknown"]
    exit_code: int | None
    outputs: tuple[RunnerOutput, ...]
    stdout_tail: str


class TaskRunner(Protocol):
    """Bounded command runner (AW-04). Cancel on STOP/revoke; a late outcome is not saved."""

    async def run(self, admission: WorkAdmission, task: RunnerTask) -> RunnerOutcome: ...

    async def staged(self, admission: WorkAdmission, output: RunnerOutput) -> bytes: ...


@dataclass(frozen=True)
class HarnessTurn:
    """One bounded instruction to an agent harness session (AW-08)."""

    session_key: str
    instruction: str
    skill_ids: tuple[str, ...]
    files: tuple[ArtifactRef, ...]


@dataclass(frozen=True)
class HarnessEvent:
    kind: Literal["progress", "permission_ask", "result", "failed", "cancelled", "unknown"]
    detail: str
    capability_id: str | None = None


class AgentHarness(Protocol):
    """Replaceable execution adapter. Model calls only via ModelGateway; a permission ask
    becomes a ticket; ``close`` purges the harness session and caches."""

    async def turn(self, admission: WorkAdmission, turn: HarnessTurn) -> list[HarnessEvent]: ...

    async def cancel(self, session_key: str) -> None: ...

    async def close(self, session_key: str) -> None: ...
