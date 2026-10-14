"""Scripted provider for tests and offline runs: no network, no credentials, no cost."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass, field

from ai_agent_application.llm import ProviderCall, ProviderError
from ai_agent_application.llm.model import ProviderEvent

ADAPTER_ID = "mock"


@dataclass(frozen=True)
class Pause:
    """Waits (cooperatively) between events, e.g. to exercise cancellation and timeouts."""

    seconds: float


type Step = ProviderEvent | ProviderError | Pause


@dataclass
class ScriptedProvider:
    """Plays one script per call, in order; an exhausted list is a test error."""

    scripts: list[list[Step]]
    calls: list[ProviderCall] = field(default_factory=list)
    closed: int = 0

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        self.calls.append(call)
        if not self.scripts:
            raise AssertionError("no scripted answer left")
        return self._play(self.scripts.pop(0))

    async def _play(self, steps: Iterable[Step]) -> AsyncIterator[ProviderEvent]:
        try:
            for step in steps:
                if isinstance(step, Pause):
                    await asyncio.sleep(step.seconds)
                elif isinstance(step, ProviderError):
                    raise step
                else:
                    yield step
        finally:
            self.closed += 1
