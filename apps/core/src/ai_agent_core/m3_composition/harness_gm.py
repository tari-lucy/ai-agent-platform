"""The game master as a source of events: an offer is waiting, so tell her it is there.

The game master (doc08) stays internal and without a voice. It writes an Opportunity from her
confirmed experience; this source only notices that a live one exists and reports *that*, never
its content: the offer reaches her through ``persona_brief`` as before, she may decline it, and
the master's intent is not her knowledge. Low importance: it fills her free time, it does not
interrupt work or the game. With the master switched off this source reports nothing.
"""

import logging
from collections.abc import Callable
from typing import Any

from ai_agent_contracts.common import Scope

from .harness_events import Event, Level

LOG = logging.getLogger(__name__)

OFFER_TEXT = (
    "An opportunity for your own development is waiting. Call persona_brief to see it. It is "
    "optional: take it up, put it off or decline it, as you see fit."
)


class GmSource:
    name = "gm"

    def __init__(self, runtime: Any, interval: float = 120.0) -> None:
        self.rt, self.interval = runtime, interval
        self._told: set[str] = set()

    async def live_offers(self, scope: Scope) -> list[str]:
        """Ids of the offers she can see now (OFFERED, not expired)."""
        from ai_agent_contracts.persona import Opportunity
        from ai_agent_contracts.states import OpportunityState

        from .gm_cycle import _all

        now = self.rt.supervisor.clock.utc()
        return [
            o.id
            for o in await _all(self.rt, Opportunity, scope)
            if o.status is OpportunityState.OFFERED and o.expires_at > now
        ]

    async def poll(self, scope: Scope, current: Callable[[], None]) -> list[Event]:
        budget = getattr(self.rt, "private_api_budget", None)
        run = self.rt.supervisor.snapshot
        if (
            budget is None
            or not budget.gm_enabled
            or scope.memory_mode != "work"
            or run.gm_paused
            or run.autonomy != "autonomous"
        ):
            return []
        ids = await self.live_offers(scope)
        current()
        fresh = [i for i in ids if i not in self._told]
        self._told = set(ids)  # an offer that is gone or answered is forgotten
        if not fresh:
            return []
        return [Event("gm", "offer", "offer", OFFER_TEXT, Level.LOW)]

    async def delivered(self, scope: Scope, current: Callable[[], None]) -> None:
        return None
