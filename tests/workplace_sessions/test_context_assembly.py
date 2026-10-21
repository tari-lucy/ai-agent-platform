"""Bounded R1 context on a real SessionBook, Source owner and FileReleaseGuard."""

import asyncio

import pytest
from ai_agent_application.agent_activity.session_context import ReleasedSessionContext
from ai_agent_application.agent_experience.session import ContextRefs, SessionContext
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Source
from test_sessions import AREA, SUPPORT, edit, opening, world


class OwnerSources:
    def __init__(self, w):
        self.w = w

    async def get_source(self, scope, source_id, *, purpose):
        return await self.w.store.repository(Source, SUPPORT["CAT"], scope).get(
            source_id, purpose=purpose
        )


def test_pending_inputs_are_not_skipped_and_all_shown_bases_travel_with_context(tmp_path):
    async def scenario():
        w, book, _ = await world(tmp_path)
        try:
            opened = await book.open(opening())
            inputs = []
            for sequence in range(1, 4):
                ref = await w.source()
                inputs.append(ref)
                await book.append(edit(opened.resource_id, sequence, key=f"input-{sequence}"), ref)

            class Context:
                async def build(self, scope, query, ticket_ids=()):
                    return SessionContext("A synthetic persona brief", (), ContextRefs(), 0)

                async def still_valid(self, scope, refs):
                    return True

            provider = ReleasedSessionContext(book, Context(), OwnerSources(w), w.guard)
            session = await book.get(AREA, opened.resource_id)
            admission = await w.admit()
            built = await provider.build(admission, session, max_messages=4, max_chars=2000)
            assert built.through_sequence == 2  # pending 3 waits; it is not dropped for newest data
            assert built.sources == tuple(inputs[:2])
            assert len(built.messages) == 4 and len(built.files) == 0
            assert [message.role for message in built.messages] == [
                "system",
                "user",
                "user",
                "user",
            ]
            assert len(await book.messages(AREA, opened.resource_id)) == 3
            with pytest.raises(ContractError):
                await provider.build(admission, session, max_messages=4, max_chars=10)
            await book.close(edit(opened.resource_id, 4, kind="close", key="close"))
            with pytest.raises(ContractError):
                await provider.build(admission, session, max_messages=4, max_chars=2000)
        finally:
            await w.close()

    asyncio.run(scenario())


def test_late_stop_during_source_read_discards_the_assembled_context(tmp_path):
    async def scenario():
        w, book, _ = await world(tmp_path)
        try:
            opened = await book.open(opening())
            await book.append(edit(opened.resource_id, 1), await w.source())

            class Context:
                async def build(self, scope, query, ticket_ids=()):
                    return SessionContext("", (), ContextRefs(), 0)

                async def still_valid(self, scope, refs):
                    return True

            class Sources(OwnerSources):
                async def get_source(self, scope, source_id, *, purpose):
                    result = await super().get_source(scope, source_id, purpose=purpose)
                    assert result is not None
                    w.active = False  # cancellation arrives while the owner is reading
                    return result

            provider = ReleasedSessionContext(book, Context(), Sources(w), w.guard)
            admission = await w.admit()
            with pytest.raises(ContractError):
                await provider.build(
                    admission,
                    await book.get(AREA, opened.resource_id),
                    max_messages=4,
                    max_chars=2000,
                )
        finally:
            await w.close()

    asyncio.run(scenario())
