"""AW-06 composition: the person's brief for activity steps and results returned to memory.

Reads go through the existing persona/social/memory owners with api_transfer checks (the
brief is sent to the model); writes go only through the memory owner, called by the activity
composition after a step. No executor, tool or skill text reaches memory or character directly;
character change stays with bounded development, which reads these episodes like any other.
"""

from typing import TYPE_CHECKING

from ai_agent_application.agent_experience import ExperienceRecorder, PersonaContext
from ai_agent_application.memory.recall import RecallService
from ai_agent_application.social import SocialService
from ai_agent_application.storage import Owner, Principal

from .dialogue import Profiles, memory
from .state import PRINCIPALS

if TYPE_CHECKING:
    from .service import Runtime


def persona(rt: Runtime) -> PersonaContext:
    clock = rt.supervisor.clock.utc
    return PersonaContext(
        Profiles(rt),
        memory(rt),
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
