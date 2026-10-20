"""A memory cycle that failed after a saved stage continues from there, once, without repaying.

Production-built host, public profile, real owners/HTTP and the live adapter over a synthetic
scripted transport. Calls are counted per role from the requests the "provider" received.
"""

import asyncio
from datetime import UTC, datetime, timedelta

from ai_agent_application.storage import Owner
from ai_agent_contracts.llm import LLMAttempt
from ai_agent_contracts.memory import Episode, MemoryCycle
from quality_support import Model, prepared_host

from ai_agent_core.m3_composition.memory_cycles import obstacle, resume_key
from ai_agent_core.m3_composition.state import PRINCIPALS

FACTS = ("Ника любит маяки.", "Ника пьёт чай с чабрецом.")


async def cycles(host):
    return await host.runtime.store.repository(
        MemoryCycle, PRINCIPALS[Owner.MEMORY], host.scope
    ).list(limit=50)


async def settle(host, operation_id):
    stored = await host.finish(operation_id, succeeded=False)
    return stored.status.value


def test_a_failed_summary_continues_without_paying_the_extraction_again(tmp_path, monkeypatch):
    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        out: dict = {}
        async with host.open():
            await host.start()
            for number, fact in enumerate(FACTS):
                await host.say(f"m{number}", fact, "Запомнила.")
            rt = host.runtime
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list(limit=50)
            model.queues["extractor"].append(dict(proposals=[]))
            # the provider answers the summary with something unusable (a known failure)
            model.queues["consolidator"].append(dict(wrong="x"))
            made = await host.process("cycle-1", [e.id for e in episodes])
            assert made.status_code == 200, made.text
            out["first"] = await settle(host, made.json()["operation_id"])
            failed = (await cycles(host))[0]
            out["failed"] = (failed.status, failed.stage)
            out["paid_before"] = (model.count("extractor"), model.count("consolidator"))
            # the next autonomous check continues the batch from the saved stage
            model.queues["consolidator"].append(dict(content="Ника любит маяки и чай."))
            model.queues["reflection"].append(
                dict(keep_state_reason="Двух разговоров мало для изменения.", changes=[])
            )
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            resumed = await rt.store.repository(
                MemoryCycle, PRINCIPALS[Owner.MEMORY], host.scope
            ).get(resume_key(host.scope, failed))
            assert resumed is not None
            await host.finish(resumed.operation_id)
            done = await rt.store.repository(MemoryCycle, PRINCIPALS[Owner.MEMORY], host.scope).get(
                resumed.id
            )
            out["resumed"] = (done.status, done.stage, done.episode_ids == failed.episode_ids)
            out["paid_after"] = (model.count("extractor"), model.count("consolidator"))
            # another check never continues the same failure twice nor reopens the batch
            calls = model.count()
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            out["again"] = (model.count() - calls, len(await cycles(host)))
        return out

    out = asyncio.run(scenario())
    assert out["first"] == "FAILED" and out["failed"] == ("failed", "consolidate")
    assert out["paid_before"] == (1, 1)
    assert out["resumed"] == ("completed", "done", True)
    assert out["paid_after"] == (1, 2)  # extraction is never paid again
    assert out["again"] == (0, 2)


def attempt(status, at):
    return LLMAttempt.model_construct(status=status, started_at=at)


def test_only_a_proven_in_process_failure_may_continue():
    t0 = datetime(2026, 10, 19, tzinfo=UTC)

    def cycle(status, *, episodes=("e1",), error="CAPABILITY_UNAVAILABLE"):
        return MemoryCycle.model_construct(
            id="c-" + status,
            status=status,
            stage="consolidate",
            episode_ids=list(episodes),
            created_at=t0,
            updated_at=t0 + timedelta(minutes=5),
            error_code=error,
        )

    failed = cycle("failed")
    inside = t0 + timedelta(minutes=2)
    outside = t0 + timedelta(hours=1)
    assert obstacle(failed, [failed], [attempt("failed", inside)], False) is None
    assert obstacle(failed, [failed], [attempt("unknown", inside)], False) == "ATTEMPT_UNKNOWN"
    assert obstacle(failed, [failed], [attempt("unknown", outside)], False) is None
    assert obstacle(cycle("unknown"), [], [], False) == "OUTCOME_UNKNOWN"
    assert obstacle(cycle("completed", error=None), [], [], False) == "NOT_FAILED"
    assert obstacle(failed, [failed], [], True) == "ALREADY_RESUMED"
    assert obstacle(failed, [failed] * 3, [], False) == "RETRY_LIMIT"
