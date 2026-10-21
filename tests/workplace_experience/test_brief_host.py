"""The person's brief on the production composition: memory reaches the step within rights.

Real build, memory owner with its recall index, activity enabled offline; synthetic data and no
model call. A memory allowed for transfer is shown to the person's model; one that is not, or
one revoked later, is not (the host rights let activity confirm api_transfer, the source's own
flag still decides).
"""

import asyncio

from ai_agent_application.memory import SourceDraft
from ai_agent_contracts.states import Provenance
from test_activity import WorkModel, prepared

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command
from ai_agent_core.m3_composition.workplace_experience import persona


def test_brief_shows_transferable_memory_and_hides_the_rest(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        async with host.open():
            rt, scope = host.runtime, host.scope

            async def remember(name, text, api):
                return await memory(rt).register_source(
                    command(scope, "src-" + name),
                    SourceDraft(
                        provenance=Provenance.OPERATOR_INPUT,
                        source_type="operator-message",
                        adapter_id="dialogue",
                        adapter_version="1",
                        occurred_at=rt.supervisor.clock.utc(),
                        content=text,
                        read_allowed=True,
                        api_transfer_allowed=api,
                        public_disclosure_allowed=False,
                    ),
                )

            shared = await remember("shared", "Маяк на мысе зажигают в сумерках.", True)
            await remember("private", "Маяк: личная записка, только для меня.", False)
            brief = await persona(rt).build(scope, "маяк на мысе")
            assert "зажигают в сумерках" in brief.text
            assert "личная записка" not in brief.text
            assert [ref.source_id for ref in brief.sources] == [shared.source_id]

            await memory(rt).set_rights(
                command(scope, "revoke"),
                shared.source_id,
                shared.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            later = await persona(rt).build(scope, "маяк на мысе")
            assert "зажигают в сумерках" not in later.text and later.sources == ()

    asyncio.run(scenario())
