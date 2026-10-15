"""Bounded autonomous internal game master for the private pilot, through existing owners.

One small path: confirmed experience -> one GM job (screened line, model draft, story screen) ->
one stored Opportunity -> the character sees only the offer in its next ordinary reply -> its
explicit decision by exact id/version -> only a later confirmed exchange resolves it as experience.

Nothing here is a second game master, scheduler or opportunity journal: proposals are written by
`GameMaster`, jobs by the job owner, usage by the ledger. The tick is read-only until it submits
one job, and every reason it skips is a fixed code (never text, prompts or ids of content).
"""

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from ai_agent_application.dialogue.context import data
from ai_agent_application.extraction.evidence import classify
from ai_agent_application.gamemaster import Dice, Family, GameMaster
from ai_agent_application.memory.service import derived_id
from ai_agent_application.storage import Owner
from ai_agent_application.usage import UsageLedger
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Delivery, ExecutionCommand, Utterance
from ai_agent_contracts.memory import Episode, Source
from ai_agent_contracts.persona import GmRuntimeState, Opportunity, StoryArc
from ai_agent_contracts.states import DeliveryState, ErrorCode, OpportunityState

from .schemas import TaskInput
from .state import OPERATOR, PRINCIPALS, rules_of

if TYPE_CHECKING:
    from ai_agent_application.dialogue.model import OfferDecision

    from .history import PrivateHistory
    from .service import Execution, Runtime

S = OpportunityState
LOG = logging.getLogger(__name__)
PAGE = 200
MAX_GROUNDS = 3
UNRESOLVED = (S.OFFERED, S.ACCEPTED, S.REALIZING)
ACCEPTED_IN = "accepted-in:"

# The only situation family of the private pilot: what can happen in this private conversation.
PRIVATE_FAMILIES = (
    Family(
        "private-conversation",
        "Тема, вопрос или небольшое размышление для продолжения приватного разговора",
        ("m3.dialogue",),
        variants=("вопрос собеседнику", "наблюдение из канона", "совместное размышление"),
    ),
)


async def _all[R: (Episode, Opportunity, StoryArc)](
    rt: Runtime, model: type[R], scope: Scope
) -> list[R]:
    owner = Owner.MEMORY if model is Episode else Owner.GM
    repo = rt.store.repository(model, PRINCIPALS[owner], scope)
    found: list[R] = []
    after = None
    for _ in range(25):
        page = (
            await repo.list_for_lifecycle(limit=PAGE, after_id=after)
            if model is Opportunity
            else await repo.list(limit=PAGE, after_id=after)
        )
        found.extend(page)
        if len(page) < PAGE:
            return found
        after = page[-1].id
    raise ContractError(ErrorCode.RESOURCE_BUSY)


def master(rt: Runtime, ex: Execution | None, scope: Scope, gm: GmRuntimeState) -> GameMaster:
    from ai_agent_application.memory import MemoryService

    run = rt.supervisor.snapshot
    return GameMaster(
        rt.store,
        PRINCIPALS[Owner.GM],
        rt.registry,
        MemoryService(rt.store, PRINCIPALS[Owner.MEMORY], rt.registry, reader_access=rt.readers),
        rules_of(gm, rt.registry),
        rt.families,
        Dice("dialogue-offer"),
        admitted=ex.allowed if ex is not None else (lambda: False),
        clock=rt.supervisor.clock.utc,
        reader_access=rt.readers,
        active_clock=rt.active_seconds,
        paused=run.gm_paused,
        intensity=gm.intensity,
    )


class AutonomousGM:
    """The runtime tick's GM step. At most one job submission per call; usually a no-op."""

    def __init__(self, runtime: Runtime) -> None:
        self.rt = runtime
        self.next_check = 0.0
        self.next_active: float | None = None
        self.last_reason: str | None = None

    def _skip(self, reason: str) -> str:
        if reason != self.last_reason:
            LOG.info("GM tick skipped: %s", reason)
        self.last_reason = reason
        return reason

    async def tick(self) -> str:
        rt = self.rt
        budget, run = rt.private_api_budget, rt.supervisor.snapshot
        if budget is None or not budget.gm_enabled:
            return "GM_DISABLED"
        elapsed = rt.supervisor.clock.elapsed()
        if elapsed < self.next_check:
            return "THROTTLED"
        self.next_check = elapsed + 30
        if (
            run.state.value != "ACTIVE"
            or run.activity.value != "OPEN"
            or run.autonomy != "autonomous"
        ):
            self.next_active = None  # a new active period starts with a full interval
            return self._skip("RUN_NOT_AUTONOMOUS")
        if run.scope.memory_mode != "work":
            return self._skip("NOT_WORK")
        if run.gm_paused:
            self.next_active = None
            return self._skip("GM_PAUSED")
        try:
            return await self._tick(run.scope)
        except ContractError as error:
            return self._skip("ERROR_" + error.error.code.value)

    async def _tick(self, scope: Scope) -> str:
        rt = self.rt
        budget = rt.private_api_budget
        assert budget is not None
        if (await rt.jobs.store.active(limit=1)).items:
            return self._skip("FOREGROUND_BUSY")  # dialogue and daily memory go first
        gm = await rt.state.gm(scope)
        if gm.intensity <= 0:
            return self._skip("INTENSITY_ZERO")
        active = rt.active_seconds()
        if self.next_active is None:
            self.next_active = active + budget.gm_interval_seconds  # no burst after (re)start
        if active < self.next_active:
            return self._skip("INTERVAL")
        now = rt.supervisor.clock.utc()
        await self._expire(scope, gm)
        opportunities = await _all(rt, Opportunity, scope)
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if sum(o.created_at >= today for o in opportunities) >= budget.gm_daily_offers:
            return self._skip("DAILY_OFFERS")
        if any(o.status in UNRESOLVED for o in opportunities):
            return self._skip("OFFER_PENDING")
        arcs = await _all(rt, StoryArc, scope)
        line = next(
            (
                a
                for a in sorted(arcs, key=lambda a: a.updated_at, reverse=True)
                if a.status == "open"
            ),
            None,
        )
        if line is None and arcs:
            return self._skip("LINE_NOT_OPEN")  # an operator pause/close is respected
        if line is None and rt.gm_line is None:
            return self._skip("NO_LINE")
        if line is not None and (
            line.cooldown_active_until is not None
            and active < line.cooldown_active_until
            or line.cooldown_until is not None
            and now < line.cooldown_until
        ):
            return self._skip("LINE_COOLDOWN")  # the model call would only be refused later
        needed = 2 if line is not None else 3  # opening a line is screened too
        attempts = await UsageLedger(rt.store, PRINCIPALS[Owner.LLM], rt.registry).attempts(scope)
        if budget.gm_room(attempts, now) < needed:
            return self._skip("BUDGET")
        used = {g.source_id for o in opportunities for g in o.grounds}
        grounds = await self._experience(scope, used)
        if not grounds:
            return self._skip("NO_EXPERIENCE")
        slot = await self._slot(scope, now.date())
        if slot is None:
            return self._skip("SLOTS_EXHAUSTED")
        from .service import gm_request_id

        request_id = gm_request_id(now.date(), slot)
        direction, boundaries = ("", "") if line is not None else rt.gm_line or ("", "")
        await rt.submit(
            None,
            request_id,
            TaskInput(
                kind="gm",
                trigger_id=request_id,
                source_ids=[g.source_id for g in grounds],
                arc_id=line.id if line is not None else None,
                direction=direction,
                boundaries=boundaries,
                seed=request_id,
            ),
        )
        self.next_active = active + budget.gm_interval_seconds
        self.last_reason = None
        LOG.info(
            "GM tick submitted one job: grounds=%d line=%s", len(grounds), "open" if line else "new"
        )
        return "SUBMITTED"

    async def _expire(self, scope: Scope, gm: GmRuntimeState) -> None:
        rt = self.rt
        token = rt.supervisor.admit("gm")
        try:
            with rt.store.fence(lambda: rt.supervisor.validate(token), owner=Owner.GM):
                expired = await master(rt, None, scope, gm).expire_due(scope)
        finally:
            rt.supervisor.retire(token)
        if expired:
            LOG.info("GM offers expired: %d", len(expired))

    async def _experience(self, scope: Scope, used: set[str]) -> list[SourceRef]:
        """Independent first-hand sources of the newest confirmed exchanges, not used before."""
        rt = self.rt
        episodes = sorted(
            (e for e in await _all(rt, Episode, scope) if e.outcome_confirmed),
            key=lambda e: e.created_at,
            reverse=True,
        )
        picked: list[SourceRef] = []
        for episode in episodes[:MAX_GROUNDS]:
            for ref in episode.sources:
                if ref.source_id in used or any(p.source_id == ref.source_id for p in picked):
                    continue
                source = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], scope).get(
                    ref.source_id, purpose="api_transfer"
                )
                if (
                    source is None
                    or source.version != ref.version
                    or source.availability != "available"
                    or not source.api_transfer_allowed
                ):
                    continue
                verdict = classify(source, scope)
                if verdict.independent and verdict.reject is None:
                    picked.append(ref)
            if len(picked) >= MAX_GROUNDS:
                break
        return picked[:MAX_GROUNDS]

    async def _slot(self, scope: Scope, day: date) -> int | None:
        from .service import GM_SLOTS, gm_request_id

        repo = self.rt.store.repository(ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope)
        for slot in range(1, GM_SLOTS + 1):
            if await repo.get(derived_id("m3cmd", scope, gm_request_id(day, slot))) is None:
                return slot
        return None


# ---- the character's side ---------------------------------------------------------------------


@dataclass(frozen=True)
class ShownOffer:
    opportunity_id: str
    version: int
    grounds: tuple[SourceRef, ...]


async def offer_for_context(
    rt: Runtime, scope: Scope, history: PrivateHistory
) -> tuple[str, ShownOffer] | None:
    """The one live offer the character may see: no line direction, lesson, family or rolls."""
    budget, run = rt.private_api_budget, rt.supervisor.snapshot
    if (
        budget is None
        or not budget.gm_enabled
        or scope.memory_mode != "work"
        or run.autonomy != "autonomous"
        or run.gm_paused
    ):
        return None
    now = rt.supervisor.clock.utc()
    live = [
        o
        for o in await _all(rt, Opportunity, scope)
        if o.status is S.OFFERED and o.expires_at > now
    ]
    for offer in sorted(live, key=lambda o: o.created_at, reverse=True)[:1]:
        grounds: list[SourceRef] = []
        for ref in offer.grounds:
            source = await rt.store.repository(Source, OPERATOR, scope).get(
                ref.source_id, purpose="api_transfer"
            )
            if source is None or source.version != ref.version:
                return None  # a withdrawn ground closes the offer for the character too
            evidence = await history.evidence(scope, source)
            if evidence is None:
                return None
            grounds.extend(evidence)
        if not history.remember(tuple(grounds)):
            return None
        block = (
            f'<offer id="{data(offer.id)}" version="{offer.version}"'
            f' until="{offer.expires_at.isoformat(timespec="minutes")}">'
            f"{data(offer.applicability)} | границы: {data(offer.boundaries)}</offer>"
        )
        return block, ShownOffer(offer.id, offer.version, tuple(grounds))
    return None


async def apply_decision(
    ex: Execution, shown: ShownOffer | None, decision: OfferDecision | None, utterance_id: str
) -> str:
    """The owner's transition for the explicit choice; never inferred from the reply text."""
    if decision is None:
        return "NONE"
    if shown is None or (decision.opportunity_id, decision.version) != (
        shown.opportunity_id,
        shown.version,
    ):
        LOG.info("GM offer decision dropped: NOT_SHOWN")
        return "NOT_SHOWN"
    rt, scope = ex.runtime, ex.command.scope
    gm = await rt.state.gm(scope)
    service = master(rt, ex, scope, gm)
    current = await rt.store.repository(Opportunity, PRINCIPALS[Owner.GM], scope).get_for_lifecycle(
        decision.opportunity_id
    )
    if current is None or current.version != decision.version:
        LOG.info("GM offer decision dropped: VERSION")
        return "VERSION"
    try:
        await ex.verify()
        if decision.decision == "accept":
            await service.accept(scope, current.id, choice=ACCEPTED_IN + utterance_id)
        else:
            await service.decline(scope, current.id)
    except ContractError as error:
        LOG.info("GM offer decision refused: %s", error.error.code.value)
        return "REFUSED_" + error.error.code.value
    LOG.info("GM offer decision applied: %s", decision.decision)
    return decision.decision.upper()


async def resolve_accepted(ex: Execution, incoming: Source) -> list[str]:
    """An accepted offer becomes experience only after its reply was really delivered and the
    person then wrote again: that next message is the independent evidence of the outcome."""
    rt, scope = ex.runtime, ex.command.scope
    resolved: list[str] = []
    accepted = [
        o
        for o in await _all(rt, Opportunity, scope)
        if o.status is S.ACCEPTED and (o.choice or "").startswith(ACCEPTED_IN)
    ]
    if not accepted:
        return resolved
    gm = await rt.state.gm(scope)
    service = master(rt, ex, scope, gm)
    for offer in accepted:
        utterance_id = (offer.choice or "")[len(ACCEPTED_IN) :]
        reply = await rt.store.repository(Utterance, PRINCIPALS[Owner.GATEWAY], scope).get(
            utterance_id
        )
        delivery = await rt.store.repository(Delivery, PRINCIPALS[Owner.CHANNEL], scope).get(
            derived_id("panel-delivery", scope, utterance_id)
        )
        if (
            reply is None
            or delivery is None
            or delivery.status is not DeliveryState.DELIVERED
            or delivery.revision != reply.revision
            or incoming.occurred_at <= delivery.updated_at
        ):
            continue
        try:
            await ex.verify()
            await service.resolve(
                scope,
                offer.id,
                "Персонаж принял предложение сюжета в доставленном ответе.",
                "Ответ доставлен и подтверждён; собеседник продолжил разговор новым сообщением.",
                SourceRef(
                    source_id=incoming.id,
                    version=incoming.version,
                    scope=scope,
                    availability="available",
                ),
            )
            resolved.append(offer.id)
        except ContractError as error:
            LOG.info("GM offer resolution refused: %s", error.error.code.value)
    return resolved
