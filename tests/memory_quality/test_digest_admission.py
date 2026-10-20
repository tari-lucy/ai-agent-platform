"""The new autonomous consolidate path needs a digest, WORK and explicit memory admission."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode, RunState
from pydantic import ValidationError

from ai_agent_core.m3_composition.schemas import TaskInput
from ai_agent_core.m3_composition.service import Runtime


@pytest.mark.parametrize(
    "digest,enabled,state,activity,autonomy,mode,foreign,allowed",
    [
        (True, True, RunState.ACTIVE, "OPEN", "autonomous", "work", False, True),
        (False, True, RunState.ACTIVE, "OPEN", "autonomous", "work", False, False),
        (True, False, RunState.ACTIVE, "OPEN", "autonomous", "work", False, False),
        (True, True, RunState.STOPPED, "OPEN", "autonomous", "work", False, False),
        (True, True, RunState.ACTIVE, "PAUSED", "autonomous", "work", False, False),
        (True, True, RunState.ACTIVE, "OPEN", "observation", "work", False, False),
        (True, True, RunState.ACTIVE, "OPEN", "autonomous", "test", False, False),
        (True, True, RunState.ACTIVE, "OPEN", "autonomous", "work", True, False),
    ],
)
def test_autonomous_digest_admission(
    digest, enabled, state, activity, autonomy, mode, foreign, allowed
):
    scope = Scope(
        workspace_id="workspace", persona_id="persona", memory_space_id="memory", memory_mode=mode
    )
    run = SimpleNamespace(
        scope=scope,
        state=state,
        activity=SimpleNamespace(value=activity),
        autonomy=autonomy,
        gm_paused=True,
    )
    runtime = SimpleNamespace(
        supervisor=SimpleNamespace(snapshot=run),
        private_api_budget=SimpleNamespace(memory_processing=enabled, gm_enabled=False),
    )
    now = datetime(2026, 10, 20, tzinfo=UTC)
    request = TaskInput(
        kind="consolidate",
        trigger_id="digest-test",
        source_ids=["source"],
        period_start=now,
        period_end=now,
        digest_of=["summary"] if digest else [],
    )
    requested = scope.model_copy(update={"persona_id": "other"}) if foreign else scope
    if allowed:
        Runtime.authorize_submission(runtime, None, requested, request)
    else:
        with pytest.raises(ContractError) as caught:
            Runtime.authorize_submission(runtime, None, requested, request)
        assert caught.value.error.code is ErrorCode.ACCESS_DENIED


def test_only_a_digest_can_pin_more_than_twenty_sources():
    now = datetime(2026, 10, 20, tzinfo=UTC)
    payload = dict(
        kind="consolidate",
        trigger_id="digest-large",
        source_ids=[f"source-{i}" for i in range(21)],
        period_start=now,
        period_end=now,
    )
    with pytest.raises(ValidationError):
        TaskInput.model_validate(payload)
    digest = TaskInput.model_validate({**payload, "digest_of": ["summary"]})
    assert len(digest.source_ids) == 21
