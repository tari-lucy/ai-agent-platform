"""Batch reads use the same tenant/ACL/TTL/dependency policy and bounded SQL as get/list."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from ai_agent_application.storage import RecordAccess
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.memory import Source, Topic
from sqlalchemy import event
from test_storage import (
    GRANTS,
    MEMORY,
    NOW,
    OPERATOR,
    SCOPE,
    SOCIAL,
    new_store,
    save,
    source,
    topic,
)


def test_many_is_one_fetch_and_never_reuses_revoked_or_expired_rows(tmp_path):
    async def scenario():
        now = [NOW]
        store = await new_store(tmp_path, clock=lambda: now[0])
        try:
            for name, access in (("visible", True), ("private", False)):
                await save(
                    store,
                    source(name),
                    request_id=name,
                    access=(RecordAccess(OPERATOR.id, api_transfer=access),),
                )
            short = source("short")
            short = short.model_copy(
                update={
                    "retention": short.retention.model_copy(
                        update={"expires_at": NOW + timedelta(seconds=1)}
                    )
                }
            )
            await save(
                store,
                short,
                request_id="short",
                access=(RecordAccess(OPERATOR.id, api_transfer=True),),
            )
            other = SCOPE.model_copy(update={"persona_id": "other"})
            await store.provision(other, GRANTS)
            await save(
                store,
                source("foreign", scope=other),
                request_id="foreign",
                access=(RecordAccess(OPERATOR.id, api_transfer=True),),
            )
            queries = []

            def counted(conn, cursor, statement, params, context, many):
                queries.append(statement)

            event.listen(store.engine.sync_engine, "before_cursor_execute", counted)
            repo = store.repository(Source, OPERATOR, SCOPE)
            ids = ["visible", "private", "short", "foreign", "absent", "visible"]
            rows = await repo.get_many(ids, purpose="api_transfer")
            event.remove(store.engine.sync_engine, "before_cursor_execute", counted)
            assert {r.id for r in rows} == {"visible", "short"}
            assert sum(s.lstrip().startswith("SELECT") for s in queries) == 2  # schema + one fetch
            now[0] += timedelta(seconds=2)
            assert [r.id for r in await repo.get_many(ids, purpose="api_transfer")] == ["visible"]
            await save(
                store,
                source("visible", version=2),
                request_id="revoke",
                expected_version=1,
                access=(),
            )
            assert await repo.get_many(ids, purpose="api_transfer") == []
            assert [r.id for r in await repo.get_many(ids)] == ["private"]
            await store.provision(
                SCOPE,
                tuple(
                    replace(g, api_transfer=False) if g.principal == OPERATOR else g for g in GRANTS
                ),
            )
            assert await repo.get_many(["private"], purpose="api_transfer") == []
        finally:
            await store.close()

    asyncio.run(scenario())


def test_group_preserves_dependency_visibility_and_exact_private_expiry(tmp_path):
    async def scenario():
        now = [NOW]
        store = await new_store(tmp_path, clock=lambda: now[0])
        try:
            item = source("basis")
            item = item.model_copy(
                update={
                    "retention": item.retention.model_copy(
                        update={"expires_at": NOW + timedelta(seconds=1)}
                    )
                }
            )
            access = (
                RecordAccess(OPERATOR.id, api_transfer=True),
                RecordAccess(SOCIAL.id, api_transfer=True),
            )
            await save(store, item, access=access)
            ref = SourceRef(source_id=item.id, version=1, scope=SCOPE, availability="available")
            await save(
                store,
                topic(sources=[ref.model_dump(mode="json")]),
                SOCIAL,
                "topic",
                access=(RecordAccess(OPERATOR.id, api_transfer=True),),
            )
            group = await store.read_group((Source, Topic), OPERATOR, SCOPE, purpose="api_transfer")
            assert [r.id for r in group[Source]] == [item.id]
            assert len(group[Topic]) == 1
            now[0] += timedelta(seconds=2)
            group = await store.read_group((Source, Topic), OPERATOR, SCOPE)
            assert group[Source] == []
            assert group[Topic][0].sources[0].availability == "expired"
            group = await store.read_group((Source, Topic), OPERATOR, SCOPE, purpose="api_transfer")
            assert group[Source] == group[Topic] == []
        finally:
            await store.close()

    asyncio.run(scenario())


def test_batches_fail_closed_on_overflow_or_invalid_ids(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            for name in ("one", "two"):
                await save(store, source(name), request_id=name)
            repo = store.repository(Source, MEMORY, SCOPE)
            assert await repo.get_many([]) == []
            for invalid in (["../escape"], ["x"] * 201, "one", [1]):
                with pytest.raises(ContractError):
                    await repo.get_many(invalid)
            with pytest.raises(ContractError):
                await store.read_group((Source,), MEMORY, SCOPE, limit=1)
            selected = await store.read_group((Source,), MEMORY, SCOPE, ids={Source: ["two"]})
            assert [r.id for r in selected[Source]] == ["two"]
        finally:
            await store.close()

    asyncio.run(scenario())
