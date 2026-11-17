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


def test_activities_are_her_own_list_ordered_by_state(tmp_path: Path) -> None:
    harness = host(tmp_path, [])
    folder = harness.folders()
    (folder / "activities.md").write_text(
        "# Мои занятия\n\n## Закончила пост [готово]\nВышел в канал.\n\n"
        "## Строю дом в Minecraft [идёт]\nФундамент 9x7 готов,\nдальше стены.\n\n"
        "## Стрим [план]\n\n## Без состояния\nтекст\n",
        encoding="utf-8",
    )
    rows = build(harness, SCOPE, NOW).activities
    assert [row.title for row in rows] == ["Строю дом в Minecraft", "Стрим", "Без состояния", "Закончила пост"]
    assert rows[0].state == "doing" and "Фундамент" in rows[0].detail and rows[2].state == "planned"
    assert rows[-1].state == "done"


def test_hours_count_only_real_events_of_the_last_day(tmp_path: Path) -> None:
    harness = host(tmp_path, [])
    rows = [
        {"at": "2026-11-14T11:30:00+00:00", "status": "spoken", "text": "раз"},
        {"at": "2026-11-14T11:40:00+00:00", "status": "failed", "text": "не дошло"},
        {"at": "2026-11-12T11:40:00+00:00", "status": "spoken", "text": "давно"},
    ]
    harness.speaker.journal.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    base = harness.listener.journal.parent
    (base / "imagine.jsonl").write_text(json.dumps({"at": "2026-11-14T08:05:00+00:00", "status": "made"}) + "\n")
    hours = build(harness, SCOPE, NOW).hours
    assert len(hours) == 24 and hours[-1].hour.startswith("2026-11-14T12:00")
    assert sum(h.said for h in hours) == 1 and hours[-2].said == 1  # 11:30 is the hour before noon
    assert sum(h.drew for h in hours) == 1 and sum(h.heard for h in hours) == 1


def test_pictures_are_listed_from_the_log_and_served_by_listed_name_only(tmp_path: Path) -> None:
    harness = host(tmp_path, [])
    folder = harness.folders()
    (folder / "art").mkdir()
    (folder / "art" / "a.jpg").write_bytes(b"\xff\xd8jpeg")
    (folder / "secret.jpg").write_bytes(b"x")
    log = harness.listener.journal.parent / "imagine.jsonl"
    log.write_text(
        json.dumps({"at": "2026-11-14T10:00:00+00:00", "status": "made", "file": "a.jpg", "scene": "обрыв"}) + "\n"
        + json.dumps({"at": "2026-11-14T10:05:00+00:00", "status": "made", "file": "gone.jpg", "scene": "нет файла"}) + "\n"
        + json.dumps({"at": "2026-11-14T10:06:00+00:00", "status": "made", "file": "../secret.jpg"}) + "\n",
        encoding="utf-8",
    )
    listed = build(harness, SCOPE, NOW).pictures
    assert [p.name for p in listed] == ["a.jpg"] and listed[0].caption == "обрыв"
    from ai_agent_core.m3_composition.harness_now import picture

    assert picture(harness, SCOPE, "a.jpg").media_type == "image/jpeg"  # type: ignore[union-attr]
    assert picture(harness, SCOPE, "secret.jpg") is None  # exists, but she never drew it
    assert picture(harness, SCOPE, "../secret.jpg") is None
