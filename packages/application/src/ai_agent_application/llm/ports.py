"""Ports of the LLM gateway. Adapters implement the provider; composition owns the rest."""

from collections.abc import AsyncIterator, Callable
from typing import Protocol

from ai_agent_contracts.extensions import ExtensionRegistry

from .model import AttemptReport, GenerationParameters, ProviderCall, ProviderEvent


class LLMProvider(Protocol):
    """One wire protocol. ``stream`` yields neutral events and raises ``ProviderError``.

    Closing the iterator (``aclose``) must release the connection. The provider never receives
    tools, so no event can carry an executable argument.
    """

    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderEvent]: ...


type ProviderFactory = Callable[[str], LLMProvider]
"""adapter_id -> provider; a missing adapter is a CAPABILITY failure, not a guess."""

type AttemptSink = Callable[[AttemptReport], None]
"""Receives every attempt exactly once, also after cancellation. M2-05 persists from here."""

type Admission = Callable[[], bool]
"""Trusted executor check (JobOwner.verify + scheduler.verify). False closes the output."""


def register_llm_extensions(registry: ExtensionRegistry) -> None:
    registry.register("llm.generation-parameters", GenerationParameters)
