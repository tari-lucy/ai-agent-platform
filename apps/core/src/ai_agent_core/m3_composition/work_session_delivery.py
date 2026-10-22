"""Private work replies: release a draft, then confirm exact delivery through existing owners."""

from contextlib import ExitStack
from typing import TYPE_CHECKING

from ai_agent_application.agent_activity.sessions import SessionBook
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import EpisodeDraft, SourceDraft, derived_id
from ai_agent_application.storage import Owner, Principal
from ai_agent_contracts.agent_sessions import SessionReplyOrigin, WorkSession
from ai_agent_contracts.api import AcknowledgeCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.control import Delivery, ExecutionCommand, Utterance
from ai_agent_contracts.panel import WorkSessionDraft
from ai_agent_contracts.states import DeliveryState, ErrorCode, OperationState, Provenance

from .dialogue import memory
from .schemas import JobArguments
from .state import OPERATOR, PRINCIPALS, command, put
from .work_session_runtime import AUDIENCE, ORIGIN
from .workplace_experience import ticket_release

if TYPE_CHECKING:
    from .service import Runtime

SOURCE_TYPE = "work-session-reply"


def origin(rt: Runtime, utterance: Utterance) -> SessionReplyOrigin:
    if (
        utterance.audience_id != AUDIENCE
        or utterance.channel_ids != [AUDIENCE]
        or utterance.origin is None
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    if utterance.origin.schema_id != ORIGIN:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    result = rt.registry.validate(ORIGIN, utterance.origin.schema_version, utterance.origin.data)
    if not isinstance(result, SessionReplyOrigin):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    return result


async def draft(rt: Runtime, session: WorkSession) -> WorkSessionDraft | None:
    pending = session.pending_reply
    if pending is None:
        return None
    utterance = await rt.store.repository(Utterance, OPERATOR, session.scope).get(
        pending.utterance_id, purpose="api_transfer"
    )
    text = None
    acknowledgeable = False
    if utterance is not None and (utterance.version, utterance.revision) == (
        pending.version,
        pending.revision,
    ):
        details = origin(rt, utterance)
        if (
            details.session_id == session.id
            and details.generation == session.generation
            and await ticket_release(rt).bases_open(
                session.scope, utterance.sources, details.context_files, "api_transfer"
            )
        ):
            text = utterance.content
            run = rt.supervisor.snapshot
            if utterance.operation_id is not None:
                loaded = await rt.jobs.store.load(utterance.operation_id, utterance.scope)
                original = (
                    None
                    if loaded is None
                    else await rt.store.repository(
                        ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], utterance.scope
                    ).get(loaded[0].command_id)
                )
                acknowledgeable = (
                    loaded is not None
                    and loaded[0].status == OperationState.SUCCEEDED
                    and original is not None
                    and original.freshness.control_epoch == run.control_epoch
                    and pending.scope == run.scope
                    and run.state.value == "ACTIVE"
                    and run.activity.value == "OPEN"
                )
    return WorkSessionDraft(
        scope=pending.scope,
        utterance_id=pending.utterance_id,
        version=pending.version,
        revision=pending.revision,
        text=text,
        withheld=text is None,
        acknowledgeable=text is not None and acknowledgeable,
    )


async def acknowledge(
    rt: Runtime, authority: ControlAuthority, request: AcknowledgeCommand
) -> Receipt:
    from .service import DOMAIN_OWNERS
    from .work_sessions import sessions

    scope = request.scope
    rt.supervisor.authorize_operator(authority, scope)
    utterance = await rt.store.repository(
        Utterance, Principal(authority.principal_id, "operator"), scope
    ).get(request.target, purpose="api_transfer")
    if utterance is None or utterance.scope != scope or utterance.operation_id is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    details = origin(rt, utterance)
    if (utterance.version, utterance.revision) != (
        request.expected_version,
        request.arguments.revision,
    ):
        raise ContractError(ErrorCode.VERSION_CONFLICT)
    session = await sessions(rt).get(scope, details.session_id)
    if (
        session is None
        or session.state in ("closed", "invalidated")
        or session.generation != details.generation
    ):
        raise ContractError(ErrorCode.STALE_CONTEXT)
    delivery_id = derived_id("work-delivery", scope, utterance.id)
    delivery = await rt.store.repository(Delivery, PRINCIPALS[Owner.CHANNEL], scope).get(
        delivery_id
    )
    if (
        delivery is None
        or delivery.revision != utterance.revision
        or delivery.channel_id != AUDIENCE
    ):
        raise ContractError(ErrorCode.ACCESS_DENIED)
    loaded = await rt.jobs.store.load(utterance.operation_id, scope)
    original = (
        None
        if loaded is None
        else await rt.store.repository(ExecutionCommand, PRINCIPALS[Owner.BEHAVIOR], scope).get(
            loaded[0].command_id
        )
    )
    if original is None:
        raise ContractError(ErrorCode.STALE_CONTEXT)
    if loaded is None or loaded[0].status != OperationState.SUCCEEDED:
        raise ContractError(ErrorCode.RESOURCE_BUSY)
    args = rt.registry.validate(
        original.arguments.schema_id, original.arguments.schema_version, original.arguments.data
    )
    if not isinstance(args, JobArguments) or args.request.work_session_id != session.id:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    admission = rt.supervisor.admit("activity")
    made = None
    linked = False

    def guard() -> None:
        rt.supervisor.authorize_operator(authority, scope)
        rt.supervisor.validate(admission)
        if original.freshness != admission.freshness:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    async def verify_source(area: Scope, ref: SourceRef, text: str) -> None:
        row = await memory(rt).get_source(area, ref.source_id)
        if (
            row is None
            or row.version != ref.version
            or row.content != text
            or row.source_type != SOURCE_TYPE
            or row.external_id != utterance.id
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    async def verify_delivery(area: Scope, identifier: str, ref: SourceRef) -> None:
        row = await rt.store.repository(Delivery, PRINCIPALS[Owner.CHANNEL], area).get(identifier)
        if (
            row is None
            or row.id != delivery_id
            or row.status != DeliveryState.DELIVERED
            or row.utterance_id != utterance.id
            or row.revision != utterance.revision
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    try:
        guard()
        pinned = original.model_copy(
            update={
                "freshness": admission.freshness,
                "cancellation_id": admission.id,
                "sources": utterance.sources,
            }
        )
        await rt.policy.check(pinned, None)
        if not await ticket_release(rt).bases_open(
            scope, utterance.sources, details.context_files, "api_transfer"
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)
        rows = await sessions(rt).messages(
            scope, session.id, after_sequence=details.after_sequence, limit=200
        )
        known = next(
            (
                row
                for row in rows
                if row.speaker == "character"
                and row.source.source_id
                == derived_id(
                    "src", scope, "external", SOURCE_TYPE, "workplace-session", utterance.id
                )
            ),
            None,
        )
        # A completed replay may have no pending pointer; any new/replaced draft must match exactly.
        pending = session.pending_reply
        if delivery.status != DeliveryState.DELIVERED or known is None:
            if pending is None or pending.utterance_id != utterance.id:
                raise ContractError(ErrorCode.STALE_CONTEXT)
        with ExitStack() as stack:
            for owner in DOMAIN_OWNERS:
                stack.enter_context(rt.store.fence(guard, owner=owner))
            stack.enter_context(
                rt.store.authorize_writes(
                    lambda connection: rt.policy.transaction_check(pinned, connection),
                    owners=DOMAIN_OWNERS,
                )
            )
            made = await memory(rt).register_source(
                command(scope, "work-received-" + utterance.id),
                SourceDraft(
                    provenance=Provenance.OBSERVATION,
                    source_type=SOURCE_TYPE,
                    adapter_id="workplace-session",
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
                source_id=made.source_id,
                version=made.version,
                scope=scope,
                availability="available",
            )
            linked = any(
                row.source.source_id == own.source_id and row.speaker == "character" for row in rows
            )
            if delivery.status != DeliveryState.DELIVERED:
                updated = delivery.model_copy(
                    update={
                        "version": delivery.version + 1,
                        "updated_at": rt.supervisor.clock.utc(),
                        "completed_at": rt.supervisor.clock.utc(),
                        "status": DeliveryState.DELIVERED,
                    }
                )
                await put(
                    rt.store,
                    rt.registry,
                    Owner.CHANNEL,
                    scope,
                    "work-delivered-" + utterance.id,
                    updated,
                    delivery.version,
                    rt.readers,
                )
            book = SessionBook(
                rt.store,
                PRINCIPALS[Owner.ACTIVITY],
                rt.supervisor.clock.utc,
                authorize=lambda area: guard(),
                verify_source=verify_source,
                verify_delivery=verify_delivery,
                readers=rt.readers,
            )
            await book.record_delivered(
                scope,
                "work-linked-" + utterance.id,
                session.id,
                generation=details.generation,
                through_sequence=details.through_sequence,
                source=own,
                text=utterance.content,
                delivery_id=delivery_id,
                context_sources=tuple(utterance.sources),
                context_files=tuple(details.context_files),
            )
            linked = True
            inputs = [
                row.source.source_id
                for row in rows
                if row.speaker == "operator"
                and details.after_sequence < row.sequence <= details.through_sequence
            ]
            # Replay uses the original input refs, not a changing session cursor.
            inputs = [ref.source_id for ref in utterance.sources if ref.source_id in inputs]
            episode_id, _ = await memory(rt).record_episode(
                command(scope, "work-episode-" + utterance.id),
                EpisodeDraft(
                    sources=(*utterance.sources, own),
                    participant_ids=(),
                    choice="Ответил в рабочем разговоре.",
                    actual_outcome="Панель подтвердила получение точной редакции.",
                    operation_id=utterance.operation_id,
                    provenance="observation",
                    experience_source_ids=tuple((*inputs, own.source_id)),
                ),
            )
            async with rt.store.transaction(PRINCIPALS[Owner.CHANNEL], scope, request) as unit:
                if unit.replay is not None:
                    return unit.replay
                receipt = Receipt(
                    request_id=request.request_id,
                    scope=scope,
                    status="completed",
                    operation_id=utterance.operation_id,
                )
                await unit.complete(receipt)
        rt.notify("work-session.changed", session.scope, session.id)
        rt.notify("delivery.changed", scope, delivery_id)
        rt.notify("memory.changed", scope, episode_id)
        return receipt
    finally:
        rt.supervisor.retire(admission)
        if made is not None and not linked:
            # Outside the revoked execution fences; only this generated orphan is retired.
            await memory(rt).expire_source(
                command(scope, "work-orphan-" + utterance.id), made.source_id, made.version
            )
