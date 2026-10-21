"""The old activity entry must use R2's released view, never raw ticket body/title."""

import asyncio
from types import SimpleNamespace

from ai_agent_application.agent_experience.session import ContextRefs, SessionContext, TicketView
from ai_agent_contracts.common import Scope

from ai_agent_core.m3_composition.activity import context


def test_activity_uses_released_title_and_messages_instead_of_raw_ticket():
    async def scenario():
        raw = SimpleNamespace(
            kind="question",
            state="waiting_reply",
            next_turn="agent",
            title="PRIVATE_SOURCE_TITLE",
            request=None,
        )

        class Tickets:
            async def get(self, scope, ticket_id):
                return raw

            async def messages(self, scope, ticket_id):
                raise AssertionError("raw ticket message path must not be used")

        scope = Scope(
            workspace_id="sample",
            persona_id="fictional",
            memory_space_id="work",
            memory_mode="work",
        )
        task = SimpleNamespace(scope=scope, goal="Synthetic work", waiting_ticket_ids=["ticket"])
        released = SessionContext(
            "",
            (
                TicketView(
                    "ticket",
                    "question",
                    "waiting_reply",
                    "agent",
                    "Обращение скрыто",
                    (("agent", "Hidden"), ("operator", "My independent answer")),
                    True,
                ),
            ),
            ContextRefs(),
            1,
        )
        shown = await context(SimpleNamespace(tickets=Tickets()), task, released)
        assert shown.replies == (("Обращение скрыто", "My independent answer"),)
        assert "PRIVATE_SOURCE_TITLE" not in str(shown)
        assert shown.consumed_ticket_ids == ("ticket",)

    asyncio.run(scenario())
