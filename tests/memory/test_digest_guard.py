"""A digest cannot introduce an unpinned source or save after its transfer right is revoked."""

import pytest
from ai_agent_application.extraction.digest import Digester, digest_id
from ai_agent_application.extraction.model import SummaryResult
from ai_agent_application.llm import Generation
from ai_agent_contracts.memory import Summary
from memory_support import MEMORY, command, make_scope, new_world, run
from test_retained_excerpts import ref, summary

A = make_scope()


@pytest.mark.parametrize("revoke_during_call", [False, True])
def test_digest_source_fence(tmp_path, revoke_during_call):
    async def scenario():
        world = await new_world(tmp_path, [A])
        sources = [await world.add(A, f"Факт {i} о маяке.") for i in range(2)]
        ids = ["summary-d0", "summary-d1"]
        for i, source in enumerate(sources):
            await summary(world, A, f"d{i}", f"По словам собеседницы, факт {i}.", [ref(source)])

        class Generator:
            calls = 0

            async def generate(self, request, cancel=None):
                self.calls += 1
                if revoke_during_call:
                    await world.memory.set_rights(
                        command(A),
                        sources[-1].source_id,
                        1,
                        read_allowed=True,
                        api_transfer_allowed=False,
                        public_disclosure_allowed=False,
                    )
                return Generation(
                    request.operation_id, "completed", None, None,
                    SummaryResult(content="Два факта по словам собеседницы."),
                    "", (), False, None,
                )

        generator = Generator()
        pinned = [ref(s) for s in sources] if revoke_during_call else [ref(sources[0])]
        result = await Digester(
            world.memory, generator, admitted=lambda: True, clock=world.clock
        ).digest(A, ids, source_refs=pinned)
        stored = await world.store.repository(Summary, MEMORY, A).get(digest_id(A, ids))
        await world.store.close()
        return result, stored, generator.calls

    result, stored, calls = run(scenario())
    assert result is None and stored is None
    assert calls == int(revoke_during_call)
