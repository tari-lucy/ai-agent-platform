"""Drawing on purpose: a considered brief, bounds, money, STOP, the journal and the files."""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.m3_composition.harness_channel import ChannelPost, ChannelSettings
from ai_agent_core.m3_composition.harness_imagine import (
    FIELD_MAX,
    ImageFailed,
    ImageRefused,
    Imagine,
)
from ai_agent_core.m3_composition.harness_look import Look
from ai_agent_core.m3_composition.harness_mcp import PlatformMcp

JPEG = b"\xff\xd8\xff\xe0synthetic-picture"
BRIEF = {
    "meaning": "показать, как тихо и уютно в редакции ночью",
    "scene": "чёрная кошка с блокнотом за столом, лампа, стопка газет",
    "style": "тёплая акварель, приглушённые охра и бордо",
}


class Usage:
    def __init__(self, left=10.0):
        self.left, self.rows = left, []

    def remaining(self):
        return self.left

    def record_amount(self, amount, *, source):
        self.rows.append((amount, source))


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now


class Wall:
    moment = datetime(2026, 11, 6, 12, 0, 0, tzinfo=UTC)

    def __call__(self):
        return self.moment


def make(tmp_path, *, generate=None, **kwargs):
    seen = []

    async def default(prompt, size):
        seen.append((prompt, size))
        return JPEG

    clock, wall, usage = Clock(), Wall(), Usage()
    imagine = Imagine(
        tmp_path / "art",
        tmp_path / "imagine.jsonl",
        generate or default,
        clock=clock,
        wall=wall,
        usage=usage,
        **kwargs,
    )
    return imagine, seen, clock, wall, usage


def go(coro):
    return asyncio.run(coro)


def journal(tmp_path):
    path = tmp_path / "imagine.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_the_prompt_carries_every_part_of_the_brief_and_forbids_stray_words(tmp_path):
    imagine, seen, *_ = make(tmp_path)
    go(
        imagine.draw(
            {
                **BRIEF,
                "mood": "спокойное",
                "composition": "крупный план, лампа сверху",
                "avoid": "людей",
                "size": "tall",
            },
            lambda: None,
        )
    )
    prompt, size = seen[0]
    for part in (
        BRIEF["scene"],
        "Стиль: " + BRIEF["style"],
        "Настроение: спокойное",
        "Композиция: крупный план",
        "Что картинка должна выразить: " + BRIEF["meaning"],
        "Не должно быть на картинке: людей",
        "Надписей и букв",
        "Формат кадра: вертикальный, 2:3.",
    ):
        assert part in prompt
    assert size == "1024x1536"


def test_a_picture_is_saved_paid_for_and_its_brief_is_kept(tmp_path):
    imagine, seen, _, _, usage = make(tmp_path, price=0.05)
    answer = go(imagine.draw({**BRIEF, "name": "Ночная редакция"}, lambda: None))
    files = sorted(os.listdir(tmp_path / "art"))
    assert len(files) == 1 and files[0].endswith(
        "-art.jpg"
    )  # a plain ASCII name, whatever she wrote
    assert (tmp_path / "art" / files[0]).read_bytes() == JPEG
    assert files[0] in answer and "look" in answer and "art/" + files[0] in answer
    assert usage.rows == [(0.05, "image")]
    row = journal(tmp_path)[0]
    assert row["status"] == "made" and row["file"] == files[0] and row["size"] == "1536x1024"
    assert row["meaning"] == BRIEF["meaning"] and row["style"] == BRIEF["style"]


def test_without_the_core_of_the_brief_nothing_is_drawn(tmp_path):
    imagine, seen, _, _, usage = make(tmp_path)
    for missing in ("meaning", "scene", "style"):
        brief = {k: v for k, v in BRIEF.items() if k != missing}
        answer = go(imagine.draw(brief, lambda: None))
        assert answer.startswith("Не нарисовано") and "meaning" in answer
    assert go(imagine.draw({**BRIEF, "scene": "   "}, lambda: None)).startswith("Не нарисовано")
    assert seen == [] and usage.rows == [] and not (tmp_path / "art").exists()


def test_a_part_that_is_too_long_is_refused_not_cut(tmp_path):
    imagine, seen, *_ = make(tmp_path)
    answer = go(imagine.draw({**BRIEF, "scene": "а" * (FIELD_MAX + 1)}, lambda: None))
    assert "не длиннее" in answer and seen == []


def test_daily_cap_and_gap_and_budget(tmp_path):
    imagine, seen, clock, wall, usage = make(tmp_path, daily=2, min_gap=20.0)
    assert go(imagine.draw(BRIEF, lambda: None)).startswith("Готово")
    assert "слишком часто" in go(imagine.draw(BRIEF, lambda: None))
    clock.now += 21
    wall.moment += timedelta(seconds=21)
    assert go(imagine.draw(BRIEF, lambda: None)).startswith("Готово")
    clock.now += 21
    assert "дневной лимит" in go(imagine.draw(BRIEF, lambda: None))
    wall.moment += timedelta(days=1)  # a new day: the cap starts again
    assert go(imagine.draw(BRIEF, lambda: None)).startswith("Готово")
    usage.left = 0
    clock.now += 21
    assert "бюджет исчерпан" in go(imagine.draw(BRIEF, lambda: None))
    assert len(seen) == 3 and len(usage.rows) == 3


def test_a_refused_or_failed_picture_is_a_note_and_costs_nothing(tmp_path):
    async def refuse(prompt, size):
        raise ImageRefused("модель отказалась рисовать такое")

    async def fail(prompt, size):
        raise ImageFailed("network")

    imagine, _, clock, _, usage = make(tmp_path, generate=refuse)
    answer = go(imagine.draw(BRIEF, lambda: None))
    assert "отказалась" in answer and "Перепиши замысел" in answer
    imagine.generate = fail
    clock.now += 21
    assert "не пришла" in go(imagine.draw(BRIEF, lambda: None))
    assert usage.rows == [] and not (tmp_path / "art").exists()
    assert [r["status"] for r in journal(tmp_path)] == ["refused", "failed"]


def test_stop_while_drawing_drops_the_picture(tmp_path):
    imagine, *_ = make(tmp_path)
    calls = []

    def current():
        calls.append(1)
        if len(calls) >= 2:  # the check after the model answered
            raise ContractError(ErrorCode.STALE_CONTEXT)

    with pytest.raises(ContractError):
        go(imagine.draw(BRIEF, current))
    assert not (tmp_path / "art").exists()
    assert not (tmp_path / "imagine.jsonl").exists() or "made" not in (
        tmp_path / "imagine.jsonl"
    ).read_text(encoding="utf-8")


def test_mcp_lists_imagine_only_when_drawing_is_on_and_a_stopped_run_is_refused(tmp_path):
    def runtime(state=RunState.ACTIVE):
        snapshot = SimpleNamespace(state=state, control_epoch=3, scope=None)
        return SimpleNamespace(supervisor=SimpleNamespace(snapshot=snapshot), tickets=None)

    def ask(mcp, method, params=None):
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
        return asyncio.run(mcp.handle(body))["result"]

    def names(mcp):
        return {t["name"] for t in ask(mcp, "tools/list")["tools"]}

    assert "imagine" not in names(PlatformMcp(runtime(), lambda: 3))  # type: ignore[arg-type]
    imagine, *_ = make(tmp_path)
    mcp = PlatformMcp(runtime(), lambda: 3, imagine=imagine)  # type: ignore[arg-type]
    assert "imagine" in names(mcp)
    tool = next(t for t in ask(mcp, "tools/list")["tools"] if t["name"] == "imagine")
    assert tool["inputSchema"]["required"] == ["meaning", "scene", "style"]
    call = {"name": "imagine", "arguments": BRIEF}
    assert ask(mcp, "tools/call", call)["content"][0]["text"].startswith("Готово")
    stopped = PlatformMcp(runtime(RunState.STOPPING), lambda: 3, imagine=imagine)  # type: ignore[arg-type]
    assert ask(stopped, "tools/call", call)["content"][0]["text"] == "Отказ: STALE_CONTEXT"


def test_the_drawing_can_be_looked_at_and_posted_to_the_channel(tmp_path):
    imagine, *_ = make(tmp_path)
    go(imagine.draw({**BRIEF, "name": "lamp"}, lambda: None))
    name = os.listdir(tmp_path / "art")[0]

    described = []

    async def describe(prompt, image):
        described.append(image)
        return "Кошка с блокнотом."

    shots = tmp_path / "shots"
    shots.mkdir()
    look = Look(shots, describe, others={"art": tmp_path / "art"})
    answer = go(look.look(name, "", lambda: None, "art"))
    assert "Кошка с блокнотом." in answer and described == [JPEG]

    post = ChannelPost(
        ChannelSettings(
            "@c", tmp_path / "r.txt", tmp_path / "j.jsonl", 3, shots, art=tmp_path / "art"
        ),
        SimpleNamespace(),  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
    )
    assert post._photo("art/" + name) == JPEG  # the drawing is found under art/
    assert post._photo(name) is None  # the bare name looks only in the shots
    assert post._photo("art/../shots/x.jpg") is None


def test_configure_and_the_builder(monkeypatch, tmp_path):
    from ai_agent_core.local_host import harness, layout

    class Config:
        work_sessions = object()

        def __init__(self, harness_config=None):
            self.harness = harness_config

        def model_copy(self, update):
            return Config(update["harness"])

    saved = []
    monkeypatch.setattr(layout, "paths", lambda root: root)
    monkeypatch.setattr(harness, "load_config", lambda host: Config())
    monkeypatch.setattr(harness, "save_config", lambda host, config: saved.append(config))
    monkeypatch.setattr(harness, "password", lambda host: "")
    result = harness.configure(
        tmp_path, image_model="cx/gpt-image-2.5", image_daily=5, image_price=0.07
    )
    assert (result.image_model, result.image_daily, result.image_price) == (
        "cx/gpt-image-2.5",
        5,
        0.07,
    )
    assert harness.configure(tmp_path, remove_image=True).image_model is None

    settings = layout.HarnessConfig(image_model="cx/gpt-image-2.5", files_dir=str(tmp_path))
    host = SimpleNamespace(root=tmp_path)
    providers = SimpleNamespace(binding=("https://router.example/v1", "k"))
    scope = SimpleNamespace(persona_id="p")
    built = harness.imaginer(settings, host, providers, Usage(), scope)  # type: ignore[arg-type]
    assert built is not None and built.folder == tmp_path / "p" / "art"
    off = layout.HarnessConfig(files_dir=str(tmp_path))
    assert harness.imaginer(off, host, providers, Usage(), scope) is None  # type: ignore[arg-type]
    assert harness.imaginer(settings, host, SimpleNamespace(binding=None), Usage(), scope) is None  # type: ignore[arg-type]


def test_the_art_memo_is_put_once_and_never_overwritten(tmp_path):
    from ai_agent_core.local_host.harness import ART_GUIDE, seed_art_guide

    assert seed_art_guide(tmp_path, "p") is True
    target = tmp_path / "p" / "playbook" / "art.md"
    assert target.read_text(encoding="utf-8") == ART_GUIDE and "Мой стиль" in ART_GUIDE
    target.write_text("мой стиль: акварель", encoding="utf-8")
    assert seed_art_guide(tmp_path, "p") is False
    assert target.read_text(encoding="utf-8") == "мой стиль: акварель"
    assert seed_art_guide(None, "p") is False


def jpeg(width, height):
    """A JPEG header with the given size (enough for the size reader)."""
    import struct

    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\xff\xc0\x00\x11\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )


def test_the_real_size_is_told_and_a_wrong_shape_is_pointed_out(tmp_path):
    async def portrait(prompt, size):
        return jpeg(1086, 1448)  # asked for a wide frame, the router gave a tall one

    imagine, *_ = make(tmp_path, generate=portrait)
    answer = go(imagine.draw({**BRIEF, "size": "wide"}, lambda: None))
    assert "1086×1448" in answer and "Формат вышел не такой" in answer
    assert journal(tmp_path)[0]["detail"] == "1086x1448"

    async def wide(prompt, size):
        return jpeg(1536, 1024)

    imagine2, *_ = make(tmp_path / "second", generate=wide)
    answer = go(imagine2.draw({**BRIEF, "size": "wide"}, lambda: None))
    assert "1536×1024" in answer and "Формат вышел не такой" not in answer
