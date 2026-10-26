"""W7b: the daily CPU budget of the agent's own wake-ups (local journal, no Docker)."""

from datetime import UTC, datetime

import pytest

from ai_agent_core.local_host.layout import WorkSessionsConfig
from ai_agent_core.m3_composition.work_session_cpu import (
    CpuSettings,
    exhausted,
    used_today,
    writer,
)

IMAGE = "sha256:" + "a" * 64


def test_journal_counts_this_persona_today(tmp_path):
    settings = CpuSettings(tmp_path / "usage", 100)
    writer(settings, "alpha", "a-1")(60.0)
    writer(settings, "beta", "b-1")(500.0)
    assert used_today(settings, "alpha") == 60.0 and not exhausted(settings, "alpha")
    writer(settings, "alpha", "a-2")(40.5)
    assert exhausted(settings, "alpha") and exhausted(settings, "beta")
    assert used_today(settings, "alpha", datetime(2000, 1, 1, tzinfo=UTC)) == 0.0
    day = next((tmp_path / "usage").iterdir())
    with day.open("a", encoding="utf-8") as stream:
        stream.write('broken\n[1]\n{"persona": "alpha", "cpu_seconds": -5}\n')
    assert used_today(settings, "alpha") == 100.5
    assert not exhausted(CpuSettings(tmp_path / "usage", None), "alpha")
    assert not exhausted(None, "alpha")


def test_budget_bounds():
    base = dict(image_id=IMAGE, areas=("projects",), output_area="projects")
    assert WorkSessionsConfig.model_validate(base).cpu_daily_minutes == 240
    with pytest.raises(ValueError):
        WorkSessionsConfig.model_validate({**base, "cpu_daily_minutes": 1441})
