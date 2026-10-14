"""Closed schemas of the development module: the stored patch and the reflection answer."""

from typing import Annotated, Literal

from ai_agent_contracts.common import Contract, Id
from ai_agent_contracts.extensions import ExtensionRegistry
from pydantic import Field

Number = Annotated[float, Field(allow_inf_nan=False)]
Value = Number | Annotated[str, Field(strict=True, max_length=2000)]


class Patch(Contract):
    """One property change as stored in ``StateChange.patch``; rollback is another such patch."""

    property: Id
    before: Value
    after: Value
    kind: Literal["change", "rollback"] = "change"


class ChangeIdea(Contract):
    property: Id
    new_value: Value
    interpretation: Annotated[str, Field(strict=True, min_length=1, max_length=1500)]
    alternative: Annotated[str, Field(strict=True, min_length=1, max_length=1500)]
    uncertainty: Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
    expected_manifestation: Annotated[str, Field(strict=True, min_length=1, max_length=1000)]
    episode_ids: list[Id] = Field(min_length=1, max_length=10)


class ReflectionResult(Contract):
    """Keeping the state is a complete answer; a change needs lived episodes as evidence."""

    keep_state_reason: Annotated[str, Field(strict=True, max_length=1000)] = ""
    changes: list[ChangeIdea] = Field(default_factory=list, max_length=5)


def register_development_extensions(registry: ExtensionRegistry) -> None:
    registry.register("development.patch", Patch)
