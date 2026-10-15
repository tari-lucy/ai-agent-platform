"""Calendar accounting reserves background capacity without adding to the total."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ai_agent_core.m3_composition.service import PrivateAPIBudget


def test_all_attempt_outcomes_count_and_memory_reserve_is_inside_total():
    now = datetime(2026, 10, 15, 12, tzinfo=UTC)
    rows = [
        SimpleNamespace(started_at=now, status=s, role_id="character")
        for s in ("completed", "failed", "unknown")
    ]
    rows.append(
        SimpleNamespace(
            started_at=now - timedelta(days=1), status="completed", role_id="reflection"
        )
    )
    budget = PrivateAPIBudget("conn", 300, 24000, "utc_day", True, 8)
    assert budget.used(rows, now) == 3 and budget.dialogue_remaining(rows, now) == 289
    rows.append(SimpleNamespace(started_at=now, status="failed", role_id="reflection"))
    assert budget.used(rows, now) == 4 and budget.memory_used(rows, now) == 1
    assert budget.dialogue_remaining(rows, now) == 289
    assert not budget.allows("game-master") and budget.allows("reflection")
    # Clock rollback does not grant a second allowance for a future-dated attempt.
    assert budget.memory_used(rows, now - timedelta(hours=1)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        dict(period="lifetime"),
        dict(max_attempts=8),
        dict(memory_daily_calls=True),
        dict(memory_daily_calls=2),
    ],
)
def test_invalid_memory_configuration_is_closed(changes):
    values = dict(
        connection_id="conn",
        max_attempts=300,
        max_prompt_chars=24000,
        period="utc_day",
        memory_processing=True,
        memory_daily_calls=8,
    )
    with pytest.raises(ValueError, match="PRIVATE_MEMORY_BUDGET"):
        PrivateAPIBudget(**{**values, **changes})
