"""Closed extension schemas owned by the persona module (profile payloads and command arguments).

The shared contracts only say that a profile carries registered extension payloads. The shapes
below are this owner's registrations; trusted composition registers them once with
``register_persona_extensions`` before opening the store.
"""

from typing import Annotated, Literal, Self

from ai_agent_contracts.common import Contract, Id, ShortText, Text
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.panel import (
    PersonaApplyArguments as ApplyArguments,
)
from ai_agent_contracts.panel import (
    PersonaCopyArguments as CopyArguments,
)
from ai_agent_contracts.panel import (
    PersonaCreateArguments as CreateArguments,
)
from ai_agent_contracts.panel import (
    PersonaValidateArguments as ValidateArguments,
)
from pydantic import Field, model_validator

MAX_MARKDOWN_CHARS = 30_000
MAX_MARKDOWN_BYTES = 60_000
MAX_PROPERTIES = 100
MAX_VALUE_CHARS = 2000

type Layer = Literal["identity", "beliefs", "dispositions", "habits", "relationships", "state"]
# Relationship aspects are owned by social memory; the profile only defines their schema/defaults.
STATE_EXCLUDED_LAYERS: frozenset[str] = frozenset({"relationships"})

Number = Annotated[float, Field(allow_inf_nan=False)]
MarkdownText = Annotated[str, Field(strict=True, min_length=1, max_length=MAX_MARKDOWN_CHARS)]


class PropertyDefinition(Contract):
    name: Id
    layer: Layer
    title: Annotated[ShortText, Field(min_length=1)]
    description: Text = ""
    kind: Literal["scale", "text"]
    minimum: Number | None = None
    maximum: Number | None = None
    max_chars: Annotated[int, Field(strict=True, ge=1, le=MAX_VALUE_CHARS)] | None = None

    @model_validator(mode="after")
    def kind_fields(self) -> Self:
        if self.kind == "scale":
            if self.minimum is None or self.maximum is None or self.max_chars is not None:
                raise ValueError("SCALE_FIELDS")
            if not self.minimum < self.maximum:
                raise ValueError("SCALE_RANGE")
        elif self.max_chars is None or self.minimum is not None or self.maximum is not None:
            raise ValueError("TEXT_FIELDS")
        return self


class PropertyDefinitions(Contract):
    properties: list[PropertyDefinition] = Field(min_length=1, max_length=MAX_PROPERTIES)

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        names = [item.name for item in self.properties]
        if len(set(names)) != len(names):
            raise ValueError("DUPLICATE_PROPERTY")
        return self


class ValueMap(Contract):
    """Initial profile values and live character state share one shape."""

    values: dict[Id, Number | Annotated[str, Field(strict=True, max_length=MAX_VALUE_CHARS)]] = (
        Field(max_length=MAX_PROPERTIES)
    )


class DevelopmentBound(Contract):
    name: Id
    autonomous: bool
    max_step_per_episode: Annotated[Number, Field(gt=0)] | None = None
    max_step_per_window: Annotated[Number, Field(gt=0)] | None = None
    window_days: Annotated[int, Field(strict=True, ge=1, le=365)] = 7
    min_independent_episodes: Annotated[int, Field(strict=True, ge=0, le=100)] = 1


class DevelopmentBounds(Contract):
    bounds: list[DevelopmentBound] = Field(max_length=MAX_PROPERTIES)

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        names = [item.name for item in self.bounds]
        if len(set(names)) != len(names):
            raise ValueError("DUPLICATE_BOUND")
        return self


SCHEMAS: dict[str, type[Contract]] = {
    "persona.property-definitions": PropertyDefinitions,
    "persona.initial-values": ValueMap,
    "persona.development-bounds": DevelopmentBounds,
    "persona.state-values": ValueMap,
    "persona.create-args": CreateArguments,
    "persona.validate-args": ValidateArguments,
    "persona.apply-args": ApplyArguments,
    "persona.copy-args": CopyArguments,
}


def register_persona_extensions(registry: ExtensionRegistry) -> None:
    for schema_id, model in SCHEMAS.items():
        registry.register(schema_id, model)


__all__ = [
    "ApplyArguments",
    "CopyArguments",
    "CreateArguments",
    "ValidateArguments",
    "DevelopmentBounds",
    "PropertyDefinitions",
    "ValueMap",
    "register_persona_extensions",
]
