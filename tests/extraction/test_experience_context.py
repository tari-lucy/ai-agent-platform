"""Context is still a dependency; it is not a new observation or an independent retelling."""

from datetime import UTC, datetime

import pytest
from ai_agent_application.extraction.evidence import experience_groups
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import Episode
from pydantic import ValidationError


def episode(identifier, primary=None):
    scope = Scope(
        workspace_id="synthetic", persona_id="alpha", memory_space_id="work", memory_mode="work"
    )
    return Episode(
        id=identifier,
        scope=scope,
        created_at=datetime.now(UTC),
        sources=[
            SourceRef(source_id=s, version=1, scope=scope, availability="available")
            for s in ("shared-context", "input-" + identifier, "reply-" + identifier)
        ],
        experience_source_ids=[] if primary is None else primary,
        participant_ids=[],
        choice="Ответил.",
        actual_outcome="Доставка подтверждена.",
        operation_id="op-" + identifier,
        provenance="observation",
        outcome_confirmed=True,
    )


def test_primary_evidence_separates_context_but_shared_events_and_legacy_stay_one_group():
    first, second = (
        episode("one", ["input-one", "reply-one"]),
        episode("two", ["input-two", "reply-two"]),
    )
    groups = experience_groups([first, second])
    assert groups["input-one"] != groups["input-two"]
    legacy = experience_groups([episode("one"), episode("two")])
    assert legacy["input-one"] == legacy["input-two"]
    same = experience_groups(
        [episode("one", ["shared-context"]), episode("two", ["shared-context"])]
    )
    assert len(set(same.values())) == 1
    for selected in (["foreign"], ["input-one", "input-one"]):
        with pytest.raises(ValidationError):
            episode("one", selected)
