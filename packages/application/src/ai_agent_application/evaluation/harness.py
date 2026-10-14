"""Evaluation harness: scenarios x repeats over the laboratory, with a recorder and a budget.

``Evaluation.dry`` runs against the canned stand-in and is always allowed. ``Evaluation.real``
needs a complete ``Authorisation`` and a provider the caller built; the harness itself never reads
``.env`` or opens a network connection. Every run is a fresh isolated TEST world.
"""

import resource
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from time import perf_counter, process_time

from ai_agent_contracts.common import SourceRef

from ai_agent_application.laboratory import Lab, LabRunner, Scenario
from ai_agent_application.laboratory.lab import assignment as lab_assignment
from ai_agent_application.laboratory.report import ScenarioReport
from ai_agent_application.llm import (
    Failure,
    GenerationParameters,
    LLMProvider,
    ResolvedAssignment,
)
from ai_agent_application.moderation.local import secret_like

from .budget import Authorisation, BudgetGuard, Price
from .model import (
    OPERATOR,
    Fact,
    Forget,
    Gap,
    Live,
    NewSession,
    ProviderFault,
    QualityScenario,
    Restart,
    Resume,
    Say,
    Stop,
    Trace,
    Turn,
)
from .providers import DryModel, Recorder
from .report import EvaluationReport, RunRecord

DEFAULT_REPEATS = 3
REDACTED = "[REDACTED]"


def _safe(text: str | None) -> str | None:
    return REDACTED if text is not None and secret_like(text) else text


@dataclass
class Evaluation:
    runner: LabRunner
    recorder: Recorder
    guard: BudgetGuard
    model: str
    mock: bool

    # ---- construction ------------------------------------------------------------------

    @classmethod
    def dry(cls, runner: LabRunner, *, price: Price | None = None) -> Evaluation:
        """Canned stand-in. ``price`` only previews what the token estimate would cost."""
        guard = BudgetGuard(price=price)
        recorder = Recorder(DryModel(), guard)
        return cls._wire(runner, recorder, guard, lab_assignment, "dry-run-stand-in", True)

    @classmethod
    def real(
        cls,
        runner: LabRunner,
        provider: LLMProvider,
        authorisation: Authorisation,
        resolve: Callable[[str], ResolvedAssignment],
    ) -> Evaluation:
        """Real model. Refused (``RealRunRefused``) without a complete authorisation."""
        authorisation.validate()
        guard = BudgetGuard(
            price=authorisation.price,
            ceiling=authorisation.ceiling,
            max_output_tokens=authorisation.max_output_tokens,
        )
        recorder = Recorder(provider, guard)
        return cls._wire(runner, recorder, guard, resolve, authorisation.model_label, False)

    @classmethod
    def _wire(
        cls,
        runner: LabRunner,
        recorder: Recorder,
        guard: BudgetGuard,
        resolve: Callable[[str], ResolvedAssignment],
        model: str,
        mock: bool,
    ) -> Evaluation:
        runner.providers = lambda _adapter: recorder
        runner.resolve = resolve
        runner.mock = mock
        return cls(runner, recorder, guard, model, mock)

    # ---- running -----------------------------------------------------------------------

    async def evaluate(
        self,
        scenarios: Sequence[QualityScenario],
        *,
        repeats: int = DEFAULT_REPEATS,
        seed_prefix: str = "q",
    ) -> EvaluationReport:
        started = datetime.now(UTC)
        runs: list[RunRecord] = []
        stopped = ""
        for scenario in scenarios:
            for repeat in range(1, repeats + 1):
                seed = f"{seed_prefix}-{repeat}"
                if stopped:
                    runs.append(self._skipped(scenario, repeat, seed, stopped))
                    continue
                runs.append(await self._run(scenario, repeat, seed))
                if self.guard.exhausted:
                    stopped = f"бюджет: {self.guard.reason}"
        peak_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return EvaluationReport(
            mode="dry-run (mock)" if self.mock else "real",
            mock=self.mock,
            model=self.model,
            started_at=started,
            repeats=repeats,
            scenarios=list(scenarios),
            runs=runs,
            budget=self.guard.as_dict(),
            resources={
                "peak_rss_mb": round(peak_kb / 1024, 1),
                "cpu_seconds": round(sum(r.cpu_seconds for r in runs), 3),
            },
            stopped_reason=stopped,
        )

    def _skipped(self, s: QualityScenario, repeat: int, seed: str, reason: str) -> RunRecord:
        return RunRecord(
            s.scenario_id, repeat, seed, "skipped", self.mock, self.model, skipped_reason=reason
        )

    async def _run(self, s: QualityScenario, repeat: int, seed: str) -> RunRecord:
        trace = Trace()
        self.recorder.reset(trace)
        scenario = Scenario(
            s.scenario_id, 1, s.title, lambda lab: self._play(lab, s, trace), (s.origin,)
        )
        cpu = process_time()
        report = await self.runner.run(scenario, seed=seed, persist=False)
        return self._record(s, repeat, seed, report, trace, process_time() - cpu)

    def _record(
        self,
        s: QualityScenario,
        repeat: int,
        seed: str,
        report: ScenarioReport,
        trace: Trace,
        cpu: float,
    ) -> RunRecord:
        return RunRecord(
            s.scenario_id,
            repeat,
            seed,
            report.status,
            self.mock,
            self.model,
            hard_failures=[f.name for f in report.findings if not f.ok and f.hard],
            soft_failures=[f.name for f in report.findings if not f.ok and not f.hard],
            error=report.error,
            digest=report.digest,
            turns=[t.as_dict() for t in trace.turns],
            calls=[c.as_dict() for c in trace.calls],
            events=list(trace.events),
            cpu_seconds=round(cpu, 3),
        )

    # ---- one scenario ------------------------------------------------------------------

    async def _play(self, lab: Lab, s: QualityScenario, trace: Trace) -> None:
        trace.baseline = await lab.fingerprint(lab.scope)
        facts: dict[str, SourceRef] = {}
        for index, step in enumerate(s.steps):
            if isinstance(step, Say):
                await self._say(lab, s, trace, index, step)
                continue
            trace.events.append({"index": index, "kind": type(step).__name__})
            match step:
                case Gap():
                    await lab.advance(days=step.days, hours=step.hours)
                case NewSession():
                    await lab.new_session()
                case Live():
                    await lab.episode(step.text)
                case Fact():
                    ref = await lab.source(step.text, author=step.author, api=not step.private)
                    if step.key:
                        facts[step.key] = ref
                case Forget():
                    ref = facts[step.key]
                    await lab.memory.delete_source(lab.command(), ref.source_id, ref.version)
                case Stop():
                    lab.stop()
                case Restart():
                    await lab.restart()
                case Resume():
                    lab.resume()
                case ProviderFault():
                    self.recorder.inject(Failure(step.failure), step.calls)
                case _:
                    raise TypeError("UNKNOWN_STEP")
        for check in s.checks:
            for finding in await check(lab, trace):
                lab.check(finding.name, finding.ok, finding.detail, hard=finding.hard)

    async def _say(self, lab: Lab, s: QualityScenario, trace: Trace, index: int, step: Say) -> None:
        visitor = None if step.who == OPERATOR else s.people[step.who]
        first_call = len(trace.calls)
        began = perf_counter()
        outcome = await lab.say(visitor, step.text, None, directed=step.directed)
        elapsed = (perf_counter() - began) * 1000
        usages = () if outcome is None else outcome.usage
        known = [u for u in usages if u.input_tokens is not None and u.output_tokens is not None]
        complete = bool(usages) and len(known) == len(usages)
        trace.turns.append(
            Turn(
                index=index,
                session=lab.session_no,
                who=step.who,
                text=_safe(step.text) or "",
                status=None if outcome is None else outcome.status,
                reason=None if outcome is None else outcome.reason,
                reply=None
                if outcome is None or outcome.draft is None
                else _safe(outcome.draft.text),
                dropped=() if outcome is None else outcome.dropped,
                input_tokens=sum(u.input_tokens or 0 for u in known) if complete else None,
                output_tokens=sum(u.output_tokens or 0 for u in known) if complete else None,
                usage_sources=tuple(sorted({u.source for u in usages})),
                latency_ms=round(elapsed, 3),
                call_ids=tuple(range(first_call, len(trace.calls))),
            )
        )


def character_assignment(
    model_id: str,
    *,
    connection_id: str,
    adapter_id: str,
    endpoint_setting: str,
    credential_setting: str,
    max_output_tokens: int,
    timeout_seconds: float = 60.0,
) -> ResolvedAssignment:
    """Assignment for a real run. Only the *names* of the settings are given, never the values."""
    return ResolvedAssignment(
        assignment_id="eval-character",
        assignment_version=1,
        role_id="character",
        connection_id=connection_id,
        adapter_id=adapter_id,
        model_id=model_id,
        endpoint_setting=endpoint_setting,
        credential_setting=credential_setting,
        parameters=GenerationParameters(
            max_output_tokens=max_output_tokens, timeout_seconds=timeout_seconds
        ),
        capability_verification="unverified",
    )
