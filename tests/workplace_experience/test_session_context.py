"""R2 session context port on real owners: memory, tickets, deletion ledger, file guard.

Profile and recall are port doubles (the real ones are exercised in the AW-06 host test).
Checked: the context carries the exact bases of what it shows; a checkpoint built from it is
invalidated when any basis closes, and a rebuild withholds the closed ticket text.
"""

from types import SimpleNamespace as NS

from ai_agent_application.agent_experience import ContextRefs, PersonaContext, WorkContext
from ai_agent_application.tickets.release import WITHHELD
from test_ticket_release import MemorySources, draft, opened, owners
from test_ticket_service import edit, reply
from wf_support import AREA, World, run


class Person:
    """Profile/state/recall doubles with the published shapes; recall returns one real source."""

    def __init__(self, memory_ref) -> None:
        self.memory_ref = memory_ref

    async def get_profile(self, scope, version=None):
        return NS(canon="Любит маяки.", style="Коротко.")

    async def get_state(self, scope):
        return None

    async def recall(self, request):
        return NS(fragments=[NS(source=self.memory_ref, content="Маяк на мысе.")])


async def context(world: World):
    tickets, release = await owners(world)
    remembered = await world.source()
    person = Person(remembered)
    persona = PersonaContext(person, MemorySources(world), None, person)
    return tickets, WorkContext(persona, tickets, release), remembered


def test_context_carries_its_bases_and_a_closed_basis_invalidates_the_checkpoint(tmp_path):
    async def scenario(world: World):
        tickets, port, remembered = await context(world)
        basis = await world.source()
        ticket = await opened(world, tickets, "t-1", draft([basis]))
        answer = edit(
            "ticket.reply",
            ticket,
            {"ticket_id": ticket.id, "body": "Списком тезисов.", "outcome": "answer"},
            "r-1",
            scope=AREA,
        )
        await tickets.reply("operator", answer, reply("Списком тезисов.", "answer", ticket.id))

        built = await port.build(AREA, "маяк", [ticket.id, "ticket-missing"])
        assert "Любит маяки" in built.text() and "Маяк на мысе" in built.text()
        [view] = built.tickets
        assert not view.withheld and view.title == ticket.title
        assert [a for a, _ in view.messages] == ["agent", "operator"]
        assert {r.source_id for r in built.refs.sources} == {
            remembered.source_id,
            basis.source_id,
        }
        assert built.omitted == 1  # the unknown ticket
        assert await port.still_valid(AREA, built.refs)

        # The ticket's basis loses its transfer right: the checkpoint is no longer valid ...
        await world.memory.set_rights(
            world.command(AREA, "revoke"),
            basis.source_id,
            basis.version,
            read_allowed=True,
            api_transfer_allowed=False,
            public_disclosure_allowed=False,
        )
        assert not await port.still_valid(AREA, built.refs)
        # ... and a rebuilt context withholds that ticket's text and does not carry its basis.
        rebuilt = await port.build(AREA, "маяк", [ticket.id])
        [view] = rebuilt.tickets
        assert view.withheld and view.messages == (
            ("agent", WITHHELD),
            ("operator", "Списком тезисов."),
        )
        assert {r.source_id for r in rebuilt.refs.sources} == {remembered.source_id}
        assert await port.still_valid(AREA, rebuilt.refs)

        # A memory shown in the brief is blocked in the ledger (as after a restore).
        await world.ledger.block(AREA, remembered.source_id, 1, "rights_revoked", world.now)
        assert not await port.still_valid(AREA, rebuilt.refs)
        assert await port.still_valid(AREA, ContextRefs())

    run(tmp_path, scenario)


def test_material_bases_are_checked_by_the_file_owner(tmp_path):
    async def scenario(world: World):
        tickets, port, _ = await context(world)
        basis = await world.source()
        change, data = world.change(b"Note.")
        note = await world.files.write(await world.admit(sources=[basis]), change, data)
        ticket = await opened(world, tickets, "t-f", draft(materials=[note.reference], basis="f"))
        built = await port.build(AREA, "маяк", [ticket.id])
        assert built.refs.files == (note.reference,)
        assert await port.still_valid(AREA, built.refs)
        await world.ledger.block(AREA, basis.source_id, 1, "rights_revoked", world.now)
        assert not await port.still_valid(AREA, built.refs)
        assert (await port.build(AREA, "маяк", [ticket.id])).tickets[0].withheld

    run(tmp_path, scenario)
