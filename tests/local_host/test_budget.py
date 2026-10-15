"""Explicit cap updates and UTC boundaries; no model, credentials or money are involved."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE

from ai_agent_core.local_host.budget import configure_budget
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import HostError, LiveConfig, load_config, paths
from ai_agent_core.local_host.process import exclusive_host
from ai_agent_core.m3_composition.service import PrivateAPIBudget


def test_calendar_boundary_counts_unknown_and_future_attempts():
    now = datetime(2026, 10, 15, 23, 59, tzinfo=UTC)
    rows = [
        SimpleNamespace(started_at=now - timedelta(days=1), status="completed"),
        SimpleNamespace(started_at=now, status="failed"),
        SimpleNamespace(started_at=now, status="unknown"),
        SimpleNamespace(started_at=now + timedelta(days=1), status="running"),
    ]
    daily = PrivateAPIBudget("conn", 100, 24000, "utc_day")
    assert daily.used(rows, now) == 3
    assert daily.used(rows, now.replace(hour=0, minute=0)) == 3
    tomorrow = now.replace(hour=0, minute=0) + timedelta(days=1)
    assert daily.reset_at(now) == tomorrow
    assert daily.used(rows, tomorrow) == 1
    lifetime = PrivateAPIBudget("conn", 100, 24000)
    assert lifetime.used(rows, tomorrow) == 4 and lifetime.reset_at(tomorrow) is None


def test_offline_budget_update_preserves_state_and_refuses_active_root(tmp_path):
    root = tmp_path / "host"
    asyncio.run(
        initialise(
            root, profile_markdown=REFERENCE_PROFILE, live=LiveConfig(model_id="fixture-model")
        )
    )
    host = paths(root)
    before = load_config(host)
    database = host.database.read_bytes()
    with exclusive_host(root):
        with pytest.raises(HostError, match="HOST_ALREADY_RUNNING"):
            configure_budget(root, limit=100, period="utc_day")
    assert load_config(host) == before
    configure_budget(root, limit=100, period="utc_day")
    updated = load_config(host)
    assert updated.live.attempt_limit == 100 and updated.live.attempt_period == "utc_day"
    assert updated.model_copy(update={"live": before.live}) == before
    assert host.database.read_bytes() == database
    for limit in (0, 101):
        with pytest.raises(HostError, match="INVALID_OPTION"):
            configure_budget(root, limit=limit, period="utc_day")
    assert load_config(host) == updated
