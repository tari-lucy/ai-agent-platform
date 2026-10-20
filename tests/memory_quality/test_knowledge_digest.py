"""Old summaries of one month become one digest, once, within the memory reserve.

Production-built host, public profile, real owners and the live adapter over a synthetic scripted
transport. Summaries are seeded through the memory owner with long-lived (protected) grounds, so
the test is about folding, not about expiry.
"""

import asyncio
from datetime import timedelta

from ai_agent_application.memory import SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Summary
from ai_agent_contracts.states import Provenance
from quality_support import Model, prepared_host

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command, put


class DigestModel(Model):
    """The scripted provider also recognises the digest prompt (same consolidator role)."""

    @staticmethod
    def role(body):
        if "Сожми несколько сводок" in body["messages"][0]["content"]:
            return "consolidator"
        return Model.role(body)


DAYS = ("Говорили о маяке.", "По словам Ники, она любит чай.", "Обсуждали ярмарку.")


async def seed(rt, scope, name, text, days_ago):
    now = rt.supervisor.clock.utc()
    made = await memory(rt).register_source(
        command(scope, "src-" + name),
        SourceDraft(
            provenance=Provenance.OPERATOR_INPUT,
            source_type="fixture",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=now - timedelta(days=days_ago),
            content=text,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
            protected=True,
        ),
    )
    ref = SourceRef(source_id=made.source_id, version=1, scope=scope, availability="available")
    period = now - timedelta(days=days_ago)
    record = Summary(
        id="summary-" + name,
        scope=scope,
        created_at=now,
        updated_at=now,
        version=1,
        period_start=period - timedelta(hours=1),
        period_end=period,
        episode_ids=[],
        sources=[ref],
        content="Сводка: " + text,
        analysis_id="cons-" + name,
    )
    await put(rt.store, rt.registry, Owner.MEMORY, scope, "sum-" + name, record, None, rt.readers)
    return ref


def test_a_month_of_old_summaries_is_folded_once(tmp_path, monkeypatch):
    async def scenario():
        model = DigestModel()
        host = await prepared_host(tmp_path, model, monkeypatch)
        out: dict = {}
        async with host.open():
            await host.start()
            rt, scope = host.runtime, host.scope
            now = rt.supervisor.clock.utc()
            # three summaries of one calendar month, all older than 30 days
            month_end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0) - timedelta(
                days=40
            )
            offset = (now - month_end).days
            refs = [await seed(rt, scope, f"d{n}", t, offset + n) for n, t in enumerate(DAYS)]
            await seed(rt, scope, "fresh", "Свежая сводка.", 2)  # too recent: not folded
            model.queues["consolidator"].append(
                dict(content="За месяц: маяк, чай по словам Ники и ярмарка.")
            )
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            for _ in range(200):
                rows = await rt.store.repository(Summary, PRINCIPALS[Owner.MEMORY], scope).list(
                    limit=50
                )
                digests = [s for s in rows if s.analysis_id.startswith("dig-")]
                if digests:
                    break
                await asyncio.sleep(0.05)
            out["digests"] = digests
            out["refs"] = {r.source_id for r in refs}
            out["system"] = model.last("consolidator")["messages"][0]["content"]
            out["user"] = model.last("consolidator")["messages"][-1]["content"]
            for _ in range(200):  # the digest job must be over, or the next check just yields
                if not (await rt.jobs.store.active(limit=1)).items:
                    break
                await asyncio.sleep(0.05)
            calls = model.count()
            rt.daily_memory.next_check = 0
            await rt.daily_memory.tick()
            model.queues["consolidator"].append(dict(content="Повторный дайджест."))
            for _ in range(100):  # give a wrongly submitted job time to call the model
                if model.count() > calls:
                    break
                await asyncio.sleep(0.05)
            out["again"] = model.count() - calls
        return out

    out = asyncio.run(scenario())
    assert len(out["digests"]) == 1
    digest = out["digests"][0]
    assert digest.content.startswith("Дайджест сводок (интерпретация")
    assert {r.source_id for r in digest.sources} == out["refs"]  # grounded by their sources
    assert "Свежая сводка" not in out["user"] and "Говорили о маяке" in out["user"]
    assert "Не добавляй фактов" in out["system"]
    assert out["again"] == 0  # the same month is never digested twice
