from pathlib import Path

from ai_agent_core.local_host.harness import seed_activities
from ai_agent_core.m3_composition.harness_now import _activities


def test_the_activities_file_is_seeded_once_and_the_panel_can_read_it(tmp_path: Path) -> None:
    assert seed_activities(tmp_path, "opechatka") is True
    path = tmp_path / "opechatka" / "activities.md"
    rows = _activities(tmp_path / "opechatka")
    assert [row.state for row in rows] == ["planned"] and rows[0].title
    path.write_text("## Моё [идёт]\nсвоё\n", encoding="utf-8")
    assert seed_activities(tmp_path, "opechatka") is False  # hers now: never overwritten
    assert path.read_text(encoding="utf-8").startswith("## Моё")
    assert seed_activities(None, "opechatka") is False
