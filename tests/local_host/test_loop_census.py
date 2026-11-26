"""The hourly loop census names what piles up on the event loop, by code location only."""

import asyncio

from ai_agent_core.bootstrap import loop_census


def test_census_groups_waiting_tasks_and_timers_by_location():
    async def scenario():
        async def leaky():
            while True:
                await asyncio.sleep(0.05)

        tasks = [asyncio.create_task(leaky()) for _ in range(3)]
        await asyncio.sleep(0.01)
        line = loop_census(asyncio.get_running_loop())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        return line

    line = asyncio.run(scenario())
    assert "leaky" in line and "x3" in line
    assert line.startswith("tasks=4 ")  # three leaks and the scenario itself
