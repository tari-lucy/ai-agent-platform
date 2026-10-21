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

from ai_agent_core.access.reads import SQLiteReader
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command
from ai_agent_core.m3_composition.ticket_reads import ReleasedReader
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

            # The operator's panel read. Storage itself already hides a record whose source
            # lost api_transfer; the ledger (e.g. after restoring an older copy) is what only
            # the release gate sees: the wrapped generic reader must close that too.
            second = await memory(rt).register_source(
                command(scope, "src-2"),
                SourceDraft(
                    provenance=Provenance.OPERATOR_INPUT,
                    source_type="operator-message",
                    adapter_id="dialogue",
                    adapter_version="1",
                    occurred_at=rt.supervisor.clock.utc(),
                    content="Маяк покрасили.",
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                ),
            )
            other = SourceRef(
                source_id=second.source_id,
                version=second.version,
                scope=scope,
                availability="available",
            )
            later = await rt.tickets.raise_ticket(
                "t-2",
                scope,
                "agent",
                "agent",
                TicketDraft(
                    kind="idea",
                    title="Зарисовать маяк",
                    summary="Маяк покрасили — можно зарисовать.",
                    attempted="Нашла заметку.",
                    decision_needed="Нужно ли?",
                    basis="paint",
                    sources=(other,),
                ),
                "Зарисовать?",
            )
            reader = ReleasedReader(SQLiteReader(rt.store), rt)

            async def shown():
                page = await reader.read("tickets", "operator", scope, None, 50, None)
                return {row.id: row.summary for row in page.items}

            assert (await shown())[later.resource_id] == "Маяк покрасили — можно зарисовать."
            await rt.maintenance.ledger.block(
                scope, other.source_id, other.version, "rights_revoked", rt.supervisor.clock.utc()
            )
            assert (await shown())[later.resource_id] == WITHHELD
            single = await reader.read("tickets", "operator", scope, later.resource_id, 50, None)
            assert single.summary == WITHHELD
            messages = await reader.read("ticket-messages", "operator", scope, None, 50, None)
            assert {m.body for m in messages.items if m.ticket_id == later.resource_id} == {
                WITHHELD
            }
            # Other resources pass through unchanged.
            plain = await SQLiteReader(rt.store).read(
                "permissions", "operator", scope, None, 50, None
            )
            assert await reader.read("permissions", "operator", scope, None, 50, None) == plain

    asyncio.run(scenario())
