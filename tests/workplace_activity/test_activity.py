"""AW-05 activity contour on the production composition (build), SQLite and real owners.

The live adapter talks to a scripted synthetic transport (no network, no paid calls); what is
asserted is the route: admission, journal, shared daily limit, tasks/tickets owners, STOP and
restart. Scripted answers prove wiring, not model quality.
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

from ai_agent_application.agent_activity.model import ActivitySettings
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import CapabilityManifest, Ticket, WorkAttempt, WorkTask
from quality_support import Model, prepared_host
from test_http import post, scope_params

from ai_agent_core.local_host.activity import configure_activity
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.m3_composition.state import PRINCIPALS, put

ACTIVITY_MARKER = "Ты — рабочий контур персонажа"


class WorkModel(Model):
    def __init__(self) -> None:
        super().__init__()
        self.queues["activity"] = []

    @staticmethod
    def role(body: dict) -> str:
        if ACTIVITY_MARKER in body["messages"][0]["content"]:
            return "activity"
        return Model.role(body)


FAST = ActivitySettings(cooldown_seconds=0, check_seconds=0)


def fast(rt):
    """Test pacing only: look for work on every runtime tick, no cooldown between steps."""
    rt.activity = FAST
    rt.autonomous_activity.next_check = 0.0


async def prepared(tmp_path, monkeypatch, model):
    host = await prepared_host(tmp_path, model, monkeypatch)
    await enable_tickets(host.root)
    settings = await configure_activity(host.root, enabled=True)
    assert settings.enabled and not settings.own_goals
    return host


async def until(read, done, what, seconds=20.0):
    for _ in range(int(seconds / 0.05)):
        value = await read()
        if done(value):
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("timeout: " + what)


async def rows(rt, model, scope, owner=Owner.ACTIVITY):
    return await rt.store.repository(model, PRINCIPALS[owner], scope).list(limit=200)


async def assign(host, request_id, goal):
    response = await post(
        host.env,
        dict(
            type="work.assign",
            request_id=request_id,
            target="work",
            scope=host.scope.model_dump(mode="json"),
            arguments=dict(
                schema_id="work.assign-args", data={"goal": goal, "reason": "Поручение."}
            ),
        ),
    )
    assert response.status_code == 200, response.text
    return response.json()["resource_id"]


async def task(host, task_id):
    return await host.runtime.store.repository(
        WorkTask, PRINCIPALS[Owner.ACTIVITY], host.scope
    ).get(task_id)


async def manifest(rt, scope, capability_id):
    now = datetime.now(UTC)
    row = CapabilityManifest(
        id="manifest-" + capability_id,
        scope=scope,
        created_at=now,
        updated_at=now,
        version=1,
        capability_id=capability_id,
        capability_version=1,
        summary="Чтение общего справочника.",
        instructions="Ищет страницу справочника по запросу.",
        input_schema_id="reference.query",
        output_schema_id="reference.page",
        effect="none",
        areas=["reference"],
        network="none",
        runner_required=False,
    )
    await put(
        rt.store, rt.registry, Owner.WORKSPACE, scope, "seed-" + row.id, row, None, rt.readers
    )


def test_assignment_plan_ticket_answer_and_restart_stopped(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            # Stored while STOPPED; nothing runs and no model is called.
            first = await assign(host, "assign-1", "Составить конспект справочника.")
            await asyncio.sleep(1.5)
            assert model.count("activity") == 0 and (await task(host, first)).state == "open"
            await manifest(rt, scope, "reference.read")
            model.queues["activity"].append(
                dict(
                    action="use_capability",
                    capability_id="reference.read",
                    summary="Нужен справочник.",
                )
            )
            await host.start()
            waiting = await until(
                lambda: task(host, first), lambda t: t.state == "waiting", "capability ticket"
            )
            [ticket] = await rows(rt, Ticket, scope, Owner.TICKETS)
            assert (
                ticket.kind == "capability_request"
                and ticket.request.capability_id == "reference.read"
            )
            assert waiting.waiting_ticket_ids == [ticket.id]

            # Only the dependent task waits: a second assignment still gets its step.
            model.queues["activity"].append(
                dict(action="ask_operator", question="Какой формат конспекта?", summary="Уточняю.")
            )
            second = await assign(host, "assign-2", "Сделать конспект второй главы.")
            asked = await until(
                lambda: task(host, second), lambda t: t.state == "waiting", "question"
            )
            question = await rt.tickets.get(scope, asked.waiting_ticket_ids[0])
            assert question.kind == "question" and question.next_turn == "operator"
            calls = model.count("activity")
            await asyncio.sleep(1.5)
            assert model.count("activity") == calls  # NO_WORK: nothing answered, no model call

            model.queues["activity"].append(dict(action="complete", summary="Конспект готов."))
            answered = await post(
                host.env,
                dict(
                    type="ticket.reply",
                    request_id="reply-1",
                    target=question.id,
                    scope=scope.model_dump(mode="json"),
                    expected_version=question.version,
                    arguments=dict(
                        schema_id="ticket.reply-args",
                        data={
                            "ticket_id": question.id,
                            "body": "Списком тезисов.",
                            "outcome": "answer",
                        },
                    ),
                ),
            )
            assert answered.status_code == 200, answered.text
            done = await until(
                lambda: task(host, second), lambda t: t.state == "done", "completion"
            )
            assert done.outcome.startswith("Собственный вывод")  # not presented as verified
            body = model.last("activity")
            assert "Списком тезисов." in body["messages"][1]["content"]
            closed = await rt.tickets.get(scope, question.id)
            assert closed.state == "resolved"
            run_scope = rt.supervisor.snapshot.scope
            await until(
                lambda: rows(rt, WorkAttempt, run_scope),
                lambda found: len(found) == 3 and {a.state for a in found} == {"succeeded"},
                "settled attempts",
            )
            assert model.count("activity") == 3  # one call per step, no hidden retries
            # The first task is still waiting for its capability; nothing was granted.
            assert (await task(host, first)).state == "waiting"
            read = await host.env.client.get("/api/v1/work-tasks", params=scope_params(scope))
            assert read.status_code == 200 and {t["id"] for t in read.json()["items"]} == {
                first,
                second,
            }
        used = model.count("activity")
        async with host.open():
            rt = host.runtime
            fast(rt)
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            await asyncio.sleep(1.5)
            assert model.count("activity") == used  # restart never starts work

    asyncio.run(scenario())


def test_budget_share_malformed_answer_and_stop(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            fast(rt)
            # One call of activity share today; then the tick decides without the model.
            rt.private_api_budget = replace(rt.private_api_budget, activity_daily_calls=1)
            # A malformed/partial answer never acts: the task stays open, the attempt failed.
            model.queues["activity"].append({"action": "explode"})
            target = await assign(host, "assign-1", "Проверить заметки.")
            await host.start()
            await until(
                lambda: rt.store.repository(
                    WorkAttempt, PRINCIPALS[Owner.ACTIVITY], rt.supervisor.snapshot.scope
                ).list(),
                lambda found: any(a.state == "failed" for a in found),
                "failed attempt",
            )
            assert (await task(host, target)).state == "open"
            # The activity share is exhausted: no further call, not even to decide.
            await until(
                lambda: asyncio.sleep(0, rt.autonomous_activity.last_reason),
                lambda reason: reason == "BUDGET",
                "budget skip",
            )
            calls = model.count()
            await asyncio.sleep(1.5)
            assert model.count() == calls
            assert rt.autonomous_activity.last_reason == "BUDGET"
            # STOP: no further submission even with budget back.
            stop = await host.env.client.post(
                f"/api/v1/agents/{rt.supervisor.snapshot.agent_id}/stop",
                json=dict(
                    type="agent.stop",
                    request_id="stop-1",
                    target=rt.supervisor.snapshot.agent_id,
                    scope=rt.supervisor.snapshot.scope.model_dump(mode="json"),
                ),
                headers=host.env.headers,
            )
            assert stop.status_code == 200, stop.text
            rt.private_api_budget = replace(rt.private_api_budget, activity_daily_calls=20)
            await asyncio.sleep(1.5)
            assert model.count() == calls

    asyncio.run(scenario())


def test_own_goal_or_a_quiet_period_and_disabled_host(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            rt.activity = replace(FAST, own_goals=True, own_goal_daily=2)
            # Nothing assigned: the agent may choose an own goal, then works on it.
            model.queues["activity"] += [
                dict(action="goal", goal="Разобрать свои заметки о маяках.", reason="Интерес."),
                dict(action="complete", summary="Заметки разобраны."),
                dict(action="none", reason="Сейчас нечего делать."),
            ]
            await host.start()
            [own] = await until(
                lambda: rows(rt, WorkTask, scope),
                lambda found: len(found) == 1 and found[0].state == "done",
                "own goal done",
            )
            assert own.origin == "own_goal"
            # The second daily slot answered "none": a quiet period, no further calls today.
            await until(
                lambda: asyncio.sleep(0, rt.autonomous_activity.last_reason),
                lambda reason: reason == "NO_WORK",
                "quiet period",
            )
            calls = model.count("activity")
            await asyncio.sleep(1.5)
            assert calls == 3 and model.count("activity") == calls
            assert [t.id for t in await rows(rt, WorkTask, scope)] == [own.id]

    asyncio.run(scenario())


def test_assignment_unavailable_until_activity_is_enabled(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared_host(tmp_path, model, monkeypatch)
        async with host.open():
            response = await post(
                host.env,
                dict(
                    type="work.assign",
                    request_id="assign-1",
                    target="work",
                    scope=host.scope.model_dump(mode="json"),
                    arguments=dict(schema_id="work.assign-args", data={"goal": "Цель."}),
                ),
            )
            assert response.status_code >= 400 and "CAPABILITY_UNAVAILABLE" in response.text
            assert host.runtime.autonomous_activity.rt.activity is None

    asyncio.run(scenario())
