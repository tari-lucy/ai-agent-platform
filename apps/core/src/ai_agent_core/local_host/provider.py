"""Explicit offline mock provider and the fail-closed live gate.

The mock never reports usage: absent usage stays unknown instead of becoming zero.
It does not echo user text, endpoints or settings.
"""

import json
from collections.abc import AsyncIterator

from ai_agent_application.llm import (
    Delta,
    Failure,
    Finished,
    ProviderCall,
    ProviderError,
    ProviderFactory,
    Started,
)
from ai_agent_application.llm.model import ProviderEvent

from .layout import HostError

REPLY = "Вымышленный ответ технического mock-режима."
ANSWERS: dict[str, dict[str, object]] = {
    "character": {"reply": REPLY},
    "reflection": {"keep_state_reason": "Технический mock не меняет состояние.", "changes": []},
}


class MockProvider:
    """Deterministic per-role answers. Roles without a defined answer fail as unsupported."""

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        return self._stream(call)

    async def _stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]:
        answer = ANSWERS.get(call.assignment.role_id)
        if answer is None:
            raise ProviderError(Failure.UNSUPPORTED)
        text = json.dumps(answer, ensure_ascii=False)
        yield Started("mock")
        for index in range(0, len(text), 16):
            yield Delta(text[index : index + 16])
        yield Finished("stop")


def provider_factory(mode: str) -> ProviderFactory:
    if mode == "mock":
        provider = MockProvider()

        def build(adapter_id: str) -> MockProvider:
            if adapter_id != "mock":
                raise ProviderError(Failure.NOT_CONFIGURED)
            return provider

        return build
    # M3 dialogue and laboratory entries still reject adapter != mock (owner fence), and the
    # default allow-list is mock only. Live wiring is a composition-owner change.
    raise HostError("LIVE_PROVIDER_NOT_ENABLED")
