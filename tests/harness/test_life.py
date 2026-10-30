"""The persona's self-directed life: slots, money envelope, windows, adaptive pacing."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ai_agent_contracts.common import Scope

from ai_agent_core.m3_composition.harness_channel import ChannelPost, ChannelSettings
from ai_agent_core.m3_composition.harness_host import HarnessSettings, Usage
from ai_agent_core.m3_composition.harness_life import (
    GOALS_TEMPLATE,
    Life,
    LifePart,
    seed_workspace,
)
from ai_agent_core.m3_composition.harness_wakes import HarnessWakes

SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")
MOSCOW = timezone(timedelta(hours=3))


def at(hour: int, minute: int = 0, days: int = 0) -> datetime:
    """A moment given in Moscow time, as the host clock would report it in UTC."""
    day = datetime(2026, 10, 31, hour, minute, tzinfo=MOSCOW) + timedelta(days=days)
    return day.astimezone(UTC)


def moscow(moment: datetime) -> datetime:
    return moment.astimezone(MOSCOW)


def usage(tmp_path: Path, budget: float = 1.0) -> Usage:
    return Usage(
        HarnessSettings(
            image_id="i",
            port=4096,
            password="p",
            upstream="https://x/v1",
            key="k",
            model="m",
            usage_dir=tmp_path / "usage",
            daily_budget=budget,
            input_rate=0.12,
            output_rate=0.12,
            currency="USD",
        )
    )


def life(tmp_path: Path, mode: str = "normal") -> Life:
    return Life(mode, tmp_path / "life.json", 1.0, to_local=moscow)


def spend(u: Usage, tokens: int, wake: str | None = None) -> None:
    row = {"kind": "usage", "status": 200, "input": tokens, "output": 0}
    u.record(row if wake is None else {**row, "wake": wake})


def test_a_due_slot_fires_once_stale_and_quiet_ones_do_not(tmp_path):
    u, lf = usage(tmp_path), life(tmp_path)
    assert lf.slot_due(at(3), u) is None  # quiet hours
    assert lf.slot_due(at(10, 59), u) is None  # before the first slot
    assert lf.slot_due(at(11, 5), u) == 11
    lf.mark_done(11, at(11, 5))
    assert lf.slot_due(at(11, 30), u) is None  # already handled today
    assert lf.slot_due(at(20, 0), u) is None  # the 16:00 slot was missed by 4 h: skipped...
    assert lf.slot_due(at(21, 10), u) == 21  # ...and the evening one still runs
    assert lf.slot_due(at(11, 5), u) is None  # the morning slot stays handled


def test_sleep_has_no_slots_and_no_share(tmp_path):
    u, lf = usage(tmp_path), life(tmp_path, "sleep")
    assert lf.slot_due(at(12), u) is None
    assert lf.autonomy_left(1.0, u) == 0


def test_window_cap_share_and_expiry(tmp_path):
    u, lf = usage(tmp_path), life(tmp_path)  # share 0.5, cap 0.10
    now = at(11, 5)
    assert lf.limit(1.0, u, now) == 1.0  # no window: only the day
    lf.open_window("w1", now)
    spend(u, 500_000, "w1")  # 0.06 of the 0.10 cap
    assert abs(lf.limit(1.0 - u.spent(), u, now) - 0.04) < 1e-9
    assert lf.limit(0.02, u, now) == 0.02  # the day's rest may be smaller
    later = now + timedelta(minutes=31)
    assert lf.limit(0.9, u, later) == 0.9  # the window expired: the dialogue is not capped
    # The share: she spent 0.45 on her own today, only 0.05 is left of 0.5 whatever the window.
    spend(u, 3_750_000, "w0")
    assert lf.autonomy_left(1.0 - u.spent(), u) < 0.1


def test_empty_windows_shrink_the_rhythm_until_she_finds_something_to_do(tmp_path):
    u, lf = usage(tmp_path), life(tmp_path)
    for index, hour in enumerate((11, 16)):
        now = at(hour, 5)
        lf.open_window(f"e{index}", now)
        spend(u, 1000, f"e{index}")  # a few cents' worth of nothing
        lf.close_expired(now + timedelta(minutes=40), u)
    assert lf._read()["idle"] == 2
    assert lf.slot_due(at(11, 5, days=1), u) is None  # the first slot is skipped...
    assert lf.slot_due(at(16, 5, days=1), u) == 16  # ...one wake a day: the middle slot
    now = at(16, 10, days=1)
    lf.open_window("busy", now)
    spend(u, 700_000, "busy")  # 0.084 of the 0.10 cap: she had something to do
    lf.close_expired(now + timedelta(minutes=40), u)
    assert lf._read()["idle"] == 0


def test_the_operator_talking_resets_the_pace(tmp_path):
    u, lf = usage(tmp_path), life(tmp_path)
    closed = datetime.now(UTC) - timedelta(hours=1)
    lf.path.write_text(json.dumps({"idle": 3, "closed": closed.timestamp()}), encoding="utf-8")
    spend(u, 1000)  # a dialogue row (no wake tag) newer than the last window
    assert lf.slot_due(at(11, 5), u) == 11  # not shrunk to one wake a day
    assert lf._read()["idle"] == 0


def test_usage_tags_rows_and_survives_a_currency_switch(tmp_path):
    u = usage(tmp_path)
    spend(u, 100_000)
    spend(u, 100_000, "w1")
    spend(u, 200_000, "w1")
    assert u.calls() == 3 and abs(u.spent_tag("w1") - 0.036) < 1e-9
    assert abs(u.spent_tagged() - 0.036) < 1e-9
    assert u.untagged_after(datetime.now(UTC) - timedelta(minutes=1))
    assert u.average_cost() is None  # fewer than five calls: no average yet
    for _ in range(3):
        spend(u, 100_000)
    assert abs(u.average_cost() - 0.014) < 1e-9
    other = Usage(replace(u.settings, currency="RUB"))
    assert other.spent() == 0 and other.spent_tagged() == 0


def test_life_part_enqueues_a_slot_as_an_ordinary_wake_once(tmp_path):
    async def scenario():
        u, lf = usage(tmp_path), life(tmp_path)
        wakes = HarnessWakes(tmp_path / "wakes.json")
        now = at(11, 5)
        snapshot = SimpleNamespace(
            autonomy="autonomous",
            activity=SimpleNamespace(value="OPEN"),
            scope=SCOPE,
            control_epoch=4,
        )
        host = SimpleNamespace(
            current=lambda: True, wakes=wakes, usage=u, settings=SimpleNamespace(currency="USD")
        )
        runtime = SimpleNamespace(
            harness=host,
            supervisor=SimpleNamespace(snapshot=snapshot, clock=SimpleNamespace(utc=lambda: now)),
        )
        part = LifePart(runtime, lf)  # type: ignore[arg-type]
        await part._step()
        [wake] = wakes._load()
        assert "GOALS.md" in wake.text and "DIARY.md" in wake.text and "11:00" in wake.text
        await part._step()  # the slot is handled and a wake is waiting: nothing stacks
        assert len(wakes._load()) == 1
        snapshot.autonomy = "observation"  # not autonomous: she does not start on her own
        lf.path.unlink()
        await part._step()
        assert len(wakes._load()) == 1

    asyncio.run(scenario())


def test_channel_daily_post_limit_counts_only_today(tmp_path):
    async def scenario():
        sent = []

        class Bot:
            def send_message(self, chat, text):
                sent.append(text)
                return 1

            def send_voice(self, chat, ogg, caption=""):
                return 2

        async def check(prompt):
            return True, ""

        journal = tmp_path / "channel.jsonl"
        yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        journal.write_text(
            json.dumps({"at": yesterday, "outcome": "posted", "kind": "text", "text": "x"}) + "\n",
            encoding="utf-8",
        )
        post = ChannelPost(
            ChannelSettings("@t", tmp_path / "rules.txt", journal, daily_posts=2), Bot(), check
        )
        for text in ("раз", "два"):
            assert "Опубликовано" in await post.post(text, voice=False, current=lambda: None)
        refused = await post.post("три", voice=False, current=lambda: None)
        assert refused.startswith("Не опубликовано") and sent == ["раз", "два"]

    asyncio.run(scenario())


def test_goals_and_diary_are_seeded_once_and_never_overwritten(tmp_path):
    seed_workspace(tmp_path)
    assert (tmp_path / "GOALS.md").read_text(encoding="utf-8") == GOALS_TEMPLATE
    (tmp_path / "GOALS.md").write_text("мои цели", encoding="utf-8")
    (tmp_path / "DIARY.md").write_text("запись", encoding="utf-8")
    seed_workspace(tmp_path)
    assert (tmp_path / "GOALS.md").read_text(encoding="utf-8") == "мои цели"
    assert (tmp_path / "DIARY.md").read_text(encoding="utf-8") == "запись"
