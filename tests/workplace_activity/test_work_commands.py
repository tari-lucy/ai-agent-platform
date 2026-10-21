"""AW-07 operator commands on work over real HTTP (production build, SQLite, scripted transport).

Assign while STOPPED; abandon pinned to the task version: a lost reply is retried with the same
request id (replay), a decision on an outdated view conflicts; a dropped task never runs after
START; restart stays STOPPED and the records read back. The activity share is reported.
"""

import asyncio

from test_activity import WorkModel, assign, fast, prepared, task
from test_http import post, scope_params


def abandon(host, request_id, task_id, version, reason="Больше не нужно."):
    return post(
        host.env,
        dict(
            type="work.abandon",
            request_id=request_id,
            target=task_id,
            scope=host.scope.model_dump(mode="json"),
            expected_version=version,
            arguments=dict(
                schema_id="work.abandon-args", data={"task_id": task_id, "reason": reason}
            ),
        ),
    )


def test_abandon_replay_conflict_and_restart(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            fast(rt)
            first = await assign(host, "assign-1", "Разобрать заметки.")
            second = await assign(host, "assign-2", "Составить план.")
            current = await task(host, first)
            stale = await abandon(host, "drop-stale", first, current.version + 1)
            assert stale.status_code in (400, 409), stale.text
            done = await abandon(host, "drop-1", first, current.version)
            assert done.status_code == 200, done.text
            again = await abandon(host, "drop-1", first, current.version)  # lost reply, retried
            assert again.status_code == 200 and again.json() == done.json()
            other = await abandon(host, "drop-2", first, current.version)  # outdated view
            assert other.status_code == 409, other.text
            dropped = await task(host, first)
            assert dropped.state == "abandoned"
            assert dropped.outcome.startswith("Отменено оператором")
            assert dropped.version == current.version + 1

            status = await host.env.client.get(
                "/api/v1/private-api-status", params=scope_params(host.scope)
            )
            assert status.json()["activity_daily_calls"] == 20
            assert status.json()["activity_calls_used"] == 0

            # The dropped task never runs; the other one does.
            model.queues["activity"].append(dict(action="complete", summary="План готов."))
            await host.start()
            for _ in range(200):
                if (await task(host, second)).state == "done":
                    break
                await asyncio.sleep(0.05)
            assert (await task(host, second)).state == "done"
            assert model.count("activity") == 1
            assert (await task(host, first)).state == "abandoned"
        async with host.open():
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            read = await host.env.client.get("/api/v1/work-tasks", params=scope_params(host.scope))
            states = {row["id"]: row["state"] for row in read.json()["items"]}
            assert states == {first: "abandoned", second: "done"}

    asyncio.run(scenario())
