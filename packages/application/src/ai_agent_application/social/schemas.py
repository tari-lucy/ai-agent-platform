"""Closed extension schema of the social owner (relationship aspects)."""

from typing import Annotated, Literal

from ai_agent_contracts.common import Contract, Id
from ai_agent_contracts.extensions import ExtensionRegistry
from pydantic import Field

MAX_ASPECTS = 100
ASPECTS_SCHEMA = "social.relationship-aspects"

type ContactPreference = Literal["normal", "no_initiative", "no_contact"]
Number = Annotated[float, Field(allow_inf_nan=False)]


class RelationshipAspects(Contract):
    """Aspect names/bounds come from the persona profile, never from a fixed emotion list."""

    values: dict[Id, Number | Annotated[str, Field(strict=True, max_length=2000)]] = Field(
        max_length=MAX_ASPECTS
    )
    # The person's own stated wish about being approached; stored as such, not as a feeling.
    contact: ContactPreference = "normal"


def register_social_extensions(registry: ExtensionRegistry) -> None:
    registry.register(ASPECTS_SCHEMA, RelationshipAspects)
