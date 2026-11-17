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
        brain=None, look=None, imagine=None, hear=None, avatar=None, stage=None, chats=None,
        channel=None, people=None,
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


def test_the_shelf_holds_drawings_and_game_shots_and_serves_only_listed_ids(tmp_path: Path) -> None:
    import os

    harness = host(tmp_path, [])
    folder = harness.folders()
    (folder / "art").mkdir()
    (folder / "shots").mkdir()
    (folder / "art" / "a.jpg").write_bytes(b"\xff\xd8jpeg")
    (folder / "shots" / "tabl_z148.jpg").write_bytes(b"\xff\xd8shot")
    (folder / "shots" / "notes.txt").write_text("not a picture")
    (folder / "secret.jpg").write_bytes(b"x")
    os.utime(folder / "art" / "a.jpg", (1_700_000_000, 1_700_000_000))  # old: still on the shelf
    log = harness.listener.journal.parent / "imagine.jsonl"
    log.write_text(json.dumps({"at": "x", "status": "made", "file": "a.jpg", "scene": "обрыв"}) + "\n")
    shelf = build(harness, SCOPE, NOW).pictures
    assert [p.id for p in shelf] == ["shot--tabl_z148.jpg", "art--a.jpg"]  # newest first
    assert shelf[1].caption == "обрыв" and shelf[0].kind == "shot" and shelf[0].caption == "tabl z148"
    from ai_agent_core.m3_composition.harness_now import picture

    assert picture(harness, SCOPE, "art--a.jpg").media_type == "image/jpeg"  # type: ignore[union-attr]
    assert picture(harness, SCOPE, "shot--tabl_z148.jpg") is not None
    assert picture(harness, SCOPE, "art--secret.jpg") is None  # not on the shelf
    assert picture(harness, SCOPE, "shot--notes.txt") is None
    assert picture(harness, SCOPE, "a.jpg") is None and picture(harness, SCOPE, "art--../secret.jpg") is None


def test_the_week_counts_per_day(tmp_path: Path) -> None:
    harness = host(tmp_path, [])
    rows = [
        {"at": "2026-11-14T11:30:00+00:00", "status": "spoken", "text": "сегодня"},
        {"at": "2026-11-10T09:00:00+00:00", "status": "spoken", "text": "четыре дня назад"},
        {"at": "2026-11-01T09:00:00+00:00", "status": "spoken", "text": "давно"},
    ]
    harness.speaker.journal.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    days = build(harness, SCOPE, NOW).days
    assert len(days) == 7 and days[-1].hour.startswith("2026-11-14T00:00")
    assert [d.said for d in days] == [0, 0, 1, 0, 0, 0, 1]  # 8..14 November


def test_abilities_show_what_is_wired_and_what_is_not(tmp_path: Path) -> None:
    harness = host(tmp_path, [])
    harness.imagine = object()
    harness.avatar = SimpleNamespace(state="on")
    scripts = harness.folders() / "skills" / "minecraft"
    scripts.mkdir(parents=True)
    (scripts / "orient.py").write_text("x")
    rows = {a.title: a for a in build(harness, SCOPE, NOW).abilities}
    assert rows["Рисовать"].state == "on" and "imagine" in rows["Рисовать"].tools
    assert rows["Аватар"].state == "on" and rows["Видеть картинки"].state == "off"
    assert rows["Слушать"].state == "idle"  # wired, but not listening at this moment
    assert rows["Играть в Minecraft"].state == "off"  # no game source in this host
    assert rows["Память и запросы"].state == "on"
