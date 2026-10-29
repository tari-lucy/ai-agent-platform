"""Autonomous UTC-day memory processing through existing admission, jobs and domain owners."""

import hashlib
import json
import logging
from collections import Counter
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING, Literal

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.dialogue.context import data
from ai_agent_application.llm import ChatMessage, GenerationRequest
from ai_agent_application.memory.service import derived_id
from ai_agent_application.persona.schemas import DevelopmentBounds
from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.api import ProcessMemoryCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.control import ExecutionCommand, Utterance
from ai_agent_contracts.llm import LLMAttempt
from ai_agent_contracts.memory import Episode, MemoryCycle, Source, Summary
from ai_agent_contracts.persona import Profile, ReflectionCheckpoint
from ai_agent_contracts.states import ErrorCode, OperationState

from . import harness_memory
from .history import PrivateHistory
from .schemas import JobArguments, TaskInput
from .state import OPERATOR, PRINCIPALS, put

if TYPE_CHECKING:
    from .service import Execution, Runtime

MAX_INPUT_CHARS = 8000
MAX_EPISODES = 10
MAX_CYCLE_ATTEMPTS = 3  # the original batch and at most two continuations of it
# Calls still needed from a stage on: extract, consolidate, reflect (+2 story screens if mutable).
REMAINING_CALLS = {"extract": 3, "consolidate": 2, "reflect": 1}


def resume_key(scope: Scope, cycle: MemoryCycle) -> str:
    return derived_id("cycle", scope, cycle.id, "resume")


type Stage = Literal["extract", "consolidate", "reflect"]


def resume_stage(cycle: MemoryCycle) -> Stage:
    """The first stage without a saved outcome: earlier stages are never paid again."""
    if cycle.stage == "consolidate":
        return "consolidate"
    return "reflect" if cycle.stage == "reflect" else "extract"


def obstacle(
    cycle: MemoryCycle,
    cycles: list[MemoryCycle],
    attempts: list[LLMAttempt],
    resumed: bool,
) -> str | None:
    """Why a cycle may not continue, or None when its saved outcome makes it safe.

    Only an in-process failure that the journal recorded continues. An unknown outcome, an
    attempt of the model whose result is not known during the cycle, an earlier continuation
    or too many attempts on the same batch stop it; nothing is guessed.
    """
    if cycle.status == "unknown":
        return "OUTCOME_UNKNOWN"
    if cycle.status != "failed":
        return "NOT_FAILED"
    if resumed:
        return "ALREADY_RESUMED"
    if sum(1 for c in cycles if c.episode_ids == cycle.episode_ids) >= MAX_CYCLE_ATTEMPTS:
        return "RETRY_LIMIT"
    if any(
        a.status in ("unknown", "running") and cycle.created_at <= a.started_at <= cycle.updated_at
        for a in attempts
    ):
        return "ATTEMPT_UNKNOWN"
    return None


DIGEST_AGE = timedelta(days=30)  # summaries this old are folded into one digest per month
DIGEST_MIN = 3
TERMINAL = {
    OperationState.SUCCEEDED,
    OperationState.FAILED,
    OperationState.CANCELLED,
    OperationState.OUTCOME_UNKNOWN,
    OperationState.EXPIRED,
}


async def pairs(
    rt: Runtime, scope: Scope, episode_ids: list[str]
) -> tuple[list[Episode], list[Source]]:
    episodes, sources = [], {}
    for identifier in dict.fromkeys(episode_ids):
        episode = await rt.store.repository(Episode, OPERATOR, scope).get(
            identifier, purpose="api_transfer"
        )
        if episode is None or not episode.outcome_confirmed:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if harness_memory.is_harness(episode.operation_id):
            # doc33 D82: a harness conversation has no dialogue Job; its own sources are the
            # evidence, each current, with content and released for api_transfer.
            for ref in episode.sources:
                source = await rt.store.repository(Source, OPERATOR, scope).get(
                    ref.source_id, purpose="api_transfer"
                )
                if (
                    source is None
                    or source.version != ref.version
                    or source.content is None
                    or source.source_type not in harness_memory.SOURCE_TYPES
                ):
                    raise ContractError(ErrorCode.ACCESS_DENIED)
                sources[source.id] = source
            episodes.append(episode)
            continue
        for ref in episode.sources:
            current = await rt.store.repository(Source, OPERATOR, scope).get(
                ref.source_id, purpose="api_transfer"
            )
            if current is None or current.version != ref.version:
                raise ContractError(ErrorCode.ACCESS_DENIED)
        stored = await rt.jobs.store.load(episode.operation_id, episode.scope)
        if stored is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        original = await rt.store.repository(
            ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope
        ).get(stored[0].command_id)
        if original is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        args = rt.registry.validate(
            original.arguments.schema_id, original.arguments.schema_version, original.arguments.data
        )
        if not isinstance(args, JobArguments) or args.request.kind != "dialogue":
            raise ContractError(ErrorCode.ACCESS_DENIED)
        reply = await rt.store.repository(Utterance, OPERATOR, scope).get(
            derived_id("reply", scope, args.request.trigger_id), purpose="api_transfer"
        )
        if reply is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        own_id = derived_id(
            "src", scope, "external", "character-utterance", "private-panel", reply.id
        )
        refs = {ref.source_id for ref in episode.sources}
        for source_id in [*args.request.source_ids, own_id]:
            source = await rt.store.repository(Source, OPERATOR, scope).get(
                source_id, purpose="api_transfer"
            )
            if source_id not in refs or source is None or source.content is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            if await PrivateHistory(rt).evidence(scope, source) is None:
                raise ContractError(ErrorCode.ACCESS_DENIED)
            sources[source.id] = source
        episodes.append(episode)
    rows = sorted(sources.values(), key=lambda s: (s.occurred_at, s.created_at, s.id))
    if len(rows) > 20 or sum(len(s.content or "") for s in rows) > MAX_INPUT_CHARS:
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    return episodes, rows


async def reflection_input(ex: Execution, request: GenerationRequest) -> GenerationRequest:
    _, sources = await pairs(ex.runtime, ex.command.scope, ex.args.request.episode_ids)
    evidence = "\n".join(
        f'<source id="{s.id}" provenance="{s.provenance.value}" kind="{s.source_type}">'
        f"{data(s.content or '')}</source>"
        for s in sources
    )
    messages = (
        *request.messages[:-1],
        ChatMessage(
            "user",
            request.messages[-1].content
            + "\n<experience_sources>"
            + evidence
            + "</experience_sources>",
        ),
    )
    return replace(request, messages=messages)


class CycleJournal:
    def __init__(self, ex: Execution):
        self.ex, self.stage = ex, "extract"
        self.ids: list[str] = []

    async def get(self) -> MemoryCycle:
        ex = self.ex
        row = await ex.runtime.store.repository(
            MemoryCycle, PRINCIPALS[Owner.MEMORY], ex.command.scope
        ).get(ex.args.request.trigger_id)
        if row is None:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        return row

    async def enter(self) -> None:
        row = await self.get()
        if row.status != "queued":
            # A leased retry cannot repeat an external stage whose outcome is uncertain.
            raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        await self.save("extract", [])

    async def save(
        self, stage: str, ids: list[str], status: str = "running", error: ErrorCode | None = None
    ) -> None:
        ex, row = self.ex, await self.get()
        await ex.verify()
        now = ex.runtime.supervisor.clock.utc()
        updated = row.model_copy(
            update=dict(
                version=row.version + 1,
                updated_at=now,
                stage=stage,
                status=status,
                record_ids=sorted(set(ids)),
                error_code=error,
            )
        )
        await put(
            ex.runtime.store,
            ex.runtime.registry,
            Owner.MEMORY,
            ex.command.scope,
            f"cycle-{row.id}-{updated.version}",
            updated,
            row.version,
            ex.runtime.readers,
        )
        self.stage, self.ids = stage, ids
        ex.runtime.notify("memory.changed", row.scope, row.id, updated.version)


class DailyMemory:
    def __init__(self, runtime: Runtime):
        self.rt, self.next_check = runtime, 0.0
        self.last_error: ErrorCode | None = None

    async def cycles(self, scope: Scope) -> list[MemoryCycle]:
        result, after = [], None
        repo = self.rt.store.repository(MemoryCycle, PRINCIPALS[Owner.MEMORY], scope)
        for _ in range(50):
            rows = await repo.list(limit=200, after_id=after)
            result.extend(rows)
            if len(rows) < 200:
                return result
            after = rows[-1].id
        raise ContractError(ErrorCode.RESOURCE_BUSY)

    async def enqueue(
        self,
        ids: list[str],
        authority: ControlAuthority | None = None,
        manual_request: ProcessMemoryCommand | None = None,
        *,
        resume: MemoryCycle | None = None,
    ) -> Receipt:
        rt, scope = self.rt, self.rt.supervisor.snapshot.scope
        budget = rt.private_api_budget
        if budget is None or not budget.memory_processing or scope.memory_mode != "work":
            raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
        run = rt.supervisor.snapshot
        if (
            run.state.value != "ACTIVE"
            or run.activity.value != "OPEN"
            or run.autonomy != "autonomous"
        ):
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if authority is not None:
            rt.supervisor.authorize_operator(authority, scope)
        episodes, sources = await pairs(rt, scope, ids)
        key = (
            resume_key(scope, resume)
            if resume is not None
            else derived_id("cycle", scope, *sorted(e.id for e in episodes))
        )
        repo = rt.store.repository(MemoryCycle, PRINCIPALS[Owner.MEMORY], scope)
        if authority is not None and manual_request is not None:
            # Pin the HTTP id/payload before submitting any paid job. The canonical batch id
            # also deduplicates different request IDs and autonomous scheduling.
            admission = rt.supervisor.admit("reflection")
            try:
                with rt.store.fence(lambda: rt.supervisor.validate(admission), owner=Owner.MEMORY):
                    request = manual_request
                    async with rt.store.transaction(
                        PRINCIPALS[Owner.MEMORY], scope, request
                    ) as unit:
                        if unit.replay is None:
                            if request.expected_version != rt.supervisor.snapshot.version:
                                raise ContractError(ErrorCode.VERSION_CONFLICT)
                            await unit.complete(
                                Receipt(
                                    request_id=request.request_id,
                                    scope=scope,
                                    status="accepted",
                                    operation_id=derived_id("m3job", scope, key),
                                )
                            )
            finally:
                rt.supervisor.retire(admission)
        old = await repo.get(key)
        if old is not None and old.operation_id is not None:
            known = await rt.jobs.store.load(old.operation_id, old.scope)
            if known is not None:
                job = known[0]
                return Receipt(
                    request_id=key,
                    scope=scope,
                    status="completed"
                    if job.status == OperationState.SUCCEEDED
                    else "rejected"
                    if job.status in TERMINAL
                    else "accepted",
                    operation_id=job.operation_id,
                    resource_id=key,
                    version=old.version,
                )
            if old.status != "queued" or old.scope != scope:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
        if (
            resume is None
            and old is None
            and any(set(ids).intersection(c.episode_ids) for c in await self.cycles(scope))
        ):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        now = rt.supervisor.clock.utc()
        attempts = await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(scope)
        profile = await rt.store.repository(Profile, PRINCIPALS[Owner.PERSONA], scope).get(
            scope.persona_id
        )
        if profile is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        # Extract + consolidate + reflect, with room for validation and application
        # story screens when this profile admits autonomous changes. Do not start
        # another batch with only three calls left and strand its reflection.
        bounds = rt.registry.validate(
            profile.development_bounds.schema_id,
            profile.development_bounds.schema_version,
            profile.development_bounds.data,
        )
        if not isinstance(bounds, DevelopmentBounds):
            raise ContractError(ErrorCode.INVALID_ARGUMENTS)
        mutable = any(item.autonomous for item in bounds.bounds)
        start: Stage = resume_stage(resume) if resume is not None else "extract"
        required = REMAINING_CALLS[start] + (2 if mutable else 0)
        if (
            budget.memory_daily_calls - budget.memory_used(attempts, now) < required
            or budget.max_attempts - budget.used(attempts, now) < required
        ):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        latest = await rt.store.repository(
            ReflectionCheckpoint, PRINCIPALS[Owner.PERSONA], scope
        ).recent(limit=1)
        if latest and now - latest[0].created_at < timedelta(minutes=10):
            raise ContractError(ErrorCode.RESOURCE_BUSY)
        digest = hashlib.sha256(
            json.dumps(sorted((s.id, s.version) for s in sources)).encode()
        ).hexdigest()
        if old is not None and old.input_digest != digest:
            raise ContractError(ErrorCode.STALE_CONTEXT)
        if resume is not None and resume.input_digest != digest:
            # Changed sources: the saved claims were made from other versions; start afresh.
            raise ContractError(ErrorCode.STALE_CONTEXT)
        args = TaskInput(
            kind="post_session",
            trigger_id=key,
            source_ids=[s.id for s in sources],
            episode_ids=sorted(e.id for e in episodes),
            period_start=min(s.occurred_at for s in sources),
            period_end=max(s.occurred_at for s in sources),
            resume_stage=start,
            resume_ids=list(resume.record_ids) if resume is not None and start != "extract" else [],
            resumed=resume is not None,
        )
        token = rt.supervisor.admit("reflection")
        try:
            if old is None:
                row = MemoryCycle(
                    id=key,
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    episode_ids=args.episode_ids,
                    source_ids=args.source_ids,
                    input_digest=digest,
                    operation_id=derived_id("m3job", scope, key),
                    stage="queued",
                    status="queued",
                )
                with rt.store.fence(lambda: rt.supervisor.validate(token), owner=Owner.MEMORY):
                    await put(
                        rt.store,
                        rt.registry,
                        Owner.MEMORY,
                        scope,
                        "prepare-" + key,
                        row,
                        None,
                        rt.readers,
                    )
            job = await rt.submit(authority, key, args)
            rt.notify("memory.changed", scope, key)
            return Receipt(
                request_id=key,
                scope=scope,
                status="accepted",
                operation_id=job.operation_id,
                resource_id=key,
                version=1,
            )
        finally:
            rt.supervisor.retire(token)

    async def continue_failed(self, scope: Scope, cycles: list[MemoryCycle]) -> bool:
        """Submit the newest safely continuable failed cycle; True when one was submitted."""
        repo = self.rt.store.repository(MemoryCycle, PRINCIPALS[Owner.MEMORY], scope)
        attempts = await UsageLedger(
            self.rt.store, PRINCIPALS[Owner.LLM], self.rt.registry
        ).attempts(scope)
        failed = sorted(
            (c for c in cycles if c.status == "failed"), key=lambda c: c.updated_at, reverse=True
        )
        for cycle in failed:
            resumed = await repo.get(resume_key(scope, cycle)) is not None
            reason = obstacle(cycle, cycles, attempts, resumed)
            if reason is None:
                await self.enqueue(cycle.episode_ids, resume=cycle)
                logging.getLogger(__name__).info(
                    "Daily memory continue: stage=%s records=%d",
                    resume_stage(cycle),
                    len(cycle.record_ids),
                )
                return True
        return False

    async def digest_due(self, scope: Scope) -> bool:
        """Submit one knowledge digest for the oldest month of old summaries; True if submitted.

        A month needs at least three summaries older than 30 days; a digested set is never
        digested again (deterministic id) and a digest is never part of another one. One
        memory call is required within the same daily reserve.
        """
        from ai_agent_application.extraction.digest import digest_id, is_digest

        rt = self.rt
        budget = rt.private_api_budget
        if budget is None or not budget.memory_processing or scope.memory_mode != "work":
            return False
        now = rt.supervisor.clock.utc()
        attempts = await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(scope)
        if (
            budget.memory_daily_calls - budget.memory_used(attempts, now) < 1
            or budget.max_attempts - budget.used(attempts, now) < 1
        ):
            return False
        repo = rt.store.repository(Summary, PRINCIPALS[Owner.MEMORY], scope)
        months: dict[tuple[int, int], list[Summary]] = {}
        after = None
        for _ in range(50):
            rows = await repo.list(limit=200, after_id=after)
            for row in rows:
                if not is_digest(row) and row.period_end < now - DIGEST_AGE:
                    key = (row.period_end.year, row.period_end.month)
                    months.setdefault(key, []).append(row)
            if len(rows) < 200:
                break
            after = rows[-1].id
        for month in sorted(months):
            group = sorted(months[month], key=lambda s: (s.period_start, s.id))[:31]
            if len(group) < DIGEST_MIN:
                continue
            ids = [s.id for s in group]
            identifier = digest_id(scope, ids)
            if await repo.get(identifier) is not None:
                continue
            refs = list({r.source_id: r for s in group for r in s.sources})
            if len(refs) > 200:
                continue  # never submit a digest with only part of its grounds pinned
            args = TaskInput(
                kind="consolidate",
                trigger_id="digest-" + identifier.removeprefix("summary-")[:40],
                source_ids=refs,
                period_start=group[0].period_start,
                period_end=max(s.period_end for s in group),
                digest_of=ids,
            )
            token = rt.supervisor.admit("reflection")
            try:
                await rt.submit(None, args.trigger_id, args)
            finally:
                rt.supervisor.retire(token)
            logging.getLogger(__name__).info(
                "Daily memory digest: month=%04d-%02d summaries=%d", *month, len(ids)
            )
            return True
        return False

    async def tick(self) -> None:
        rt, run = self.rt, self.rt.supervisor.snapshot
        budget = rt.private_api_budget
        elapsed = rt.supervisor.clock.elapsed()
        if elapsed < self.next_check or budget is None or not budget.memory_processing:
            return
        if (
            run.state.value != "ACTIVE"
            or run.activity.value != "OPEN"
            or run.autonomy != "autonomous"
        ):
            return
        self.next_check = elapsed + 60
        try:
            if (await rt.jobs.store.active(limit=1)).items:
                return  # foreground work has priority
            cycles = await self.cycles(run.scope)
            # Reconcile metadata under current admission; never apply an old raw result.
            token = rt.supervisor.admit("reflection")
            try:
                for cycle in tuple(cycles):
                    if cycle.status not in ("queued", "running") or cycle.operation_id is None:
                        continue
                    known = await rt.jobs.store.load(cycle.operation_id, cycle.scope)
                    if (
                        cycle.scope != run.scope
                        or known is not None
                        and known[0].status in TERMINAL
                    ):
                        updated = cycle.model_copy(
                            update=dict(
                                version=cycle.version + 1,
                                updated_at=rt.supervisor.clock.utc(),
                                status="unknown",
                                error_code=ErrorCode.OUTCOME_UNKNOWN,
                            )
                        )
                        with rt.store.fence(
                            lambda: rt.supervisor.validate(token), owner=Owner.MEMORY
                        ):
                            await put(
                                rt.store,
                                rt.registry,
                                Owner.MEMORY,
                                cycle.scope,
                                f"cycle-reconcile-{cycle.id}-{updated.version}",
                                updated,
                                cycle.version,
                                rt.readers,
                            )
                        cycles[cycles.index(cycle)] = updated
                    elif known is None and cycle.status == "queued":
                        await self.enqueue(cycle.episode_ids)
                        return
            finally:
                rt.supervisor.retire(token)
            # A failed batch continues from its saved stage before any new batch is selected.
            if await self.continue_failed(run.scope, cycles):
                return
            processed = {
                identifier for c in cycles for identifier in c.episode_ids if c.status != "queued"
            }
            today = rt.supervisor.clock.utc().replace(hour=0, minute=0, second=0, microsecond=0)
            selected: list[str] = []
            deferred: Counter[str] = Counter()
            after = None
            repo = rt.store.repository(Episode, OPERATOR, run.scope)
            for _ in range(50):
                rows = await repo.list(limit=200, after_id=after, purpose="api_transfer")
                for row in rows:
                    if row.id in processed or row.created_at >= today:
                        continue
                    try:
                        await pairs(rt, run.scope, [*selected, row.id])
                    except ContractError as error:
                        deferred[error.error.code.value] += 1
                        continue
                    selected.append(row.id)
                    if len(selected) == MAX_EPISODES:
                        break
                if len(selected) == MAX_EPISODES or len(rows) < 200:
                    break
                after = rows[-1].id
            if selected or deferred:
                # Counts and fixed codes only: which experience waits, never what it contains.
                logging.getLogger(__name__).info(
                    "Daily memory batch: selected=%d deferred=%s",
                    len(selected),
                    ",".join(f"{k}:{v}" for k, v in sorted(deferred.items())) or "-",
                )
            if selected:
                await self.enqueue(selected)
            else:
                await self.digest_due(run.scope)  # only when no new experience waits
            self.last_error = None
        except ContractError as error:
            code = error.error.code
            if self.last_error != code:
                logging.getLogger(__name__).warning("Daily memory deferred: %s", code.value)
            self.last_error = code


async def manual(
    rt: Runtime, authority: ControlAuthority, request: ProcessMemoryCommand
) -> Receipt:
    run = rt.supervisor.snapshot
    if request.scope != run.scope or request.target != run.session_id:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    result = await rt.daily_memory.enqueue(request.arguments.episode_ids, authority, request)
    return result.model_copy(update={"request_id": request.request_id})
