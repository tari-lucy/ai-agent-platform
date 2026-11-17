"""The panel's "what is she doing now" picture is built from journals, never invented."""

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from ai_agent_contracts.common import Scope
from ai_agent_core.m3_composition.harness_activity import Activity
from ai_agent_core.m3_composition.harness_events import Event
from ai_agent_core.m3_composition.harness_now import build
from ai_agent_core.m3_composition.harness_watch import Watch

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="p-work", memory_mode="work")
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


def host(tmp_path: Path, parts: list[object]) -> SimpleNamespace:
    speech = tmp_path / "speech.jsonl"
    rows = [
        {"at": "2026-11-14T11:00:00+00:00", "status": "failed", "text": "не дошло"},
        {"at": "2026-11-14T11:05:00+00:00", "status": "spoken", "text": "Привет, я Опечатка"},
    ]
    speech.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    heard = tmp_path / "listening.jsonl"
    heard.write_text(json.dumps({"at": "2026-11-14T11:06:00+00:00", "text": "как дела"}) + "\n")
    folder = tmp_path / "files"
    folder.mkdir()
    (folder / "GOALS.md").write_text("# Цели\nОсмотреть спавн\n", encoding="utf-8")
    return SimpleNamespace(
        parts=parts,
        speaker=SimpleNamespace(journal=speech),
        listener=SimpleNamespace(journal=heard),
        folders=lambda: folder,
    )


def test_no_harness_is_an_honest_empty_picture() -> None:
    picture = build(None, SCOPE, NOW)
    assert picture.enabled is False and picture.lines == [] and picture.goals is None


def test_the_picture_joins_the_journals_newest_first(tmp_path: Path) -> None:
    activity = Activity("http://127.0.0.1:1", "x")
    activity.running["c1"] = ("идёт к табличке", 0.0)
    activity.recent.append("осмотрелась")
    watch = Watch(SimpleNamespace(), [], None)
    watch.queue.events[("game", "chat", "k")] = Event(
        "game", "chat", "k", "игрок написал привет", created=NOW.timestamp() - 30
    )
    picture = build(host(tmp_path, [activity, watch]), SCOPE, NOW)
    assert picture.doing == "идёт к табличке" and picture.recently == ["осмотрелась"]
    kinds = [line.kind for line in picture.lines]
    assert kinds[0] == "event" and "said" in kinds and "heard" in kinds
    assert all("не дошло" not in line.text for line in picture.lines)  # failed speech is not "said"
    assert picture.goals is not None and picture.goals.startswith("# Цели")
    assert [line.at for line in picture.lines] == sorted((line.at for line in picture.lines), reverse=True)


def test_a_missing_journal_gives_fewer_lines_not_an_error(tmp_path: Path) -> None:
    harness = host(tmp_path, [])
    harness.speaker.journal = tmp_path / "absent.jsonl"
    harness.folders = lambda: None
    picture = build(harness, SCOPE, NOW)
    assert [line.kind for line in picture.lines] == ["heard"] and picture.goals is None
