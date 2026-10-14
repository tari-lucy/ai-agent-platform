"""Canonical bounded GM metadata is auditable without encoding fields in IDs."""

import pytest
from ai_agent_contracts.persona import StoryRoll
from pydantic import ValidationError


def test_story_roll_retains_seed_weights_and_result():
    roll = StoryRoll(
        seed="synthetic-seed",
        context="family|scene",
        options=["find", "talk"],
        weights=[1.0, 0.0],
        chosen="find",
        value=0.3,
    )
    assert roll.seed == "synthetic-seed" and roll.weights == [1.0, 0.0]


@pytest.mark.parametrize(
    "changes",
    [
        {"weights": [-1.0, 1.0]},
        {"weights": [0.0, 0.0]},
        {"weights": [1.0]},
        {"chosen": "not-listed"},
        {"value": float("nan")},
    ],
)
def test_invalid_roll_metadata_is_rejected(changes):
    value = dict(
        seed="seed", context="choice", options=["a", "b"], weights=[1.0, 1.0], chosen="a", value=0.3
    )
    with pytest.raises(ValidationError):
        StoryRoll(**(value | changes))
