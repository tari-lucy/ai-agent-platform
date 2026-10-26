"""W7 wake-ups: production host owners, synthetic harness/provider only.

- ``wake_me`` stores a timer and opens the diary conversation once;
- nothing fires while STOPPED or before the timer is due;
- a due timer fires after START as a ``wake`` message the session driver answers;
- quiet hours and the hourly cap hold it back; a closed diary drops it.
"""

import asyncio
from datetime import timedelta

from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_sessions import WakeTimer
from session_support import ack, install
from test_activity import WorkModel, fast, prepared, until
from test_context_revocation import ready
from test_turn_journal import journaling
from test_work_sessions_http import transcript

from ai_agent_core.m3_composition.state import PRINCIPALS, put
from ai_agent_core.m3_composition.work_session_wake import (
    DIARY,
    AutonomousWake,
    WakeSettings,
    wake_me,
)


async def due_now(rt, scope, timer_id):
    """Fixture time travel: the stored timer becomes due (the owner's own record, rewritten)."""
    repo = rt.store.repository(WakeTimer, PRINCIPALS[Owner.ACTIVITY], scope)
    timer = await repo.get(timer_id)
    moved = timer.model_copy(
        update={"version": timer.version + 1, "due_at": rt.supervisor.clock.utc() - timedelta(1)}
    )
    await put(rt.store, rt.registry, Owner.ACTIVITY, scope, "due-" + timer_id, moved, timer.version)


def test_wake_fires_into_the_diary_only_when_active_and_answers_there(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope)
            seen: list[str] = []
            journaling(rt, seen)
            waker = AutonomousWake(rt, WakeSettings(max_per_hour=1))
            rt.autonomous_wake = waker

            made = await wake_me(rt, scope, lambda: None, "key-1", "30", "Проверить сборку")
            again = await wake_me(rt, scope, lambda: None, "key-1", "30", "Проверить сборку")
            assert made == again and made["conversation"] == DIARY  # replay: one timer
            second = await wake_me(rt, scope, lambda: None, "key-2", "60", "Second")
            assert second["timer_id"] != made["timer_id"]
            for bad in (("0", "x"), ("10081", "x"), ("5", " "), ("abc", "x")):
                try:
                    await wake_me(rt, scope, lambda: None, "bad", *bad)
                except Exception as error:
                    assert "INVALID_ARGUMENTS" in repr(error) or "Неверн" in str(error) or error
                else:
                    raise AssertionError(bad)

            assert await waker.tick() == "RUN_NOT_AUTONOMOUS"  # STOPPED: nothing fires
            await due_now(rt, scope, made["timer_id"])
            assert await waker.tick() == "RUN_NOT_AUTONOMOUS"

            model.queues["activity"].append({"reply": "Synthetic diary answer"})
            await host.start()
            assert await waker.tick() == "FIRED"
            sessions = await host.env.client.get(
                "/api/v1/work-sessions",
                params=dict(zip(
                    ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
                    scope.partition, strict=True,
                )),
            )  # fmt: skip
            (diary,) = [s for s in sessions.json()["items"] if s["title"] == DIARY]
            view = await until(lambda: transcript(host, diary["id"]), ready, "diary draft", 60)
            wake = view["entries"][0]
            assert wake["speaker"] == "wake" and "Проверить сборку" in wake["text"]
            assert "Будильник" in seen[-1] and "Проверить сборку" in seen[-1]
            assert (await ack(host, view["pending_reply"], "wake-ack")).status_code == 200
            view = await transcript(host, diary["id"])
            assert view["entries"][-1]["speaker"] == "character"
            assert view["entries"][-1]["reply_to_sequence"] == wake["sequence"]

            # The hourly cap holds the second one back even when due.
            await due_now(rt, scope, second["timer_id"])
            waker.next_check = 0.0
            assert await waker.tick() == "HOURLY_CAP"
            waker.settings = WakeSettings(max_per_hour=5, quiet=(0, 0))
            waker.next_check = 0.0
            hour = rt.supervisor.clock.utc().astimezone().hour
            waker.settings = WakeSettings(max_per_hour=5, quiet=(hour, (hour + 1) % 24))
            assert await waker.tick() == "QUIET_HOURS"

    asyncio.run(scenario())


def test_quiet_hours_wrap_midnight():
    from datetime import UTC, datetime

    settings = WakeSettings(quiet=(23, 8))
    local = datetime.now(UTC).astimezone()
    for hour, quiet in ((23, True), (2, True), (7, True), (8, False), (15, False)):
        moment = local.replace(hour=hour, minute=0)
        assert settings.quiet_at(moment) is quiet, hour
