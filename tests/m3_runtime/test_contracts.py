"""Shared M3 runtime records reject incomplete recovery and transfer claims."""

from datetime import UTC, datetime

import pytest
from ai_agent_contracts.common import Contract, Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.persona import (
    GmRuntimeState,
    ReflectionCheckpoint,
    SettingsTransferOperation,
)
from pydantic import ValidationError

SCOPE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="synthetic-persona",
    memory_space_id="synthetic-work",
    memory_mode="work",
)
NOW = datetime(2026, 10, 14, tzinfo=UTC)


class Empty(Contract):
    pass


def value(model, **updates):
    registry = ExtensionRegistry()
    registry.register("runtime.fixture", Empty)
    body = dict(id="runtime", scope=SCOPE.model_dump(), created_at=NOW, updated_at=NOW, version=1)
    body.update(updates)
    return model.model_validate(body, context={"extensions": registry})


@pytest.mark.parametrize("seconds", [-1.0, float("nan"), float("inf")])
def test_active_time_checkpoint_cannot_advance_from_invalid_values(seconds):
    with pytest.raises(ValidationError):
        value(
            GmRuntimeState,
            intensity=0.5,
            active_seconds=seconds,
            rules_version=1,
            rules={"schema_id": "runtime.fixture", "data": {}},
        )


def test_reflection_checkpoint_requires_grounded_sources_and_strict_completion():
    args = dict(
        trigger_id="trigger",
        profile_version=1,
        state_version=1,
        response={"schema_id": "runtime.fixture", "data": {}},
        sources=[],
    )
    with pytest.raises(ValidationError):
        value(ReflectionCheckpoint, **args)
    args["sources"] = [
        dict(source_id="source", version=1, scope=SCOPE.model_dump(), availability="available")
    ]
    with pytest.raises(ValidationError):
        value(ReflectionCheckpoint, **args, completed="yes")
    assert not value(ReflectionCheckpoint, **args).completed


def test_a_partial_transfer_cannot_be_claimed_completed():
    test = SCOPE.model_copy(update={"memory_mode": "test", "memory_space_id": "synthetic-test"})
    args = dict(
        source_scope=test.model_dump(),
        preview_digest="a" * 64,
        selected_keys=["profile", "gm_intensity"],
        applied_keys=["profile"],
        operator_id="operator",
    )
    assert value(
        SettingsTransferOperation, **args, status="reconciliation_required"
    ).applied_keys == ["profile"]
    with pytest.raises(ValidationError):
        value(SettingsTransferOperation, **args, status="completed")
    with pytest.raises(ValidationError):
        value(
            SettingsTransferOperation,
            **(args | {"source_scope": SCOPE.model_dump()}),
            status="planned",
        )
