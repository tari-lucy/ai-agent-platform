"""AW-06 composition: the person's brief for activity steps and results returned to memory.

Reads go through the existing persona/social/memory owners with api_transfer checks (the
brief is sent to the model); writes go only through the memory owner, called by the activity
composition after a step. No executor, tool or skill text reaches memory or character directly;
character change stays with bounded development, which reads these episodes like any other.
"""

from typing import TYPE_CHECKING

from ai_agent_application.agent_experience import ExperienceRecorder, PersonaContext, WorkContext
from ai_agent_application.memory.recall import RecallService
from ai_agent_application.social import SocialService
from ai_agent_application.storage import Owner, Principal
from ai_agent_application.tickets import TicketRelease
from ai_agent_contracts.common import Scope
from ai_agent_contracts.memory import Source

from .dialogue import Profiles, memory
from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime


class AttemptSources:
    """Source rights as the activity owner sees them: a source the attempt could not record as
    its input is never shown, so nothing reaches the model without lineage."""

    def __init__(self, rt: Runtime) -> None:
        self.rt = rt

    async def get_source(self, scope: Scope, source_id: str) -> Source | None:
        return await self.rt.store.repository(Source, PRINCIPALS[Owner.ACTIVITY], scope).get(
            source_id, purpose="api_transfer"
        )


def persona(rt: Runtime) -> PersonaContext:
    clock = rt.supervisor.clock.utc
    return PersonaContext(
        Profiles(rt),
        AttemptSources(rt),
        SocialService(
            rt.store,
            PRINCIPALS[Owner.SOCIAL],
            rt.registry,
            clock=clock,
            reader_access=rt.readers,
        ),
        None
        if rt.dialogue_index is None
        else RecallService(
            rt.store, Principal("operator", "operator"), rt.dialogue_index, clock=clock
        ),
    )


def recorder(rt: Runtime) -> ExperienceRecorder:
    return ExperienceRecorder(memory(rt), rt.supervisor.clock.utc)


def ticket_release(rt: Runtime) -> TicketRelease:
    """Release gate of ticket history: memory owner, host deletion ledger, file owner's guard.

    Without recovery there is no ledger (sources are still checked at their owner); without
    workplace files a ticket with materials is not released, since they cannot be checked.
    """
    ledger = getattr(rt.maintenance, "ledger", None)
    return TicketRelease(
        memory(rt),
        ledger=ledger,
        files=None if rt.workplace is None else rt.workplace.files.guard,
    )


def session_context(rt: Runtime) -> WorkContext:
    """The R2 context port for a working session (R1 wires it into its transport)."""
    return WorkContext(persona(rt), rt.tickets, ticket_release(rt))
