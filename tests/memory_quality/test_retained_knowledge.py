"""After the full conversation expires, the next reply still sees the knowledge from it.

Real owners/HTTP on a temporary SQLite with a virtual store clock, the real retention owner and
independent ledger. What the scripted model SAW is read from its request.
"""

import asyncio
from datetime import timedelta

from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore
from ai_agent_application.memory import ClaimDraft, SourceDraft
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Owner
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.states import Provenance
from dev_support import answer
from integration_support import prepared
from test_history import send
from test_http import start
from test_memory_rights_and_control import allow_memory, fragments

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command

TALK = "Сначала про погоду. Мой талисман — бирюзовый фонарик бабушки. Потом про чай."
CLAIM = "По словам Ники, её талисман — бирюзовый фонарик."


def test_the_next_reply_sees_retained_knowledge_after_the_conversation_expires(tmp_path):
    async def scenario():
        replies = [answer(reply=f"Ответ {n}.") for n in range(2)]
        async with prepared(tmp_path, replies) as env:
            rt, scope = env.runtime, env.world.scope
            physical = SQLitePhysicalStore(rt.store)
            await physical.install()
            ledger = SQLiteDeletionLedger(tmp_path / "independent-ledger.sqlite")
            retention = RetentionService(
                rt.store,
                PRINCIPALS[Owner.MEMORY],
                memory(rt),
                physical,
                ledger,
                index=env.index,
                clock=rt.store.clock,
            )
            try:
                await start(env)
                allow_memory(env)
                # 30 minutes of the 30-day term are left: inside the 7-day lead
                made = await memory(rt).register_source(
                    command(scope, "src-talk"),
                    SourceDraft(
                        provenance=Provenance.OPERATOR_INPUT,
                        source_type="fixture",
                        adapter_id="fixture",
                        adapter_version="1",
                        occurred_at=rt.store.clock() - timedelta(days=30, minutes=-30),
                        content=TALK,
                        read_allowed=True,
                        api_transfer_allowed=True,
                        public_disclosure_allowed=False,
                    ),
                )
                ref = SourceRef(
                    source_id=made.source_id, version=1, scope=scope, availability="available"
                )
                await memory(rt).record_claim(
                    command(scope, "clm-talk"),
                    ClaimDraft("claim-talk", "interlocutor", CLAIM, "reported", (ref,), 0.6),
                )
                kept = await retention.sweep(scope)
                env.world.clock.now += timedelta(minutes=35)  # the full text is past its term
                late = await retention.sweep(scope)
                await send(env, "q1", "Какой у меня талисман?")
                shown = fragments(env)
                prompt = " ".join(m.content for m in env.world.provider.calls[-1].messages)
                return dict(kept=kept, late=late, shown=shown, prompt=prompt, raw=made.source_id)
            finally:
                ledger.close()

    out = asyncio.run(scenario())
    assert out["late"].expired == (out["raw"],) and out["late"].unconsolidated == ()
    assert [m["text"] for m in out["shown"]] == [CLAIM]  # the knowledge stays in the reply
    assert out["shown"][0]["by"] == "утверждение-reported"  # still attributed as reported
    assert "Потом про чай" not in out["prompt"]  # the rest of the conversation is gone
