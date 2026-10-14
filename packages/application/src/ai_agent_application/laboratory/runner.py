"""Scenario runner: fresh isolated test world per run, hard invariants, replayable report."""

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import ArtifactRef, Scope
from ai_agent_contracts.persona import Profile
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import LaboratoryReport

from ai_agent_application.gamemaster import StoryRule, StoryRules
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.moderation.local import secret_like
from ai_agent_application.storage import Principal, Storage

from .clock import VirtualClock
from .lab import LABORATORY, PERSONA, Lab
from .profile import REFERENCE_PROFILE
from .report import Finding, ScenarioReport

DEFAULT_RULES = StoryRules.layered(
    1,
    project=(
        StoryRule("no-harm", "project", "hard", "Нет насилия над людьми.", ("убить", "пытк")),
    ),
)


@dataclass(frozen=True)
class Scenario:
    scenario_id: str
    version: int
    title: str
    run: Callable[[Lab], Awaitable[None]]
    tags: tuple[str, ...] = ()


@dataclass
class LabRunner:
    """``scope_factory`` provisions a fresh TEST scope per run; ``index_factory`` a fresh index."""

    storage: Storage
    scope_factory: Callable[[str], Awaitable[Scope]]
    index_factory: Callable[[], RecallIndex]
    watch: Sequence[Scope] = ()
    rules: StoryRules = DEFAULT_RULES
    profile_markdown: str = REFERENCE_PROFILE
    principal: Principal = LABORATORY
    # share this clock with the store: every run starts at its fixed start, so reports replay
    clock: VirtualClock = field(default_factory=VirtualClock)
    reports: list[ScenarioReport] = field(default_factory=list)
    scopes: list[Scope] = field(default_factory=list)

    artifact_writer: Callable[[Scope, ScenarioReport], Awaitable[ArtifactRef]] | None = None

    async def run(
        self, scenario: Scenario, *, seed: str = "lab-seed", persist: bool = True
    ) -> ScenarioReport:
        if len(seed) > 128 or secret_like(seed):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        scope = await self.scope_factory(f"{scenario.scenario_id}-{seed}")
        if scope.memory_mode != "test":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if await self.storage.repository(Profile, PERSONA, scope).list(limit=1):
            raise ContractError(ErrorCode.VERSION_CONFLICT)
        self.scopes.append(scope)
        del self.scopes[:-64]
        self.clock.now = self.clock.start
        self.clock._scheduled.clear()
        lab = await Lab.create(
            self.storage,
            scope,
            seed=seed,
            index=self.index_factory(),
            profile_markdown=self.profile_markdown,
            rules=self.rules,
            clock=self.clock,
        )
        before = {s.memory_space_id: await lab.fingerprint(s) for s in self.watch}
        started = lab.clock()
        status: str = "passed"
        error = ""
        try:
            await scenario.run(lab)
        except Exception as exc:  # the report keeps the type only: messages may echo input
            status, error = "error", type(exc).__name__
        finished = lab.clock()
        self.clock.now = (
            self.clock.start
        )  # compare isolation at the same instant: TTLs must not look like writes
        lab.findings.extend(await self._invariants(lab, before))
        self.clock.now = finished
        if status != "error" and any(not f.ok and f.hard for f in lab.findings):
            status = "failed"
        report = ScenarioReport(
            scenario_id=scenario.scenario_id,
            scenario_version=scenario.version,
            seed=seed,
            started_at=started,
            finished_at=finished,
            status=status,  # type: ignore[arg-type]
            findings=tuple(
                Finding(
                    "REDACTED" if secret_like(f.name) else f.name,
                    f.ok,
                    "[REDACTED]" if secret_like(f.detail) else f.detail,
                    f.hard,
                )
                for f in lab.findings
            ),
            log=tuple(
                {"kind": "redacted", "reason": "SECRET_LIKE_DATA"}
                if secret_like(str(entry))
                else entry
                for entry in lab.log
            ),
            model_calls=len(lab.brain.calls),
            error=error,
        )
        if persist:
            await self._persist(lab, report)
        self.reports.append(report)
        del self.reports[:-64]
        return report

    async def _invariants(self, lab: Lab, before: dict[str, dict[str, int]]) -> list[Finding]:
        found = [
            Finding("TEST_SCOPE_ONLY", lab.scope.memory_mode == "test"),
            Finding(
                "NO_UNSCRIPTED_MODEL_CALLS",
                not lab.brain.unscripted,
                f"unscripted roles: {sorted(set(lab.brain.unscripted))}",
            ),
            Finding(
                "STOP_RESPECTED",
                lab.outputs_while_stopped == 0,
                f"{lab.outputs_while_stopped} outputs while stopped",
            ),
        ]
        for scope in self.watch:
            after = await lab.fingerprint(scope)
            same = before[scope.memory_space_id] == after
            found.append(Finding(f"ISOLATION:{scope.memory_mode}", same, "records changed"))
        leaked = secret_like("\n".join(str(entry) for entry in lab.log) + "\n" + str(lab.findings))
        found.append(Finding("NO_SECRETS_IN_REPORT", not leaked, "secret-like text in the log"))
        return found

    async def replay(self, scenario: Scenario, report: ScenarioReport) -> bool:
        """Run again with the same seed in a fresh world; the digest must match exactly."""
        again = await self.run(scenario, seed=report.seed, persist=False)
        return again.digest == report.digest

    async def _persist(self, lab: Lab, report: ScenarioReport) -> None:
        digest = report.digest
        record_id = f"labrep-{digest[:40]}"
        failed = len(report.failed)
        mapping = {"passed": "passed", "failed": "failed", "error": "unknown"}
        now = lab.clock()
        artifact = (
            None if self.artifact_writer is None else await self.artifact_writer(lab.scope, report)
        )
        record = LaboratoryReport.model_validate(
            {
                "id": record_id,
                "scope": lab.scope.model_dump(),
                "created_at": now,
                "updated_at": now,
                "version": 1,
                "scenario_id": report.scenario_id,
                "scenario_version": report.scenario_version,
                "operation_id": f"lab-{digest[:40]}",
                "status": mapping[report.status],
                "report_artifact": artifact,
                "summary": (
                    f"MOCK {report.status}: {len(report.findings)} checks, {failed} failed;"
                    f" seed {report.seed}; digest {digest[:12]}"
                ),
            }
        )
        command = MessageCommand(
            request_id=record_id,
            scope=lab.scope,
            target="laboratory",
            type="dialogue.message",
            arguments=MessageArguments(text="report"),
        )
        async with self.storage.transaction(self.principal, lab.scope, command) as unit:
            if unit.replay is None:
                await unit.put(record)
                await unit.complete(
                    Receipt(
                        request_id=record_id,
                        scope=lab.scope,
                        status="completed",
                        resource_id=record_id,
                        version=1,
                    )
                )


def derive(seed: str, *parts: str) -> str:
    """Stable short id for scenario authors (never random, never wall-clock)."""
    return hashlib.sha256("\0".join([seed, *parts]).encode()).hexdigest()[:12]
