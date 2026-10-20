"""An old relevant claim/summary outside the recent windows reaches the next model request.

Production-built host, the public profile, real HTTP owners and the live adapter over a synthetic
scripted transport. Memory is seeded through the memory owner. What the model SAW is asserted
from the request body; a scripted answer proves the route and the rules, not the text quality.
"""

import asyncio
import logging
from datetime import timedelta

from ai_agent_application.memory import ClaimDraft, SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import Summary
from ai_agent_contracts.states import Provenance
from quality_support import Model, prepared_host, view

from ai_agent_core.local_host.init import test_scope as lab_scope
from ai_agent_core.m3_composition import long_term
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command, put

OLD_SAID = "Ника: мой талисман — бирюзовый фонарик, его подарила бабушка."
OLD_CLAIM = "По словам Ники, её талисман — бирюзовый фонарик от бабушки."
OLD_SUMMARY = "Говорили о детстве. По словам Ники, бирюзовый фонарик — её талисман."
QUESTION = "Напомни, какой у меня талисман? Что за фонарик?"
# five exchanges are ten turns: the seeded message leaves the 8-turn window
FILLER = [(f"Вымышленная реплика {n} про погоду.", f"Ответ {n} про погоду.") for n in range(5)]


async def source(rt, scope: Scope, name: str, text: str, *, transfer: bool = True) -> SourceRef:
    made = await memory(rt).register_source(
        command(scope, "src-" + name),
        SourceDraft(
            provenance=Provenance.OPERATOR_INPUT,
            source_type="operator-message",
            adapter_id="dialogue",
            adapter_version="1",
            occurred_at=rt.supervisor.clock.utc(),
            content=text,
            read_allowed=True,
            api_transfer_allowed=transfer,
            public_disclosure_allowed=False,
            external_id="ext-" + name,
        ),
    )
    return SourceRef(
        source_id=made.source_id, version=made.version, scope=scope, availability="available"
    )


async def claim(rt, scope: Scope, name: str, text: str, ref: SourceRef) -> None:
    await memory(rt).record_claim(
        command(scope, "clm-" + name),
        ClaimDraft("claim-" + name, "interlocutor", text, "reported", (ref,), 0.6),
    )


async def summary(rt, scope: Scope, name: str, text: str, ref: SourceRef) -> None:
    now = rt.supervisor.clock.utc()
    record = Summary(
        id="summary-" + name,
        scope=scope,
        created_at=now,
        updated_at=now,
        version=1,
        period_start=now - timedelta(hours=1),
        period_end=now,
        episode_ids=[],
        sources=[ref],
        content=text,
        analysis_id="fixture-analysis",
    )
    await put(rt.store, rt.registry, Owner.MEMORY, scope, "sum-" + name, record, None, rt.readers)


def test_old_relevant_memory_beyond_the_windows_reaches_the_next_request(
    tmp_path, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)

    async def scenario():
        model = Model()
        host = await prepared_host(tmp_path, model, monkeypatch)
        out: dict = {}
        async with host.open():
            await host.start()
            rt, scope = host.runtime, host.scope
            old = await source(rt, scope, "old", OLD_SAID)
            await claim(rt, scope, "old", OLD_CLAIM, old)
            await summary(rt, scope, "old", OLD_SUMMARY, old)
            # the same topic without a transfer right, and in a TEST space: never shown
            closed = await source(
                rt, scope, "closed", "Ника: фонарик-талисман зелёный.", transfer=False
            )
            await claim(
                rt, scope, "closed", "По словам Ники, её талисман — зелёный фонарик.", closed
            )
            lab = lab_scope(scope, host.config.test_space_ids[0])
            lab_ref = await source(rt, lab, "lab", "Ника: талисман — оранжевый фонарик.")
            await claim(rt, lab, "lab", "По словам Ники, талисман — оранжевый фонарик.", lab_ref)
            # newer, less relevant records fill both recent windows (40 claims, 5 summaries)
            noise = await source(rt, scope, "noise", "Ника: обсуждали погоду, порт и чай.")
            for n in range(long_term.CLAIM_WINDOW + 5):
                await claim(rt, scope, f"noise-{n}", f"По словам Ники, тема {n}: погода.", noise)
            for n in range(long_term.SUMMARY_WINDOW + 1):
                await summary(rt, scope, f"noise-{n}", f"Сводка {n}: погода и порт.", noise)
            for index, (text, reply) in enumerate(FILLER):  # the old talk leaves the 8 turns
                await host.say(f"f{index}", text, reply)
            real = long_term.archived

            async def recent_only(*args):
                return [], []

            monkeypatch.setattr(long_term, "archived", recent_only)  # behaviour before the fix
            await host.say("before", QUESTION, "Не помню.")
            out["before"] = view(model.last("character"))
            monkeypatch.setattr(long_term, "archived", real)
            caplog.clear()
            await host.say("after", QUESTION, "Бирюзовый фонарик.")
            out["after"] = view(model.last("character"))
            out["log"] = next(
                r.message for r in caplog.records if r.message.startswith("Private memory")
            )
        return out

    out = asyncio.run(scenario())
    before = " ".join(m["text"] for m in out["before"]["memory"])
    assert "бирюзовый" not in before  # the old record was lost behind the recent windows
    seen = out["after"]
    turns = " ".join(text for _, text in seen["turns"])
    assert len(seen["turns"]) == 8 and "талисман" not in turns.replace(QUESTION, "")
    shown_as = [(m["by"], m["text"]) for m in seen["memory"]]
    assert ("утверждение-reported", OLD_CLAIM) in shown_as  # attribution reaches the prompt
    assert ("сводка-интерпретация", OLD_SUMMARY) in shown_as
    assert len(seen["memory"]) <= long_term.MAX_FRAGMENTS and all(m["at"] for m in seen["memory"])
    shown = " ".join(m["text"] for m in seen["memory"])
    assert "зелёный" not in shown and "оранжевый" not in shown  # no closed or TEST memory
    assert "По словам Ники, тема" not in shown  # newer noise claims stay out
    assert sum(len(m["text"]) for m in seen["memory"]) <= 650 * 3  # its own token ceiling
    assert "archive=2" in out["log"] and "талисман" not in out["log"]
