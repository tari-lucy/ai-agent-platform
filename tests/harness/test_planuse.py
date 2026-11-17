"""Her use of the ChatGPT plan, counted from her own session, and what the home screen shows of it."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ai_agent_contracts.common import Scope
from ai_agent_core.m3_composition import harness_limit as hl
from ai_agent_core.m3_composition.harness_autonomy import AutonomyPart
from ai_agent_core.m3_composition.harness_now import build
from ai_agent_core.m3_composition.harness_planuse import PlanUsage, turn_tokens

NOW = datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="p-work", memory_mode="work")


def ms(at: datetime) -> int:
    return int(at.timestamp() * 1000)


def turn(
    ident: str, at: datetime, new: int, out: int = 0, cached: int = 0, role: str = "assistant"
) -> dict:
    return {
        "info": {
            "id": ident,
            "role": role,
            "time": {"created": ms(at), "completed": ms(at)},
            "tokens": {
                "input": new,
                "output": out,
                "reasoning": 0,
                "cache": {"read": cached, "write": 0},
            },
        }
    }


class Api:
    def __init__(self, rows: list[dict]) -> None:
        self.rows, self.calls = rows, []

    async def opencode(self, method: str, path: str, body=None, *, timeout=60):
        self.calls.append(path)
        if path == "/session":
            return [
                {"id": "old", "time": {"updated": 1}},
                {"id": "ses_a", "time": {"updated": 5}},
                {"id": "gone", "time": {"updated": 9, "archived": 9}},
            ]
        return self.rows


class Clock:
    def __init__(self) -> None:
        self.now, self.mono = NOW, 0.0

    def utc(self) -> datetime:
        return self.now

    def monotonic(self) -> float:
        return self.mono

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)
        self.mono += timedelta(**delta).total_seconds()


def test_a_turn_counts_the_new_tokens_and_not_the_cache() -> None:
    info = turn("m", NOW, new=1000, out=200, cached=90000)["info"]
    assert turn_tokens(info) == 1200
    assert turn_tokens({}) == 0 and turn_tokens({"tokens": {"input": "x"}}) == 0


def test_the_windows_add_up_and_a_turn_is_counted_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        api = Api(
            [
                turn("a", NOW - timedelta(hours=1), 3000),
                turn("b", NOW - timedelta(hours=10), 5000),
                turn("c", NOW - timedelta(days=3), 7000),
                turn("d", NOW - timedelta(days=9), 99999),
                turn("u", NOW, 1, role="user"),
            ]
        )
        usage = PlanUsage(tmp_path / "plan.jsonl", clock.utc, clock.monotonic)
        assert await usage.poll(api.opencode) == 4
        totals = usage.totals()
        # the 10-hour-old turn is in the day but not the window; the 9-day-old one is out of the week
        assert (totals.window, totals.day, totals.week) == (3000, 8000, 15000)
        clock.advance(minutes=2)
        assert await usage.poll(api.opencode) == 0  # the same turns are not added again
        assert usage.totals().week == 15000

    asyncio.run(scenario())


def test_the_session_is_looked_at_once_a_minute_and_the_latest_live_session_is_used(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        clock, api = Clock(), Api([])
        usage = PlanUsage(tmp_path / "plan.jsonl", clock.utc, clock.monotonic)
        await usage.poll(api.opencode)
        await usage.poll(api.opencode)  # inside the minute: no look
        assert api.calls == [
            "/session",
            "/session/ses_a/message?limit=40",
        ]  # the archived one is not taken
        clock.advance(seconds=61)
        await usage.poll(api.opencode)
        assert len(api.calls) == 4

    asyncio.run(scenario())


def test_the_figures_survive_a_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock, where = Clock(), tmp_path / "plan.jsonl"
        first = PlanUsage(where, clock.utc, clock.monotonic)
        await first.poll(Api([turn("a", NOW - timedelta(hours=2), 4000)]).opencode, "ses_a")
        again = PlanUsage(where, clock.utc, clock.monotonic)
        assert again.totals().window == 4000
        assert (
            await again.poll(
                Api([turn("a", NOW - timedelta(hours=2), 4000)]).opencode, "ses_a", force=True
            )
            == 0
        )
        assert (
            len(where.read_text(encoding="utf-8").splitlines()) == 1
        )  # and a turn is written once

    asyncio.run(scenario())


def test_a_limit_hit_keeps_what_the_five_hours_before_had_used(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        usage = PlanUsage(tmp_path / "plan.jsonl", clock.utc, clock.monotonic)
        await usage.poll(
            Api(
                [
                    turn("a", NOW - timedelta(hours=1), 3000),
                    turn("b", NOW - timedelta(hours=2), 400),
                ]
            ).opencode,
            "s",
        )
        until = NOW + timedelta(hours=3)
        usage.note_limit(until)
        usage.note_limit(until + timedelta(minutes=5))  # the same hit seen again: kept once
        totals = usage.totals()
        assert totals.last_limit_tokens == 3400 and totals.last_limit_at == NOW
        assert len(usage.limits) == 1

    asyncio.run(scenario())


def test_a_broken_or_missing_journal_is_no_failure(tmp_path: Path) -> None:
    where = tmp_path / "plan.jsonl"
    where.write_text(
        "not json\n" + json.dumps({"kind": "turn", "at": "bad", "id": "x", "tokens": 5}) + "\n",
        encoding="utf-8",
    )
    assert PlanUsage(where, lambda: NOW).totals().week == 0
    assert PlanUsage(tmp_path / "nope" / "x.jsonl", lambda: NOW).totals().week == 0
    usage = PlanUsage(None, lambda: NOW)  # no disk at all: still counts in memory
    asyncio.run(usage.poll(Api([turn("a", NOW, 10)]).opencode, "s", force=True))
    assert usage.totals().window == 10


def test_the_gate_tells_the_figures_about_a_new_sleep_once() -> None:
    async def scenario() -> None:
        clock, told = Clock(), []
        error = {
            "name": "APIError",
            "data": {
                "statusCode": 429,
                "message": "x",
                "responseBody": json.dumps(
                    {"error": {"type": "usage_limit_reached", "resets_in_seconds": 7200}}
                ),
            },
        }
        rows = [
            {
                "info": {
                    "id": "m",
                    "role": "assistant",
                    "error": error,
                    "time": {"created": ms(NOW), "completed": ms(NOW)},
                }
            }
        ]

        async def opencode(method, path, body=None, *, timeout=60):
            return rows

        clock.advance(seconds=5)
        gate = hl.LimitGate(
            opencode, clock.utc, mono=clock.monotonic, on_sleep=lambda s: told.append(s.until)
        )
        await gate.check("s")
        await gate.check("s")  # asleep: not looked at again, not told again
        assert len(told) == 1

    asyncio.run(scenario())


def home(tmp_path: Path, part: AutonomyPart):
    folder = tmp_path / "files"
    folder.mkdir(exist_ok=True)
    return SimpleNamespace(
        parts=[part],
        speaker=None,
        listener=None,
        folders=lambda: folder,
        brain=None,
        look=None,
        imagine=None,
        hear=None,
        avatar=None,
        stage=None,
        chats=None,
        channel=None,
        people=None,
    )


def autonomy(clock: Clock, usage: PlanUsage | None, gate: hl.LimitGate | None) -> AutonomyPart:
    part = AutonomyPart(
        SimpleNamespace(supervisor=SimpleNamespace(clock=SimpleNamespace(utc=clock.utc))),
        SimpleNamespace(),
    )
    part.usage, part.limit = usage, gate
    return part


def test_the_home_screen_shows_the_sleep_and_the_use(tmp_path: Path) -> None:
    async def scenario() -> None:
        clock = Clock()
        usage = PlanUsage(tmp_path / "plan.jsonl", clock.utc, clock.monotonic)
        await usage.poll(Api([turn("a", NOW - timedelta(hours=1), 3000)]).opencode, "s")
        gate = hl.LimitGate(Api([]).opencode, clock.utc)
        gate.sleep = hl.Sleep(until=NOW + timedelta(hours=2), since=NOW, source="headers")
        picture = build(home(tmp_path, autonomy(clock, usage, gate)), SCOPE, NOW)
        assert picture.sleep is not None and picture.sleep.source == "headers"
        assert picture.sleep.until == (NOW + timedelta(hours=2)).isoformat(timespec="seconds")
        assert picture.doing is not None and picture.doing.startswith("Сплю: лимит подписки")
        assert picture.plan_use is not None and picture.plan_use.window == 3000
        awake = build(
            home(tmp_path, autonomy(clock, usage, hl.LimitGate(Api([]).opencode, clock.utc))),
            SCOPE,
            NOW,
        )
        assert awake.sleep is None and awake.plan_use is not None and awake.plan_use.week == 3000
        nothing = build(home(tmp_path, autonomy(clock, None, None)), SCOPE, NOW)
        assert (
            nothing.sleep is None and nothing.plan_use is None
        )  # no figures yet: nothing is invented

    asyncio.run(scenario())
