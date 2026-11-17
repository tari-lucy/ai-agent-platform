"""When the plan's usage limit is out she sleeps until the window opens.

The error is read from the session, the wakes are held back (not lost), one wake is put at the
reset, and she wakes by herself.
"""

import asyncio
import json
from datetime import UTC, datetime, timedelta

from ai_agent_contracts.common import Scope

from ai_agent_core.m3_composition import harness_limit as hl
from ai_agent_core.m3_composition.harness_autonomy import (
    LIMIT_OVER_TEXT,
    DueWake,
    HarnessAutonomy,
    WakeRun,
)
from ai_agent_core.m3_composition.work_session_wake import WakeSettings

NOW = datetime(2026, 11, 17, 22, 0, tzinfo=UTC)
SCOPE = Scope(workspace_id="w", persona_id="p", memory_space_id="m", memory_mode="work")


def stamp(at):
    return int(at.timestamp() * 1000)


def limit_error(headers=None, body=None, message="The usage limit has been reached", status=429):
    return {
        "name": "APIError",
        "data": {
            "message": message,
            "statusCode": status,
            "isRetryable": True,
            "responseHeaders": headers or {},
            "responseBody": json.dumps(
                body if body is not None else {"error": {"type": "usage_limit_reached"}}
            ),
        },
    }


def row(error=None, at=NOW, role="assistant"):
    info = {"id": "m", "role": role, "time": {"created": stamp(at), "completed": stamp(at)}}
    if error is not None:
        info["error"] = error
    return {"info": info}


def test_a_usage_limit_is_told_from_an_ordinary_failure():
    assert hl.is_limit(limit_error())
    assert (
        hl.is_limit(
            limit_error(body={}, message="You exceeded your current quota", status=429) | {}
        )
        is False
    )
    assert hl.is_limit(limit_error(body={"error": {"type": "insufficient_quota"}}))
    assert not hl.is_limit(limit_error(status=500))  # a server failure: OpenCode retries
    assert not hl.is_limit(limit_error(body={}, message="Too many requests"))  # a brief rate limit
    assert hl.is_limit(
        limit_error(body={}, message="x", headers={"x-codex-primary-used-percent": "100.0"})
    )
    assert not hl.is_limit({"name": "ContextOverflowError"})


def test_the_reset_comes_from_the_windows_that_are_used_up_and_the_latest_of_them():
    error = limit_error(
        headers={
            "x-codex-primary-used-percent": "100.0",
            "x-codex-primary-reset-after-seconds": "3600",
            "x-codex-secondary-used-percent": "100.0",
            "x-codex-secondary-reset-after-seconds": "86400",
        }
    )
    after, source = hl.reset_after(error, NOW)
    assert (
        after == timedelta(days=1) and source == "headers"
    )  # the weekly window outlasts the 5-hour one
    half = limit_error(
        headers={
            "x-codex-primary-used-percent": "100.0",
            "x-codex-primary-reset-after-seconds": "3600",
            "x-codex-secondary-used-percent": "40.0",
            "x-codex-secondary-reset-after-seconds": "86400",
        }
    )
    assert hl.reset_after(half, NOW)[0] == timedelta(
        hours=1
    )  # a window with room left does not decide


def test_the_reset_can_be_an_absolute_time_or_come_from_the_body_or_retry_after():
    at = NOW + timedelta(minutes=50)
    by_clock = limit_error(
        headers={
            "x-codex-primary-used-percent": "100",
            "x-codex-primary-reset-at": str(at.timestamp()),
        }
    )
    assert hl.reset_after(by_clock, NOW)[0] == timedelta(minutes=50)
    assert hl.reset_after(limit_error(body={"error": {"resets_in_seconds": 1200}}), NOW) == (
        timedelta(minutes=20),
        "body",
    )
    assert hl.reset_after(
        limit_error(body={"error": {"resets_at": (NOW + timedelta(hours=2)).timestamp()}}), NOW
    )[0] == timedelta(hours=2)
    assert hl.reset_after(limit_error(headers={"retry-after": "300"}), NOW)[0] == timedelta(
        minutes=5
    )
    assert hl.reset_after(limit_error(body={}), NOW) == (None, "default")


def test_the_sleep_lasts_until_the_reset_plus_a_margin():
    error = limit_error(
        headers={
            "x-codex-primary-used-percent": "100",
            "x-codex-primary-reset-after-seconds": "7200",
        }
    )
    sleep = hl.read_limit([row(error=error)], NOW + timedelta(seconds=5))
    assert sleep.until == NOW + timedelta(hours=2) + hl.MARGIN and sleep.source == "headers"
    unknown = hl.read_limit([row(error=limit_error(body={}))], NOW + timedelta(seconds=5))
    assert unknown.until == NOW + hl.DEFAULT_SLEEP + hl.MARGIN and unknown.source == "default"


def test_a_limit_that_is_over_or_a_session_that_answered_fine_is_no_sleep():
    old = hl.read_limit(
        [row(error=limit_error(body={"error": {"resets_in_seconds": 60}}))],
        NOW + timedelta(hours=1),
    )
    assert old is None  # the window opened since
    assert (
        hl.read_limit(
            [row(error=limit_error()), row(at=NOW + timedelta(minutes=5))],
            NOW + timedelta(minutes=6),
        )
        is None
    )
    assert hl.read_limit([row(role="user")], NOW) is None
    assert hl.read_limit("garbage", NOW) is None


def test_the_newest_assistant_message_decides_whatever_order_the_rows_come_in():
    fresh = row(error=limit_error(body={"error": {"resets_in_seconds": 7200}}), at=NOW)
    healthy = row(at=NOW - timedelta(hours=3))
    assert hl.read_limit([healthy, fresh], NOW + timedelta(minutes=1)) is not None
    assert hl.read_limit([fresh, healthy], NOW + timedelta(minutes=1)) is not None


def test_the_sleep_is_capped_at_the_longest_window():
    sleep = hl.read_limit(
        [row(error=limit_error(body={"error": {"resets_in_seconds": 90 * 86400}}))], NOW
    )
    assert sleep.until == NOW + hl.MAX_SLEEP


def test_describe_says_how_long_and_until_when():
    sleep = hl.Sleep(until=NOW + timedelta(hours=3, minutes=20), since=NOW, source="headers")
    text = hl.describe(sleep, NOW)
    assert "3 ч 20 мин" in text and "01:20 UTC" in text
    assert "45 мин" in hl.describe(
        hl.Sleep(until=NOW + timedelta(minutes=45), since=NOW, source="body"), NOW
    )


class Clock:
    def __init__(self):
        self.now, self.mono = NOW, 0.0

    def utc(self):
        return self.now

    def monotonic(self):
        return self.mono

    def advance(self, **delta):
        self.now += timedelta(**delta)
        self.mono += timedelta(**delta).total_seconds()


class Session:
    """The native API for one session: it answers the looks and counts the posts."""

    def __init__(self):
        self.rows, self.gets, self.posts = [], 0, 0

    async def opencode(self, method, path, body=None, *, timeout=60):
        if method == "GET":
            self.gets += 1
            return self.rows
        self.posts += 1
        self.rows.append({"info": {"id": body["messageID"], "sessionID": "ses_a", "role": "user"}})
        return {"info": {"role": "assistant"}}


def test_the_gate_looks_once_a_minute_and_holds_until_the_reset():
    async def scenario():
        clock, session = Clock(), Session()
        session.rows = [row(error=limit_error(body={"error": {"resets_in_seconds": 3600}}), at=NOW)]
        clock.advance(seconds=10)
        gate = hl.LimitGate(session.opencode, clock.utc, mono=clock.monotonic)
        assert (await gate.check("ses_a")) is not None and session.gets == 1
        for _ in range(5):  # asleep: no more looks at all
            clock.advance(minutes=5)
            assert (await gate.check("ses_a")) is not None
        assert session.gets == 1
        clock.advance(hours=1)  # the window opens
        session.rows = [row(at=NOW + timedelta(hours=1))]
        assert (
            await gate.check("ses_a")
        ) is None and session.gets == 2  # one look finds her answering again
        clock.advance(seconds=10)
        assert (
            await gate.check("ses_a")
        ) is None and session.gets == 2  # and then once a minute at most

    asyncio.run(scenario())


def test_a_limit_that_is_still_on_after_the_sleep_puts_her_back_to_sleep():
    async def scenario():
        clock, session = Clock(), Session()
        error = limit_error(body={"error": {"resets_in_seconds": 600}})
        session.rows = [row(error=error, at=NOW)]
        clock.advance(seconds=5)
        gate = hl.LimitGate(session.opencode, clock.utc, mono=clock.monotonic)
        first = await gate.check("ses_a")
        clock.advance(minutes=15)  # the guess was too short
        session.rows = [
            row(error=limit_error(body={"error": {"resets_in_seconds": 7200}}), at=clock.now)
        ]
        second = await gate.check("ses_a")
        assert second is not None and second.until > first.until

    asyncio.run(scenario())


def test_fresh_names_a_sleep_once():
    gate = hl.LimitGate(Session().opencode, lambda: NOW)
    gate.sleep = hl.Sleep(until=NOW + timedelta(hours=1), since=NOW, source="body")
    assert gate.fresh() is gate.sleep and gate.fresh() is None
    gate.sleep = hl.Sleep(until=NOW + timedelta(hours=2), since=NOW, source="body")
    assert gate.fresh() is gate.sleep


class Owner:
    """The wake book: one due wake, and the wakes that were planned."""

    def __init__(self):
        self.state, self.planned = "new", []
        self.wake = DueWake("timer-a", "ses_a", NOW, "Что-то случилось в игре.")

    async def due(self, scope, now):
        return self.wake

    async def recent_count(self, scope, now):
        return 0

    async def reserve(self, scope, wake, message_id, guard):
        state = self.state
        if state == "new":
            self.state = "pending"
        return state

    async def delivered(self, scope, wake, message_id, guard):
        self.state = "done"

    async def schedule(self, scope, minutes, text, session, guard):
        self.planned.append((minutes, text, session))


def dispatcher(session, owner, clock, gate):
    run = WakeRun(SCOPE, 1, True, True, True, True)
    return HarnessAutonomy(
        session, owner, lambda: run, lambda s, e: None, clock.utc, lambda: 1, WakeSettings(), gate
    )


def test_a_wake_during_the_sleep_is_held_not_sent_and_one_wake_is_planned_for_the_reset():
    async def scenario():
        clock, session, owner = Clock(), Session(), Owner()
        session.rows = [row(error=limit_error(body={"error": {"resets_in_seconds": 7200}}), at=NOW)]
        clock.advance(seconds=10)
        gate = hl.LimitGate(session.opencode, clock.utc, mono=clock.monotonic)
        for _ in range(4):  # a game event, an idle nudge, ...
            assert await dispatcher(session, owner, clock, gate).tick() == "PROVIDER_LIMIT"
            clock.advance(minutes=1)
        assert session.posts == 0 and owner.state == "new"  # nothing went out, nothing was reserved
        assert len(owner.planned) == 1  # one alarm for the end, not one per wake
        minutes, text, ses = owner.planned[0]
        assert 120 <= minutes <= 125 and text == LIMIT_OVER_TEXT and ses == "ses_a"

    asyncio.run(scenario())


def test_after_the_reset_the_held_wake_goes_out():
    async def scenario():
        clock, session, owner = Clock(), Session(), Owner()
        session.rows = [row(error=limit_error(body={"error": {"resets_in_seconds": 600}}), at=NOW)]
        clock.advance(seconds=10)
        gate = hl.LimitGate(session.opencode, clock.utc, mono=clock.monotonic)
        assert await dispatcher(session, owner, clock, gate).tick() == "PROVIDER_LIMIT"
        clock.advance(minutes=15)
        session.rows = [row(at=clock.now)]
        assert await dispatcher(session, owner, clock, gate).tick() == "FIRED"
        assert session.posts == 1 and owner.state == "done"

    asyncio.run(scenario())


def test_urgent_wakes_are_held_too_and_a_dispatcher_without_a_gate_behaves_as_before():
    async def scenario():
        clock, session, owner = Clock(), Session(), Owner()
        owner.wake = DueWake("timer-a", "ses_a", NOW, "Рядом враг.", urgent=True)
        session.rows = [row(error=limit_error(body={"error": {"resets_in_seconds": 7200}}), at=NOW)]
        clock.advance(seconds=10)
        gate = hl.LimitGate(session.opencode, clock.utc, mono=clock.monotonic)
        assert await dispatcher(session, owner, clock, gate).tick() == "PROVIDER_LIMIT"
        plain = Owner()
        assert (
            await dispatcher(session, plain, clock, None).tick() == "FIRED"
        )  # no gate, no look, no hold

    asyncio.run(scenario())
