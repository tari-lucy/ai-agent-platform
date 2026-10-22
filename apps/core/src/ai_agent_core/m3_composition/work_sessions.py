"""Work conversation with the person (R3): operator intake and the panel transcript.

Commands go to the activity owner's ``SessionBook`` (R1); the operator's text is first stored
by the memory Source owner (rights, TTL, retention, ledger), then only its reference is
appended. Intake works while STOPPED and never calls a model, starts a runner or the agent:
the person's replies are appended by the session driver (R1) under a current admission. The
transcript gives the panel each message's text through the release gate, or ``withheld``.
"""

from typing import TYPE_CHECKING

from ai_agent_application.agent_activity.sessions import SessionBook
from ai_agent_application.control.ports import ControlAuthority
from ai_agent_application.memory import SourceDraft, derived_id
from ai_agent_application.storage import Owner
from ai_agent_application.tickets import area
from ai_agent_contracts.agent_sessions import WorkSessionMessageArguments
from ai_agent_contracts.api import CreateCommand, EditCommand, Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.panel import WorkSessionEntry, WorkSessionTranscript
from ai_agent_contracts.states import ErrorCode, Provenance

from .dialogue import memory
from .state import PRINCIPALS, command
from .workplace_experience import ticket_release

if TYPE_CHECKING:
    from .service import Runtime

SOURCE_TYPE = "work-session-message"
ADAPTER = "workplace-session"
MAX_TRANSCRIPT = 200
REFUSED = {
    ErrorCode.VERSION_CONFLICT,
    ErrorCode.ACCESS_DENIED,
    ErrorCode.RESOURCE_BUSY,
    ErrorCode.INVALID_ARGUMENTS,
}


def sessions(rt: Runtime, authority: ControlAuthority | None = None) -> SessionBook:
    def authorize(scope: Scope) -> None:
        if authority is None:
            raise ContractError(ErrorCode.ACCESS_DENIED)  # reads never write
        rt.authorize_configuration(authority, scope)

    async def verify_source(scope: Scope, ref: SourceRef, text: str) -> None:
        """The exact ref is the memory owner's current, available record of exactly this text."""
        source = await memory(rt).get_source(scope, ref.source_id)
        if (
            source is None
            or source.version != ref.version
            or source.availability != "available"
            or source.source_type != SOURCE_TYPE
            or source.content != text
        ):
            raise ContractError(ErrorCode.ACCESS_DENIED)

    return SessionBook(
        rt.store,
        PRINCIPALS[Owner.ACTIVITY],
        rt.supervisor.clock.utc,
        authorize=authorize,
        verify_source=verify_source,
        readers=rt.readers,
    )


def _enabled(rt: Runtime) -> None:
    # Sessions belong to the activity owner, whose rows a host has only with activity enabled.
    if rt.activity is None:
        raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)


async def mutate(
    rt: Runtime, authority: ControlAuthority, request: CreateCommand | EditCommand
) -> Receipt:
    _enabled(rt)
    book = sessions(rt, authority)
    if isinstance(request, CreateCommand) and request.type == "work.session.open":
        receipt = await book.open(request)
    elif isinstance(request, EditCommand) and request.type == "work.session.message":
        args = WorkSessionMessageArguments.model_validate(request.arguments.data)
        rt.authorize_configuration(authority, request.scope)
        scope = area(request.scope)
        # Stored first by its owner, keyed by the request: a retry finds the same source.
        made = await memory(rt).register_source(
            command(scope, "work-message-source-" + request.request_id),
            SourceDraft(
                provenance=Provenance.OPERATOR_INPUT,
                source_type=SOURCE_TYPE,
                adapter_id=ADAPTER,
                adapter_version="1",
                occurred_at=rt.supervisor.clock.utc(),
                content=args.text,
                read_allowed=True,
                api_transfer_allowed=True,  # the operator writes to the person (its model)
                public_disclosure_allowed=False,
                external_id=derived_id("work-message", scope, request.request_id),
                author_id=authority.principal_id,
            ),
        )
        ref = SourceRef(
            source_id=made.source_id, version=made.version, scope=scope, availability="available"
        )
        try:
            receipt = await book.append(request, ref)
        except ContractError as error:
            # Refused (stale version, closed session, full): the text this request stored is
            # retired by its owner, so it never stays as a loose memory without a message.
            # A retry of a request that did append replays above and raises nothing.
            if not made.duplicate and error.error.code in REFUSED:
                await memory(rt).expire_source(
                    command(scope, "work-message-refused-" + request.request_id),
                    made.source_id,
                    made.version,
                )
            raise
    elif isinstance(request, EditCommand) and request.type == "work.session.close":
        receipt = await book.close(request)
    else:
        raise ContractError(ErrorCode.INVALID_ARGUMENTS)
    assert receipt.resource_id is not None
    rt.notify("work-session.changed", area(request.scope), receipt.resource_id, receipt.version)
    return receipt


async def transcript(rt: Runtime, scope: Scope, session_id: str) -> WorkSessionTranscript:
    """The last messages of one conversation; each text through its source's release gate."""
    _enabled(rt)
    book = sessions(rt)
    stable = area(scope)
    session = await book.get(stable, session_id)
    if session is None:
        raise ContractError(ErrorCode.ACCESS_DENIED)
    start = max(0, session.last_sequence - MAX_TRANSCRIPT)
    entries: list[WorkSessionEntry] = []
    if session.state != "invalidated":
        release, mem = ticket_release(rt), memory(rt)
        rows = await book.messages(stable, session_id, after_sequence=start, limit=MAX_TRANSCRIPT)
        for row in rows:
            text = None
            if await release.bases_open(stable, (row.source,), (), "api_transfer"):
                source = await mem.get_source(stable, row.source.source_id, purpose="api_transfer")
                text = None if source is None else source.content
            entries.append(
                WorkSessionEntry(
                    sequence=row.sequence,
                    speaker=row.speaker,
                    text=text,
                    withheld=text is None,
                    at=row.created_at,
                    reply_to_sequence=row.reply_to_sequence,
                )
            )
    from .work_session_delivery import draft

    return WorkSessionTranscript(
        scope=scope,
        session_id=session.id,
        session_version=session.version,
        state=session.state,
        title=session.title,
        processed_sequence=session.processed_sequence,
        pending_reply=await draft(rt, session),
        entries=entries,
        complete=start == 0 and session.state != "invalidated",
        checked_at=rt.supervisor.clock.utc(),
    )
