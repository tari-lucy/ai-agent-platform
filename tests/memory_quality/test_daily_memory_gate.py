"""The daily memory pass: its own task, and no heavy scan while nothing changed."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from ai_agent_core.m3_composition.memory_cycles import DailyMemory


def runtime():
    run = SimpleNamespace(
        state=SimpleNamespace(value="ACTIVE"),
        activity=SimpleNamespace(value="OPEN"),
        autonomy="autonomous",
        scope=SimpleNamespace(),
    )
    stamps = {"Episode": (101, "e"), "Summary": (3, "s"), "LLMAttempt": (273, "a")}

    async def kind_stamp(kind):
        return stamps[kind]

    async def active(limit):
        return SimpleNamespace(items=[])

    return SimpleNamespace(
        private_api_budget=SimpleNamespace(memory_processing=True),
        supervisor=SimpleNamespace(
            snapshot=run,
            admit=lambda kind: object(),
            retire=lambda token: None,
            clock=SimpleNamespace(
                elapsed=lambda: 1000.0, utc=lambda: datetime(2026, 11, 1, 12, tzinfo=UTC)
            ),
        ),
        store=SimpleNamespace(kind_stamp=kind_stamp),
        jobs=SimpleNamespace(store=SimpleNamespace(active=active)),
    )


def test_the_pass_runs_as_its_own_task_and_never_twice_at_once():
    async def scenario():
        memory = DailyMemory(runtime())
        started, release, count = asyncio.Event(), asyncio.Event(), []

        async def slow(elapsed):
            count.append(elapsed)
            started.set()
            await release.wait()

        memory._pass = slow  # type: ignore[method-assign]
        memory.tick_background()  # returns at once: the runtime tick is not held
        await started.wait()
        memory.next_check = 0
        memory.tick_background()  # one is still running: no second pass
        assert len(count) == 1
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        memory.next_check = 0
        memory.tick_background()  # the finished one is collected, a new one may start
        await asyncio.sleep(0)
        assert len(count) == 2
        await memory.close()

    asyncio.run(scenario())


def test_while_nothing_changed_the_heavy_steps_are_not_run():
    async def scenario():
        rt = runtime()
        memory = DailyMemory(rt)
        calls = []

        async def cycles(scope):
            return []

        async def continue_failed(scope, found):
            calls.append("continue_failed")
            return False

        memory.cycles = cycles  # type: ignore[method-assign]
        memory.continue_failed = continue_failed  # type: ignore[method-assign]
        memory.idle_stamp = (
            "2026-11-01",
            (101, "e"),
            (3, "s"),
            (273, "a"),
            0,
            "",
        )
        await memory._pass(1000.0)
        assert calls == []  # the same stamp as the last idle pass: nothing was scanned
        assert memory.idle_stamp is not None

        rt.store.kind_stamp = lambda kind: asyncio.sleep(0, result=(102, "e2"))  # type: ignore[assignment]
        try:
            await memory._pass(1000.0)
        except Exception:  # noqa: BLE001 - the scan itself needs a real store; only the gate matters
            pass
        assert calls == ["continue_failed"]  # something changed: the full pass starts again

    asyncio.run(scenario())
