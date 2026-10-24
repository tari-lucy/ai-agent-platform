"""R2 ticket history release on real owners: SQLite, memory, deletion ledger, file guard.

Synthetic data only. A ticket's text is shown only while every basis is releasable: a revoked
transfer right, an expired or deleted source, a ledger block and a closed material each
withhold it; the operator's own words stay. Old bases are never dropped to make room.
"""

import asyncio

import pytest
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_application.tickets import TicketDraft, TicketRelease, TicketService
from ai_agent_application.tickets.release import WITHHELD
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from test_ticket_service import edit, registry, reply
from wf_support import ACT, AREA, CAT, MEM, World, run

TICKETS = Principal("core-tickets", "core_owner", Owner.TICKETS)


class MemorySources:
    """The memory owner's own view of its sources (as the composition will pass it)."""

    def __init__(self, world: World) -> None:
        self.world = world

    async def get_source(self, scope, source_id):
        return await self.world.memory.get_source(scope, source_id)


def test_multiple_bases_use_bounded_fresh_owner_reads_and_ledger(tmp_path):
    async def scenario(world):
        _, release = await owners(world)
        refs = [await world.source() for _ in range(9)]
        active, peak = 0, 0
        read_ids = []
        original = release.sources.get_source

        async def observed(scope, identifier):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.005)
                read_ids.append(identifier)
                return await original(scope, identifier)
            finally:
                active -= 1

        release.sources.get_source = observed
        assert await release.bases_open(AREA, refs, (), "api_transfer")
        assert 1 < peak <= 4
        assert sorted(read_ids) == sorted(ref.source_id for ref in refs)
        read_ids.clear()
        assert await release.bases_open(AREA, refs, (), "api_transfer")
        assert len(read_ids) == len(refs)  # success is not cached across invocations
        await world.ledger.block(AREA, refs[-1].source_id, 1, "rights_revoked", world.now)
        assert not await release.bases_open(AREA, refs, (), "api_transfer")

    run(tmp_path, scenario)


async def owners(world: World):
    world.store.extensions = registry()  # operator commands carry panel extension payloads
    await world.store.provision(
        AREA,
        (
            Grant(CAT, Owner.CATALOGUE, read=True, write=True, api_transfer=True),
            Grant(CAT, Owner.MEMORY, read=True, api_transfer=True),
            Grant(CAT, Owner.ACTIVITY, read=True),
            Grant(MEM, Owner.MEMORY, read=True, write=True),
            Grant(ACT, Owner.ACTIVITY, read=True, write=True),
            Grant(ACT, Owner.MEMORY, read=True),
            Grant(TICKETS, Owner.TICKETS, read=True, write=True),
            Grant(TICKETS, Owner.MEMORY, read=True),
        ),
    )
    world.memory.reader_access = (*world.memory.reader_access, RecordAccess(TICKETS.id))
    tickets = TicketService(world.store, world.clock, principal=TICKETS)
    release = TicketRelease(MemorySources(world), ledger=world.ledger, files=world.guard)
    return tickets, release


def draft(sources=(), materials=(), basis="lighthouse"):
    return TicketDraft(
        kind="question",
        title="Маяк: какой формат конспекта?",
        summary="Читала заметку о маяке.",
        attempted="Сравнила две версии.",
        decision_needed="Выбрать формат.",
        basis=basis,
        sources=tuple(sources),
        materials=tuple(materials),
    )


async def opened(world, tickets, request, d):
    made = await tickets.raise_ticket(request, AREA, "agent", "agent", d, "Вопрос по маяку.")
    return await tickets.get(AREA, made.resource_id)


def test_any_closed_basis_withholds_derived_text_but_not_the_operator(tmp_path):
    async def scenario(world: World):
        tickets, release = await owners(world)
        source = await world.source()
        ticket = await opened(world, tickets, "t-1", draft([source]))
        answer = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Списком.", "outcome": "answer"},
            "r-1",
            scope=AREA,
        )
        await tickets.reply("operator", answer, reply("Списком.", "answer", ticket.id))
        ticket = await tickets.get(AREA, ticket.id)
        rows = await tickets.messages(AREA, ticket.id)
        assert await release.ticket(AREA, ticket, "api_transfer") == ticket
        assert await release.messages(AREA, ticket, rows, "api_transfer") == rows

        # The transfer right is withdrawn: nothing derived goes to the panel or a model.
        await world.memory.set_rights(
            world.command(AREA, "revoke-api"),
            source.source_id,
            source.version,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        shown = await release.ticket(AREA, ticket, "api_transfer")
        assert shown.title == "Обращение скрыто" and shown.summary == WITHHELD
        assert (shown.id, shown.state, shown.next_turn, shown.sources) == (
            ticket.id,
            ticket.state,
            ticket.next_turn,
            ticket.sources,
        )
        bodies = [
            (m.author, m.body) for m in await release.messages(AREA, ticket, rows, "api_transfer")
        ]
        assert bodies == [("agent", WITHHELD), ("operator", "Списком.")]
        # The stored record itself is untouched: release only gates what leaves the owner.
        assert (await tickets.get(AREA, ticket.id)).summary == "Читала заметку о маяке."

    run(tmp_path, scenario)


def test_expiry_deletion_ledger_and_closed_material_each_withhold(tmp_path):
    async def scenario(world: World):
        tickets, release = await owners(world)
        expiring, deleted, blocked = [await world.source() for _ in range(3)]
        cases = {}
        for name, source in (("expired", expiring), ("deleted", deleted), ("ledger", blocked)):
            cases[name] = await opened(world, tickets, "t-" + name, draft([source], basis=name))
        await world.memory.expire_source(
            world.command(AREA, "expire"), expiring.source_id, expiring.version
        )
        await world.memory.delete_source(
            world.command(AREA, "delete"), deleted.source_id, deleted.version
        )
        # Ledger only (as after a restore of an older copy): the record still says available.
        await world.ledger.block(AREA, blocked.source_id, 1, "rights_revoked", world.now)
        for name, ticket in cases.items():
            assert not await release.open(AREA, ticket, "private"), name

        # A material: a note written from a source; blocking that source closes the file.
        basis = await world.source()
        change, data = world.change(b"Note about the lighthouse.")
        note = await world.files.write(await world.admit(sources=[basis]), change, data)
        with_file = await opened(
            world, tickets, "t-file", draft(materials=[note.reference], basis="file")
        )
        assert await release.open(AREA, with_file, "private")
        await world.ledger.block(AREA, basis.source_id, 1, "rights_revoked", world.now)
        assert not await release.open(AREA, with_file, "private")
        # Without a file guard a material cannot be checked, so it is not released.
        unchecked = TicketRelease(MemorySources(world), ledger=world.ledger)
        assert not await unchecked.open(AREA, with_file, "private")

    run(tmp_path, scenario)


def test_old_bases_are_kept_and_a_full_list_refuses_instead_of_forgetting(tmp_path):
    async def scenario(world: World):
        tickets, _ = await owners(world)
        first = [await world.source() for _ in range(50)]
        ticket = await opened(world, tickets, "t-1", draft(first))
        assert len(ticket.sources) == 50
        extra = await world.source()
        with pytest.raises(ContractError) as full:
            await opened(world, tickets, "t-2", draft([extra]))  # same basis: a repeat
        assert full.value.error.code is ErrorCode.RESOURCE_BUSY
        kept = await tickets.get(AREA, ticket.id)
        assert kept.sources == ticket.sources and kept.version == ticket.version
        # A repeat that adds nothing new still merges (no growth).
        again = await opened(world, tickets, "t-3", draft(first[:3]))
        assert again.sources == ticket.sources and again.occurrences == 2

    run(tmp_path, scenario)
