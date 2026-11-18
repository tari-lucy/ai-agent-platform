"""Timelapse of a build: frames while it runs, the first and the last always, a video when ffmpeg is there."""

import base64
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SEEDS = Path(__file__).resolve().parents[2] / "apps/core/src/ai_agent_core/local_host/seeds"
sys.path.insert(0, str(SEEDS))
import build_plan  # noqa: E402
from test_design_check import FakeWorld, house_plan  # noqa: E402

JPEG = b"\xff\xd8\xff\xe0frame"


def frames_of(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.glob("frame_*.jpg"))


def test_the_first_and_the_last_frame_are_always_taken_and_the_hud_comes_back(tmp_path):
    seen, shots = [], iter(range(100))
    recorder = build_plan.Timelapse(
        tmp_path / "t",
        lambda: JPEG + str(next(shots)).encode(),
        every=60,  # no frame in between: only the two ends
        hide_hud=lambda: seen.append("hide"),
        show_hud=lambda: seen.append("show"),
        view=lambda number: seen.append(f"view {number}"),
    )
    recorder.view_number = 2
    recorder.start()
    assert seen == ["hide", "view 2"]
    assert recorder.stop() == 2
    assert seen == ["hide", "view 2", "view 0", "show"]  # the camera and the interface are given back
    assert frames_of(tmp_path / "t") == ["frame_00001.jpg", "frame_00002.jpg"]


def test_frames_come_while_it_runs_and_a_new_recording_replaces_an_old_one(tmp_path):
    folder = tmp_path / "t"
    folder.mkdir()
    (folder / "frame_00099.jpg").write_bytes(b"old")
    enough = threading.Event()
    count = []

    def shoot():
        count.append(1)
        if len(count) >= 5:
            enough.set()
        return JPEG

    recorder = build_plan.Timelapse(folder, shoot, every=0.01).start()
    assert enough.wait(10)
    frames = recorder.stop()
    assert frames >= 5 and frames_of(folder) == [f"frame_{i:05d}.jpg" for i in range(1, frames + 1)]  # the old one is gone


def test_a_missing_frame_is_no_failure_and_a_dead_screen_stops_the_capture(tmp_path):
    answers = iter([JPEG, None, JPEG] + [None] * 50)

    def shoot():
        value = next(answers)
        if value is None:
            raise OSError("no frame")
        return value

    recorder = build_plan.Timelapse(tmp_path / "t", shoot, every=0.01).start()
    recorder._stop.wait(0.5)
    recorder.stop()
    assert recorder.frames == 2 and recorder.misses >= 1  # 1, miss, 2, then misses until it gave up
    assert recorder._thread is not None and not recorder._thread.is_alive()


def test_the_capture_stops_at_the_frame_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(build_plan, "TL_MAX", 3)
    recorder = build_plan.Timelapse(tmp_path / "t", lambda: JPEG, every=0.01).start()
    recorder._thread.join(5)
    assert recorder.frames == 3 and not recorder._thread.is_alive()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="no ffmpeg here")
def test_the_frames_become_a_video(tmp_path):
    folder = tmp_path / "frames"
    folder.mkdir()
    for n, colour in enumerate(("red", "green", "blue"), 1):
        subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={colour}:s=65x47", "-frames:v", "1",
                        str(folder / f"frame_{n:05d}.jpg")], check=True)
    out = tmp_path / "t.mp4"
    path, note = build_plan.stitch(str(folder), str(out), fps=3)
    assert path == str(out) and out.stat().st_size > 0 and "видео" in note  # an odd size is made even for the encoder


def test_without_ffmpeg_it_says_what_to_run_elsewhere(tmp_path, monkeypatch):
    (tmp_path / "frame_00001.jpg").write_bytes(JPEG)
    monkeypatch.setattr(build_plan.shutil, "which", lambda name: None)
    path, note = build_plan.stitch(str(tmp_path), str(tmp_path / "x.mp4"))
    assert path is None and "ffmpeg здесь нет" in note and "frame_%05d.jpg" in note


def test_an_empty_folder_makes_no_video(tmp_path):
    assert build_plan.stitch(str(tmp_path), str(tmp_path / "x.mp4")) == (None, "кадров нет")


class Filming(FakeWorld):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hud, self.view = [], []

    def call(self, name, args):
        if name == "takeScreenshot":
            return json.dumps({"base64": base64.b64encode(JPEG).decode(), "width": 2, "height": 2}), False
        if name == "setHudHidden":
            self.hud.append(args["hidden"])
            return json.dumps({"ok": True}), False
        if name == "setCameraView":
            self.view.append(args["view"])
            return json.dumps({"ok": True}), False
        if name == "getHotbar":
            return json.dumps({"ok": True, "hotbar": [{"item": "minecraft:oak_planks", "count": 64}], "pack": {}}), False
        return super().call(name, args)


def plan_text(tmp_path: Path) -> str:
    path = tmp_path / "p.txt"
    path.write_text("легенда:\nP = oak_planks\n\nслой 0\nPPP\nPPP\n", encoding="utf-8")
    return str(path)


def test_build_with_the_timelapse_flag_leaves_frames_a_video_note_and_the_game_as_it_was(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_plan, "SHOTS", str(tmp_path / "shots"))
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    monkeypatch.setattr(build_plan.shutil, "which", lambda name: None)  # the video itself is another test
    world = Filming({}, scaffold=[])
    code = build_plan.main(["build", plan_text(tmp_path), "--origin", "0,0,0", "--timelapse", "--timelapse-view", "2"], client=world)
    out = capsys.readouterr().out
    assert code == 0, out
    assert len(frames_of(tmp_path / "shots" / "timelapse-p")) >= 2
    assert "Таймлапс: " in out and "ffmpeg здесь нет" in out
    assert world.hud == [True, False] and world.view == [2, 0]         # hidden for the filming, given back after


def test_the_hud_comes_back_even_when_the_build_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(build_plan, "SHOTS", str(tmp_path / "shots"))
    monkeypatch.setattr(build_plan.shutil, "which", lambda name: None)
    world = Filming({}, scaffold=[])
    monkeypatch.setattr(build_plan, "cmd_build", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        build_plan.main(["build", plan_text(tmp_path), "--origin", "0,0,0", "--timelapse"], client=world)
    assert world.hud == [True, False]


def test_without_the_flag_nothing_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr(build_plan, "SHOTS", str(tmp_path / "shots"))
    monkeypatch.setattr(build_plan.time, "sleep", lambda s: None)
    world = Filming({}, scaffold=[])
    assert build_plan.main(["build", plan_text(tmp_path), "--origin", "0,0,0"], client=world) == 0
    assert world.hud == [] and not (tmp_path / "shots").exists()


def test_the_stitch_action_needs_no_game_and_says_when_there_are_no_frames(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(build_plan, "SHOTS", str(tmp_path))
    assert build_plan.main(["timelapse", "nothing"]) == 1
    assert "Нет кадров" in capsys.readouterr().out
    assert house_plan()  # the shared fixtures import cleanly
