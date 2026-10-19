"""Private admitted dialogue through the accepted owners; no publisher or implicit grants."""

from collections.abc import Sequence
from contextlib import ExitStack
from dataclasses import replace
from typing import Any, Literal

from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.dialogue import (
    BuiltContext,
    ContextBuilder,
    ControlSnapshot,
    DialogueController,
    Signal,
    SourceRights,
    Turn,
)
from ai_agent_application.dialogue.model import DialogueDecision, SectionInfo
from ai_agent_application.llm import ChatMessage
from ai_agent_application.memory import EpisodeDraft, MemoryService, SourceDraft
from ai_agent_application.memory.recall import estimate_tokens
from ai_agent_application.memory.service import derived_id
from ai_agent_application.social import SocialService
from ai_agent_application.storage import Owner, Principal, ReadPurpose
from ai_agent_contracts.api import (
    AcknowledgeCommand,
    MessageCommand,
    Receipt,
    ReflectCommand,
    StreamChunk,
    StreamTerminal,
)
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Delivery, ExecutionCommand, Utterance
from ai_agent_contracts.memory import Source
from ai_agent_contracts.persona import CharacterState, Profile
from ai_agent_contracts.states import DeliveryState, ErrorCode, Provenance

from .gm_cycle import ShownOffer, apply_decision, offer_for_context, resolve_accepted
from .history import PrivateHistory, PrivateRecall
from .schemas import JobArguments, TaskInput
from .service import DOMAIN_OWNERS, Execution, Runtime
from .state import PRINCIPALS, command, put


class Profiles:
    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def get_profile(self, scope: Scope, version: int | None = None) -> Profile | None:
        return await self.rt.store.repository(
            Profile, Principal("operator", "operator"), scope
        ).get(scope.persona_id, version=version, purpose="api_transfer")

    async def get_state(self, scope: Scope) -> CharacterState | None:
        return await self.rt.store.repository(
            CharacterState, Principal("operator", "operator"), scope
        ).get("state:" + scope.persona_id, purpose="api_transfer")


class Memory(MemoryService):
    async def get_source(
        self, scope: Scope, source_id: str, *, purpose: ReadPurpose = "private"
    ) -> Source | None:
        return await self.storage.repository(Source, Principal("operator", "operator"), scope).get(
            source_id, purpose="api_transfer"
        )


class Context(ContextBuilder):
    def __init__(self, execution: Execution, *args: Any) -> None:
        super().__init__(*args)
        self.execution = execution
        self.history = PrivateHistory(execution.runtime)
        self.shown_offer: ShownOffer | None = None
        assert isinstance(self.recall, PrivateRecall)
        self.recall.history = self.history

    async def build(
        self,
        scope: Scope,
        signal: Signal,
        *,
        person_id: str | None,
        incoming: SourceRef | None,
        turns: Sequence[Turn],
        capabilities: Sequence[str],
    ) -> BuiltContext:
        recent = await self.history.recent(scope, incoming, self.policy.recent_turns)
        built = await super().build(
            scope,
            signal,
            person_id=person_id,
            incoming=incoming,
            turns=(*recent, *turns),
            capabilities=capabilities,
        )
        from .long_term import include_memory

        built = await include_memory(
            self.execution.runtime, scope, built, self.history, signal.text, self.policy
        )
        from .biography import include_biography

        built = await include_biography(
            self.execution.runtime, scope, built, self.history, self.policy
        )
        built = await self._offer(scope, built)
        refs = {**self.history.sources, **{ref.source_id: ref for ref in built.manifest.sources}}
        if incoming is not None:
            refs[incoming.source_id] = incoming
        self.execution.extra_sources = tuple(refs.values())
        await self.execution.verify()
        return replace(built, manifest=replace(built.manifest, sources=tuple(refs.values())))

    async def _offer(self, scope: Scope, built: BuiltContext) -> BuiltContext:
        """At most one live GM offer, within the remaining context budget; never the plan."""
        found = await offer_for_context(self.execution.runtime, scope, self.history)
        if found is None:
            return built
        block, shown = found
        cost = estimate_tokens("\n" + block)
        if built.manifest.estimated_tokens + cost > self.policy.max_tokens:
            return built
        self.shown_offer = shown
        last = built.messages[-1]
        return replace(
            built,
            messages=(*built.messages[:-1], ChatMessage(last.role, last.content + "\n" + block)),
            manifest=replace(
                built.manifest,
                estimated_tokens=built.manifest.estimated_tokens + cost,
                sections=(*built.manifest.sections, SectionInfo("offer", 1, 0)),
            ),
        )


class Recorder:
    """The controller's generator, also keeping the closed decision for the offer transition."""

    def __init__(self, execution: Execution) -> None:
        self.execution = execution
        self.decision: DialogueDecision | None = None

    async def generate(self, request: Any, cancel: Any = None, on_delta: Any = None) -> Any:
        result = await self.execution.generate(request, cancel, on_delta=on_delta)
        if isinstance(result.parsed, DialogueDecision):
            self.decision = result.parsed
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.execution, name)


class View:
    def __init__(self, execution: Execution) -> None:
        self.execution = execution

    def snapshot(self) -> ControlSnapshot:
        rt = self.execution.runtime
        rt.supervisor.poll()
        run = rt.supervisor.snapshot
        return ControlSnapshot(run.state, run.activity, run.autonomy, self.execution.allowed())


class Stream:
    def __init__(self, execution: Execution, utterance_id: str) -> None:
        self.execution, self.utterance_id, self.sequence = execution, utterance_id, 0

    def event(self) -> dict[str, Any]:
        self.sequence += 1
        ex = self.execution
        return dict(
            id=derived_id("stream", ex.command.scope, ex.lease.attempt_id, str(self.sequence)),
            scope=ex.command.scope,
            created_at=ex.runtime.supervisor.clock.utc(),
            generation_id=ex.command.operation_id,
            utterance_id=self.utterance_id,
            attempt_id=ex.current_attempt or ex.lease.attempt_id,
            sequence=self.sequence,
        )

    async def emit(self, event_id: str, text: str) -> None:
        await self.execution.verify()
        self.execution.runtime.events.private_stream(
            StreamChunk(**self.event(), type="generation.chunk", data_type="text", data=text)
        )

    async def close(self, event_id: str, status: str) -> None:
        # Controller's "drafted" is not a saved final. Only perform() can confirm persistence.
        if status != "drafted":
            self.finish("cancelled" if status == "cancelled" else "incomplete")

    def finish(
        self, status: Literal["completed", "cancelled", "incomplete"], saved: str | None = None
    ) -> None:
        self.execution.runtime.events.private_stream(
            StreamTerminal(
                **self.event(), type="generation.terminal", status=status, saved_result_id=saved
            )
        )


def memory(rt: Runtime) -> Memory:
    return Memory(
        rt.store,
        PRINCIPALS[Owner.MEMORY],
        rt.registry,
        index=rt.dialogue_index,
        clock=rt.supervisor.clock.utc,
        reader_access=rt.readers,
    )


async def perform(ex: Execution) -> list[str]:
    rt, scope, request = ex.runtime, ex.command.scope, ex.args.request
    if rt.dialogue_index is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    # A real adapter also needs an explicit bounded private-pilot admission.
    roles = await rt.state.roles(scope)
    binding = await rt.state.binding(scope, roles["character"])
    if binding.adapter_id != "mock" and (
        binding.adapter_id not in rt.policy.allowed_adapters
        or rt.private_api_budget is None
        or binding.connection_id != rt.private_api_budget.connection_id
    ):
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    identifier = derived_id("reply", scope, request.trigger_id)
    old = await rt.store.repository(Utterance, PRINCIPALS[Owner.GATEWAY], scope).get(identifier)
    if old is not None:
        return [old.id]  # a saved final is never regenerated after an executor retry
    mem = memory(rt)
    social = SocialService(
        rt.store, PRINCIPALS[Owner.SOCIAL], rt.registry, clock=rt.supervisor.clock.utc
    )
    stream = Stream(ex, identifier)
    recorder = Recorder(ex)
    context = Context(
        ex,
        Profiles(rt),
        social,
        PrivateRecall(
            rt.store,
            Principal("operator", "operator"),
            rt.dialogue_index,
            clock=rt.supervisor.clock.utc,
        ),
        mem,
    )
    controller = DialogueController(
        scope,
        recorder,
        context,
        social,
        mem,
        View(ex),
        rt.supervisor.clock.utc,
        stream=stream,
    )
    incoming = await mem.get_source(scope, request.source_ids[0])
    if incoming is None or incoming.content is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if rt.private_api_budget is not None and rt.private_api_budget.gm_enabled:
        await resolve_accepted(ex, incoming)
    controller.submit(
        Signal(
            event_id=derived_id("msg", scope, request.trigger_id),
            kind="operator_message",
            text=incoming.content,
            occurred_at=incoming.occurred_at,
            directed=True,
            rights=SourceRights(read=True, api_transfer=True),
        )
    )
    try:
        outcome = await controller.step(ex.cancel)
        if outcome is None or outcome.status not in ("drafted", "silent"):
            raise ContractError(
                ErrorCode.STALE_CONTEXT if not ex.allowed() else ErrorCode.CAPABILITY_UNAVAILABLE
            )
        if outcome.draft is None:
            return []
        draft = outcome.draft
        await ex.verify()
        now = rt.supervisor.clock.utc()
        saved = Utterance(
            id=identifier,
            utterance_id=identifier,
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            revision=1,
            content=draft.text,
            content_hash=draft.content_hash,
            audience_id="operator-panel",
            channel_ids=["operator-panel"],
            moderation_status="draft",
            sources=list(
                {ref.source_id: ref for ref in (*draft.evidence, *ex.extra_sources)}.values()
            ),
            operation_id=ex.command.operation_id,
        )
        await put(
            rt.store,
            rt.registry,
            Owner.GATEWAY,
            scope,
            "save-" + identifier,
            saved,
            None,
            rt.readers,
        )
        queued = Delivery(
            id=derived_id("panel-delivery", scope, identifier),
            scope=scope,
            created_at=now,
            updated_at=now,
            version=1,
            utterance_id=identifier,
            revision=1,
            channel_id="operator-panel",
            attempt_id=ex.lease.attempt_id,
            status=DeliveryState.QUEUED,
        )
        await put(
            rt.store,
            rt.registry,
            Owner.CHANNEL,
            scope,
            "queue-" + identifier,
            queued,
            None,
            rt.readers,
        )
        await ex.verify()
        decision = recorder.decision.offer if recorder.decision is not None else None
        await apply_decision(ex, context.shown_offer, decision, identifier)
        stream.finish("completed", identifier)
        return [identifier]
    except BaseException:
        stream.finish("cancelled" if not ex.allowed() else "incomplete")
        raise


async def intake(rt: Runtime, authority: ControlAuthority, request: MessageCommand) -> TaskInput:
    from hashlib import sha256

    scope = request.scope
    event_id = derived_id("msg", scope, request.request_id)
    source_id = derived_id("src", scope, "external", "operator-message", "dialogue", event_id)
    mem = memory(rt)
    old = await mem.get_source(scope, source_id)
    if old is not None and (
        old.content is None
        or sha256(old.content.encode()).hexdigest()
        != sha256(request.arguments.text.encode()).hexdigest()
    ):
        raise ContractError(ErrorCode.IDEMPOTENCY_CONFLICT)
    existing = await rt.store.repository(ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope).get(
        derived_id("m3cmd", scope, request.request_id)
    )
    args = TaskInput(kind="dialogue", trigger_id=request.request_id, source_ids=[source_id])
    if existing is not None:
        return args  # runtime.submit checks the original pins/payload and returns the known job
    admission = rt.supervisor.admit("dialogue")

    def guard() -> None:
        rt.supervisor.authorize_operator(authority, scope)
        rt.supervisor.validate(admission)

    try:
        await rt.policy.permit(scope, "dialogue")
        with rt.store.fence(guard, owner=Owner.MEMORY):
            await mem.register_source(
                command(scope, derived_id("incoming-write", scope, request.request_id)),
                SourceDraft(
                    provenance=Provenance.SIMULATION
                    if scope.memory_mode == "test"
                    else Provenance.OPERATOR_INPUT,
                    source_type="operator-message",
                    adapter_id="dialogue",
                    adapter_version="1",
                    occurred_at=old.occurred_at if old is not None else rt.supervisor.clock.utc(),
                    content=request.arguments.text,
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                    external_id=event_id,
                ),
            )
        guard()
        return args
    finally:
        rt.supervisor.retire(admission)


async def mutate(
    rt: Runtime,
    authority: ControlAuthority,
    request: MessageCommand | AcknowledgeCommand | ReflectCommand,
) -> Receipt:
    rt.supervisor.authorize_operator(authority, request.scope)
    if rt.dialogue_index is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)
    if request.scope != rt.supervisor.snapshot.scope:
        raise ContractError(ErrorCode.STALE_CONTEXT)
    if isinstance(request, (MessageCommand, ReflectCommand)):
        if request.target != rt.supervisor.snapshot.session_id:
            raise ContractError(ErrorCode.ACCESS_DENIED)
        if isinstance(request, MessageCommand):
            if not request.arguments.text.strip() or len(request.arguments.text) > 4000:
                raise ContractError(ErrorCode.INVALID_ARGUMENTS)
            args = await intake(rt, authority, request)
        else:
            if request.expected_version != rt.supervisor.snapshot.version:
                raise ContractError(ErrorCode.VERSION_CONFLICT)
            args = TaskInput(
                kind="reflect",
                trigger_id=request.request_id,
                episode_ids=request.arguments.episode_ids,
            )
        job = await rt.submit(authority, request.request_id, args)
        rt.notify("job.changed", job.scope, job.operation_id, job.version)
        return Receipt(
            request_id=request.request_id,
            scope=request.scope,
            status="accepted",
            operation_id=job.operation_id,
            version=job.version,
        )
    return await acknowledge(rt, authority, request)


async def acknowledge(
    rt: Runtime, authority: ControlAuthority, request: AcknowledgeCommand
) -> Receipt:
    scope = request.scope
    principal = Principal(authority.principal_id, "operator")
    utterance = await rt.store.repository(Utterance, principal, scope).get(
        request.target, purpose="api_transfer"
    )
    if (
        utterance is None
        or utterance.scope != scope
        or utterance.audience_id != "operator-panel"
        or utterance.channel_ids != ["operator-panel"]
        or utterance.operation_id is None
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if (utterance.version, utterance.revision) != (
        request.expected_version,
        request.arguments.revision,
    ):
        raise ContractError(ErrorCode.VERSION_CONFLICT)
    stored = await rt.jobs.store.load(utterance.operation_id, scope)
    if stored is None:
        raise ContractError(ErrorCode.STALE_CONTEXT)
    original = await rt.store.repository(ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope).get(
        stored[0].command_id
    )
    if original is None:
        raise ContractError(ErrorCode.STALE_CONTEXT)
    job_args = rt.registry.validate(
        original.arguments.schema_id, original.arguments.schema_version, original.arguments.data
    )
    if not isinstance(job_args, JobArguments) or job_args.request.kind != "dialogue":
        raise ContractError(ErrorCode.ACCESS_DENIED)
    admission = rt.supervisor.admit("dialogue")
    pinned = original.model_copy(
        update={
            "freshness": admission.freshness,
            "cancellation_id": admission.id,
            "sources": utterance.sources,
        }
    )

    def guard() -> None:
        rt.supervisor.authorize_operator(authority, scope)
        rt.supervisor.validate(admission)

    try:
        await rt.policy.check(pinned, None)
        with ExitStack() as stack:
            for owner in DOMAIN_OWNERS:
                stack.enter_context(rt.store.fence(guard, owner=owner))
            stack.enter_context(
                rt.store.authorize_writes(
                    lambda c: rt.policy.transaction_check(pinned, c), owners=DOMAIN_OWNERS
                )
            )
            mem = memory(rt)
            # Current authoritative source rights are checked again; deleted grounds cannot revive.
            for ref in utterance.sources:
                source = await mem.get_source(scope, ref.source_id)
                if source is None or source.version != ref.version:
                    raise ContractError(ErrorCode.ACCESS_DENIED)
            delivery_id = derived_id("panel-delivery", scope, utterance.id)
            delivery = await rt.store.repository(Delivery, PRINCIPALS[Owner.CHANNEL], scope).get(
                delivery_id
            )
            if delivery is None:
                raise ContractError(ErrorCode.OUTCOME_UNKNOWN)
            saying = await mem.register_source(
                command(scope, "received-" + utterance.id),
                SourceDraft(
                    provenance=Provenance.SIMULATION
                    if scope.memory_mode == "test"
                    else Provenance.OBSERVATION,
                    source_type="character-utterance",
                    adapter_id="private-panel",
                    adapter_version="1",
                    occurred_at=utterance.created_at,
                    content=utterance.content,
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                    external_id=utterance.id,
                ),
            )
            own = SourceRef(
                source_id=saying.source_id,
                version=saying.version,
                scope=scope,
                availability="available",
            )
            episode, _ = await mem.record_episode(
                command(scope, "episode-" + utterance.id),
                EpisodeDraft(
                    sources=(*utterance.sources, own),
                    experience_source_ids=tuple(
                        dict.fromkeys((*job_args.request.source_ids, own.source_id))
                    ),
                    participant_ids=(),
                    choice="Ответил оператору.",
                    actual_outcome="Приватная панель подтвердила получение редакции.",
                    operation_id=utterance.operation_id,
                    provenance="simulation" if scope.memory_mode == "test" else "observation",
                ),
            )
            async with rt.store.transaction(PRINCIPALS[Owner.CHANNEL], scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                written_version = None
                if delivery.status != DeliveryState.DELIVERED:
                    updated = delivery.model_copy(
                        update={
                            "version": delivery.version + 1,
                            "updated_at": rt.supervisor.clock.utc(),
                            "completed_at": rt.supervisor.clock.utc(),
                            "status": DeliveryState.DELIVERED,
                        }
                    )
                    written_version = updated.version
                    await unit.put(
                        updated, expected_version=delivery.version, access=rt.readers or None
                    )
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=scope,
                    status="completed",
                    resource_id=delivery_id if written_version is not None else None,
                    operation_id=utterance.operation_id,
                    version=written_version,
                )
                await unit.complete(receipt)
            rt.notify("delivery.changed", scope, delivery_id)
            rt.notify("memory.changed", scope, episode)
            return receipt
    finally:
        rt.supervisor.retire(admission)
