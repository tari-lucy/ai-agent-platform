"""Data-configured limits and closed audit patch for relationship development."""

import math
from dataclasses import dataclass
from typing import Annotated

from ai_agent_contracts.common import Contract, Id
from pydantic import Field

Number = Annotated[float, Field(allow_inf_nan=False)]


@dataclass(frozen=True)
class AspectLimit:
    name: str
    minimum: float
    maximum: float
    max_increase: float
    max_decrease: float

    def __post_init__(self) -> None:
        if (
            not self.name
            or not all(
                math.isfinite(v)
                for v in (self.minimum, self.maximum, self.max_increase, self.max_decrease)
            )
            or self.minimum > self.maximum
            or min(self.max_increase, self.max_decrease) < 0
        ):
            raise ValueError("RELATIONSHIP_LIMIT_INVALID")


class RelationshipDelta(Contract):
    person_id: Id
    before: dict[Id, Number] = Field(min_length=1, max_length=100)
    after: dict[Id, Number] = Field(min_length=1, max_length=100)
    episode_groups: list[Id] = Field(min_length=1, max_length=200)
