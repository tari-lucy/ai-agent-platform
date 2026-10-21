"""R1 logical session contract/owner on real SQLite, no host or model calls."""

import asyncio
import runpy
from pathlib import Path

import pytest
from ai_agent_application.agent_activity.sessions import SessionBook
from ai_agent_contracts.agent_sessions import WorkSession, WorkSessionMessage
from ai_agent_contracts.api import CreateCommand, EditCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import ErrorCode
from pydantic import ValidationError

from ai_agent_core.m3_composition import new_registry

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / "workplace_files" / "wf_support.py"))
AREA, ACT, NOW = (SUPPORT[key] for key in ("AREA", "ACT", "NOW"))


def opening(key="session", **data):
    return CreateCommand.model_validate(
        dict(
            request_id=key,
            scope=AREA,
            target="work",
            type="work.session.open",
            arguments=dict(
                schema_id="work.session.open-args",
                data={
                    "title": "A synthetic project",
                    **data,
                },
            ),
        ),
        context={"extensions": new_registry()},
    )


def edit(session, version, *, key="input", kind="message", **data):
    return EditCommand.model_validate(
        dict(
            request_id=key,
            scope=AREA,
            target=session,
            type="work.session." + kind,
            expected_version=version,
            arguments=dict(
                schema_id="work.session." + kind + "-args",
                data={
                    "session_id": session,
                    **(
                        {"text": "Synthetic observation about a lighthouse."}
                        if kind == "message"
                        else {}
                    ),
                    **data,
                },
            ),
        ),
        context={"extensions": new_registry()},
    )


async def world(tmp_path, **limits):
    w = await SUPPORT["World"].create(tmp_path)
    w.store.extensions = new_registry()
    allowed = [True]

    def authorize(scope):
        if not allowed[0] or scope.partition != AREA.partition:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    async def source_check(scope, ref, expected_text):
        row = await w.store.repository(Source, ACT, scope).get(ref.source_id)
        if row is None or row.version != ref.version or row.content != expected_text:
            raise ContractError(ErrorCode.ACCESS_DENIED)

    book = SessionBook(
        w.store,
        ACT,
        w.clock,
        authorize=authorize,
        verify_source=source_check,
        **limits,
    )
    return w, book, allowed


def test_ordered_reference_history_replay_conflict_and_close(tmp_path):
    async def scenario():
        w, book, _ = await world(tmp_path)
        try:
            opened = await book.open(opening())
            assert await book.open(opening()) == opened
            with pytest.raises(ContractError):
                await book.open(opening(title="Changed request"))
            source = await w.source()
            with pytest.raises(ContractError):
                await book.append(edit(opened.resource_id, 1, text="Different body"), source)
            request = edit(opened.resource_id, 1)
            posted = await book.append(request, source)
            assert posted.version == 2 and await book.append(request, source) == posted
            rows = await book.messages(AREA, opened.resource_id)
            assert len(rows) == 1 and rows[0].sequence == 1 and rows[0].source == source
            assert "text" not in rows[0].model_dump()
            assert await book.messages(AREA, opened.resource_id, after_sequence=1) == []
            with pytest.raises(ContractError) as failure:
                await book.append(edit(opened.resource_id, 1, key="stale"), source)
            assert failure.value.error.code == ErrorCode.VERSION_CONFLICT
            second_source = await w.source()
            await book.append(edit(opened.resource_id, 2, key="second"), second_source)
            assert [row.sequence for row in await book.messages(AREA, opened.resource_id)] == [1, 2]
            assert [
                row.sequence
                for row in await book.messages(AREA, opened.resource_id, after_sequence=1)
            ] == [2]
            assert await book.append(request, source) == posted
            closing = edit(opened.resource_id, 3, kind="close", key="close")
            closed = await book.close(closing)
            assert await book.close(closing) == closed
            assert (await book.get(AREA, opened.resource_id)).state == "closed"
            with pytest.raises(ContractError):
                await book.append(edit(opened.resource_id, 4, key="late"), source)
        finally:
            await w.close()

    asyncio.run(scenario())


def test_current_authority_source_scope_and_quota_are_enforced(tmp_path):
    async def scenario():
        w, book, allowed = await world(tmp_path, max_sessions=1, max_messages=1)
        try:
            opened = await book.open(opening())
            with pytest.raises(ContractError) as failure:
                await book.open(opening("over-cap"))
            assert failure.value.error.code == ErrorCode.RESOURCE_BUSY
            source = await w.source()
            allowed[0] = False
            with pytest.raises(ContractError):
                await book.append(edit(opened.resource_id, 1), source)
            allowed[0] = True
            foreign = source.model_copy(
                update={
                    "scope": AREA.model_copy(update={"memory_space_id": "foreign"}),
                }
            )
            with pytest.raises(ContractError):
                await book.append(edit(opened.resource_id, 1), foreign)
            await book.append(edit(opened.resource_id, 1), source)
            with pytest.raises(ContractError) as failure:
                await book.append(edit(opened.resource_id, 2, key="over-messages"), source)
            assert failure.value.error.code == ErrorCode.RESOURCE_BUSY
        finally:
            await w.close()

    asyncio.run(scenario())


def test_context_references_and_message_order_are_closed_contracts():
    base = dict(id="session", scope=AREA, version=1, created_at=NOW, updated_at=NOW, title="Work")
    assert WorkSession(**base).last_sequence == 0
    for changes in (
        {"processed_sequence": 1},
        {"state": "waiting"},
        {"task_ids": ["same", "same"]},
        {"scope": AREA.model_copy(update={"agent_id": "agent", "run_id": "run"})},
    ):
        with pytest.raises(ValidationError):
            WorkSession(**(base | changes))
    source = dict(source_id="source", version=1, scope=AREA, availability="available")
    message = dict(
        id="message",
        scope=AREA,
        version=1,
        created_at=NOW,
        updated_at=NOW,
        session_id="session",
        sequence=2,
        speaker="operator",
        source=source,
    )
    assert WorkSessionMessage(**message, reply_to_sequence=1)
    with pytest.raises(ValidationError):
        WorkSessionMessage(**message, reply_to_sequence=2)
    with pytest.raises(ValidationError):
        WorkSessionMessage(**message, text="A forbidden duplicate body")


def test_history_gap_fails_instead_of_claiming_it_is_complete(tmp_path):
    async def scenario():
        w, book, _ = await world(tmp_path)
        try:
            opened = await book.open(opening())
            await book.append(edit(opened.resource_id, 1), await w.source())
            # A purger has made a reference unavailable: owner must not treat it as an empty page.
            from sqlalchemy import text

            async with w.store.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE records SET available=0 WHERE kind='WorkSessionMessage'")
                )
            with pytest.raises(ContractError):
                await book.messages(AREA, opened.resource_id)
        finally:
            await w.close()

    asyncio.run(scenario())
