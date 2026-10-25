"""History rows are bounded owner batches; gaps and foreign scopes stay closed."""

import asyncio

import pytest
from ai_agent_contracts.codec import ContractError
from sqlalchemy import event
from test_sessions import AREA, edit, opening, world


def test_history_reads_eight_rows_in_a_bounded_number_of_queries(tmp_path):
    async def scenario():
        w, book, _ = await world(tmp_path)
        try:
            made = await book.open(opening())
            for sequence in range(1, 9):
                await book.append(
                    edit(made.resource_id, sequence, key=f"row-{sequence}"), await w.source()
                )
            selects = []

            def count(conn, cursor, statement, params, context, many):
                if statement.lstrip().startswith("SELECT"):
                    selects.append(1)

            event.listen(w.store.engine.sync_engine, "before_cursor_execute", count)
            try:
                rows = await book.entries(AREA, made.resource_id, limit=8)
            finally:
                event.remove(w.store.engine.sync_engine, "before_cursor_execute", count)
            assert [sequence for sequence, _ in rows] == list(range(1, 9))
            assert all(row is not None and row.sequence == sequence for sequence, row in rows)
            assert len(selects) <= 6  # schema, session, message batch and dependency projection
            assert len(await book.entries(AREA, made.resource_id, after_sequence=8)) == 0
            with pytest.raises(ContractError):
                await book.entries(
                    AREA.model_copy(update={"persona_id": "foreign"}), made.resource_id
                )
        finally:
            await w.close()

    asyncio.run(scenario())
