"""R2 on the production composition: ticket release and session context over host owners.

Real build (ASGI), SQLite, memory owner, recovery ledger and file owner; synthetic data. A
ticket cites a real memory source; the session context shows it with that basis; revoking the
source invalidates a checkpoint and withholds the text, also from the operator's ticket read.
"""

import asyncio

from ai_agent_application.memory import SourceDraft
from ai_agent_application.tickets import TicketDraft
from ai_agent_application.tickets.release import WITHHELD
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.states import Provenance
from quality_support import Model, prepared_host

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command
from ai_agent_core.m3_composition.workplace_experience import session_context, ticket_release


def test_session_context_and_release_on_host_owners(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared_host(tmp_path, Model(), monkeypatch)
        await enable_recovery(
            host.root,
            store_dir=tmp_path / "backups",
            ledger_path=tmp_path / "ledger" / "events.sqlite",
        )
        await enable_tickets(host.root)
        await enable_workplace_files(host.root)
        async with host.open():
            rt, scope = host.runtime, host.scope
            made = await memory(rt).register_source(
                command(scope, "src-1"),
                SourceDraft(
                    provenance=Provenance.OPERATOR_INPUT,
                    source_type="operator-message",
                    adapter_id="dialogue",
                    adapter_version="1",
                    occurred_at=rt.supervisor.clock.utc(),
                    content="Встреча у маяка в пятницу.",
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                ),
            )
            ref = SourceRef(
                source_id=made.source_id,
                version=made.version,
                scope=scope,
                availability="available",
            )
            raised = await rt.tickets.raise_ticket(
                "t-1",
                scope,
                "agent",
                "agent",
                TicketDraft(
                    kind="question",
                    title="Пятница или суббота?",
                    summary="По словам оператора — пятница.",
                    attempted="Сверила заметки.",
                    decision_needed="Подтвердить день.",
                    basis="meeting-day",
                    sources=(ref,),
                ),
                "Встреча в пятницу?",
            )
            port = session_context(rt)
            built = await port.build(scope, "встреча у маяка", [raised.resource_id])
            assert not built.tickets[0].withheld and ref in built.refs.sources
            assert await port.still_valid(scope, built.refs)

            await memory(rt).set_rights(
                command(scope, "revoke"),
                ref.source_id,
                ref.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            assert not await port.still_valid(scope, built.refs)
            ticket = await rt.tickets.get(scope, raised.resource_id)
            shown = await ticket_release(rt).ticket(scope, ticket, "api_transfer")
            assert shown.summary == WITHHELD and shown.id == ticket.id
            assert (await port.build(scope, "встреча", [ticket.id])).tickets[0].withheld

    asyncio.run(scenario())
