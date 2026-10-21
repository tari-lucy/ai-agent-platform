"""Delivery-owner evidence, exact source and consumed prefix share one session transaction."""

import asyncio

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from test_sessions import AREA, edit, opening, world

TEXT = "Synthetic observation about a lighthouse."


def test_delivered_reply_preserves_new_operator_input_and_replay(tmp_path):
    async def scenario():
        w, book, _ = await world(tmp_path)
        try:
            receipt = await book.open(opening())
            first = await w.source()
            await book.append(edit(receipt.resource_id, 1, key="first"), first)
            later = await w.source()
            await book.append(edit(receipt.resource_id, 2, key="later"), later)
            reply = await w.source()
            checked = []

            async def delivered(scope, identifier, source):
                checked.append((scope, identifier, source))
                if identifier != "confirmed-delivery":
                    raise ContractError(ErrorCode.ACCESS_DENIED)

            book.verify_delivery = delivered
            arguments = dict(
                generation=1,
                through_sequence=1,
                source=reply,
                text=TEXT,
                delivery_id="confirmed-delivery",
                context_sources=(first,),
            )
            result = await book.record_delivered(
                AREA, "record-reply", receipt.resource_id, **arguments
            )
            assert result == await book.record_delivered(
                AREA, "record-reply", receipt.resource_id, **arguments
            )
            assert len(checked) == 1
            session = await book.get(AREA, receipt.resource_id)
            assert session.last_sequence == 3 and session.processed_sequence == 1
            messages = await book.messages(AREA, session.id)
            assert [row.speaker for row in messages] == ["operator", "operator", "character"]
            assert messages[-1].reply_to_sequence == 1 and messages[-1].source == reply
            assert "text" not in messages[-1].model_dump()
            with pytest.raises(ContractError):
                await book.record_delivered(
                    AREA,
                    "record-reply",
                    receipt.resource_id,
                    **dict(arguments, text="changed payload"),
                )
        finally:
            await w.close()

    asyncio.run(scenario())


def test_unconfirmed_unshown_closed_or_revoked_reply_cannot_advance_session(tmp_path):
    async def scenario():
        w, book, allowed = await world(tmp_path)
        try:
            receipt = await book.open(opening())
            first = await w.source()
            await book.append(edit(receipt.resource_id, 1), first)
            reply = await w.source()
            arguments = dict(
                generation=1,
                through_sequence=1,
                source=reply,
                text=TEXT,
                delivery_id="delivery",
                context_sources=(first,),
            )
            with pytest.raises(ContractError):
                await book.record_delivered(AREA, "no-checker", receipt.resource_id, **arguments)

            async def denied(*args):
                raise ContractError(ErrorCode.ACCESS_DENIED)

            book.verify_delivery = denied
            with pytest.raises(ContractError):
                await book.record_delivered(AREA, "not-delivered", receipt.resource_id, **arguments)

            async def confirmed(*args):
                pass

            book.verify_delivery = confirmed
            for changes in (
                {"context_sources": ()},
                {"generation": 2},
                {"through_sequence": 2},
                {"text": "not source text"},
            ):
                with pytest.raises(ContractError):
                    await book.record_delivered(
                        AREA, "bad-reply", receipt.resource_id, **dict(arguments, **changes)
                    )
            assert (await book.get(AREA, receipt.resource_id)).processed_sequence == 0
            allowed[0] = False
            with pytest.raises(ContractError):
                await book.record_delivered(AREA, "stopped", receipt.resource_id, **arguments)
            allowed[0] = True
            result = await book.record_delivered(
                AREA, "confirmed", receipt.resource_id, **arguments
            )
            session = await book.get(AREA, receipt.resource_id)
            assert session.last_sequence == session.processed_sequence == 2
            assert result.version == session.version
            await book.close(edit(session.id, session.version, kind="close", key="close"))
            with pytest.raises(ContractError):
                await book.record_delivered(AREA, "late", session.id, **arguments)
        finally:
            await w.close()

    asyncio.run(scenario())
