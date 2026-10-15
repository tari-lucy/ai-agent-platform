"""Real SQLite acceptance, invented IDs/content only. No application runtime or providers."""

import asyncio
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.common import Scope, SourceRef
from ai_agent_contracts.memory import AccountLink, Claim, Person, Source, Topic
from ai_agent_contracts.states import ErrorCode
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import TypeAdapter
from sqlalchemy import func, insert, select, text

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NOW = datetime(2026, 10, 13, tzinfo=UTC)
SCOPE = Scope(
    workspace_id="synthetic-workspace",
    persona_id="synthetic-persona",
    memory_space_id="synthetic-space",
    memory_mode="test",
)
MEMORY = Principal("owner-memory", "core_owner", Owner.MEMORY)
SOCIAL = Principal("owner-social", "core_owner", Owner.SOCIAL)
OPERATOR = Principal("operator-synthetic", "operator")
GRANTS = (
    Grant(MEMORY, Owner.MEMORY, read=True, write=True, api_transfer=True, public_disclosure=True),
    Grant(SOCIAL, Owner.SOCIAL, read=True, write=True, api_transfer=True, public_disclosure=True),
    Grant(SOCIAL, Owner.MEMORY, read=True, api_transfer=True, public_disclosure=True),
    Grant(OPERATOR, Owner.MEMORY, read=True, api_transfer=True, public_disclosure=True),
    Grant(OPERATOR, Owner.SOCIAL, read=True, api_transfer=True, public_disclosure=True),
)


def decode(model, value):
    return from_wire(TypeAdapter(model), json.dumps(value).encode())


def source(record_id="source-01", scope=SCOPE, **changes):
    value = dict(
        id=record_id,
        source_id=record_id,
        scope=scope.model_dump(mode="json"),
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        version=1,
        provenance="simulation" if scope.memory_mode == "test" else "observation",
        source_type="synthetic-text",
        adapter_id="fixture",
        adapter_version="1",
        occurred_at=NOW.isoformat(),
        registered_at=NOW.isoformat(),
        retention={
            "policy_id": "synthetic-policy",
            "expires_at": (NOW + timedelta(days=30)).isoformat(),
        },
        availability="available",
        content="Вымышленный исходный текст.",
        read_allowed=True,
        api_transfer_allowed=True,
        public_disclosure_allowed=False,
    )
    value.update(changes)
    return decode(Source, value)


def topic(record_id="topic-01", scope=SCOPE, **changes):
    value = dict(
        id=record_id,
        topic_id=record_id,
        scope=scope.model_dump(mode="json"),
        created_at=NOW.isoformat(),
        updated_at=NOW.isoformat(),
        version=1,
        participant_ids=["synthetic-person"],
        status="open",
    )
    value.update(changes)
    return decode(Topic, value)


def command(request_id="request-01", scope=SCOPE, *, body="Вымышленная команда."):
    return MessageCommand(
        request_id=request_id,
        scope=scope,
        target="synthetic-target",
        type="dialogue.message",
        arguments=MessageArguments(text=body),
    )


async def save(store, record, principal=MEMORY, request_id="request-01", **kwargs):
    incoming = command(request_id, record.scope)
    async with store.transaction(principal, record.scope, incoming) as unit:
        if unit.replay is not None:
            return unit.replay
        await unit.put(record, **kwargs)
        receipt = Receipt(
            request_id=request_id,
            scope=record.scope,
            status="completed",
            resource_id=record.id,
            version=getattr(record, "version", 1),
        )
        await unit.complete(receipt)
        return receipt


async def new_store(tmp_path, *, grants=GRANTS, scope=SCOPE, clock=lambda: NOW):
    # Linux probe redirects every mutable SQLite file to its own named volume.
    base = Path(os.environ.get("M1_02_DB_DIR", str(tmp_path)))
    base.mkdir(parents=True, exist_ok=True)
    from uuid import uuid4

    store = SQLiteStore(base / (str(uuid4()) + ".sqlite"), clock=clock)
    await store.upgrade(MIGRATIONS)
    await store.provision(scope, grants)
    return store


async def counts(store):
    async with store.connection() as connection:
        return tuple(
            [
                await connection.scalar(select(func.count()).select_from(table))
                for table in (db.records, db.revisions, db.operations, db.outbox)
            ]
        )


def run(coroutine):
    return asyncio.run(coroutine)


def test_empty_migration_single_head_pragmas(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await store.upgrade(MIGRATIONS)
            await store.check_schema()
            async with store.connection() as connection:
                for name, expected in (
                    ("journal_mode", "wal"),
                    ("synchronous", 2),
                    ("busy_timeout", 5000),
                    ("foreign_keys", 1),
                ):
                    assert await connection.scalar(text("PRAGMA " + name)) == expected
                assert await connection.scalar(text("PRAGMA integrity_check")) == "ok"
                assert (await connection.execute(text("PRAGMA foreign_key_check"))).all() == []
            config = Config()
            config.set_main_option("script_location", str(MIGRATIONS))
            assert ScriptDirectory.from_config(config).get_heads() == ["0003"]
        finally:
            await store.close()

    run(scenario())


def test_previous_migration_preserves_scope_grant(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "previous.sqlite")
        try:
            await store.upgrade(MIGRATIONS, revision="0001")
            with pytest.raises(ContractError) as error:
                await store.check_schema()
            assert error.value.error.code == ErrorCode.SERVICE_UNAVAILABLE
            async with store.connection(write=True) as connection:
                await connection.execute(
                    insert(db.scopes).values(
                        scope_id=scope_key(SCOPE),
                        **dict(
                            zip(
                                ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
                                SCOPE.partition,
                                strict=True,
                            )
                        ),
                    )
                )
                await connection.execute(
                    insert(db.grants).values(
                        scope_id=scope_key(SCOPE),
                        principal_id=MEMORY.id,
                        owner=Owner.MEMORY.value,
                        role=MEMORY.role,
                        actor_owner=Owner.MEMORY.value,
                        read=True,
                        write=True,
                        api_transfer=False,
                        public_disclosure=False,
                        expires_at=None,
                    )
                )
            await store.upgrade(MIGRATIONS)
            await store.check_schema()
            async with store.connection() as connection:
                assert await connection.scalar(select(func.count()).select_from(db.scopes)) == 1
                assert await connection.scalar(select(func.count()).select_from(db.grants)) == 1
            await save(store, source())
        finally:
            await store.close()

    run(scenario())


def test_missing_and_unknown_revision_rejected(tmp_path):
    async def scenario():
        store = SQLiteStore(tmp_path / "missing.sqlite")
        try:
            with pytest.raises(ContractError) as error:
                await store.check_schema()
            assert error.value.error.code == ErrorCode.SERVICE_UNAVAILABLE
            await store.upgrade(MIGRATIONS)
            async with store.connection(write=True) as connection:
                await connection.execute(text("UPDATE alembic_version SET version_num='unknown'"))
            with pytest.raises(ContractError):
                await store.check_schema()
        finally:
            await store.close()

    run(scenario())


def test_atomic_version_receipt_outbox_and_restart(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        original = source()
        receipt = await save(store, original)
        assert await counts(store) == (1, 1, 1, 1)
        await store.engine.dispose()  # Reopen connection after durable commit, no in-memory dedup.
        assert await save(store, original) == receipt
        assert await counts(store) == (1, 1, 1, 1)
        second = source(version=2, content="Вымышленная исправленная редакция.")
        await save(store, second, request_id="edit-01", expected_version=1)
        assert await counts(store) == (1, 2, 2, 2)
        repository = store.repository(Source, MEMORY, SCOPE)
        assert await repository.get(original.id) == second
        assert await repository.get(original.id, version=1) == original
        await store.close()

    run(scenario())


@pytest.mark.parametrize("failure", ["exception", "no-receipt", "wrong-receipt"])
def test_rollback_state_receipt_outbox(tmp_path, failure):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            with pytest.raises((ContractError, RuntimeError)):
                async with store.transaction(MEMORY, SCOPE, command()) as unit:
                    await unit.put(source())
                    if failure == "exception":
                        await unit.complete(
                            Receipt(
                                request_id="request-01",
                                scope=SCOPE,
                                status="completed",
                                resource_id="source-01",
                                version=1,
                            )
                        )
                        raise RuntimeError("SYNTHETIC_CRASH_BEFORE_COMMIT")
                    if failure == "wrong-receipt":
                        await unit.complete(
                            Receipt(
                                request_id="wrong-request",
                                scope=SCOPE,
                                status="completed",
                                resource_id="source-01",
                                version=1,
                            )
                        )
            assert await counts(store) == (0, 0, 0, 0)
        finally:
            await store.close()

    run(scenario())


def test_concurrent_cas_one_winner(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source())

            async def contender(index):
                try:
                    await save(
                        store,
                        source(version=2, content=str(index)),
                        request_id="concurrent-" + str(index),
                        expected_version=1,
                    )
                    return "committed"
                except ContractError as error:
                    assert error.error.code == ErrorCode.VERSION_CONFLICT
                    assert error.error.current_version == 2
                    return "conflict"

            assert sorted(await asyncio.gather(contender(1), contender(2))) == [
                "committed",
                "conflict",
            ]
            assert await counts(store) == (1, 2, 2, 2)
        finally:
            await store.close()

    run(scenario())


def test_duplicate_concurrent_command_one_effect(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            first, second = await asyncio.gather(save(store, source()), save(store, source()))
            assert first == second
            assert await counts(store) == (1, 1, 1, 1)
        finally:
            await store.close()

    run(scenario())


def test_request_conflict_and_revoked_replay(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source())
            with pytest.raises(ContractError) as error:
                async with store.transaction(MEMORY, SCOPE, command(body="Другой текст.")):
                    pass
            assert error.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
            await store.provision(SCOPE, ())
            with pytest.raises(ContractError) as error:
                await save(store, source())
            assert error.value.error.code == ErrorCode.ACCESS_DENIED
            assert await store.repository(Source, MEMORY, SCOPE).get("source-01") is None
            assert await counts(store) == (1, 1, 1, 1)
        finally:
            await store.close()

    run(scenario())


@pytest.mark.parametrize(
    "scope_changes",
    [
        {"workspace_id": "other-workspace"},
        {"persona_id": "other-persona"},
        {"memory_space_id": "other-space"},
        {"memory_mode": "work"},
    ],
)
def test_partition_isolation_same_id_and_request(tmp_path, scope_changes):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source())
            other = SCOPE.model_copy(update=scope_changes)
            assert await store.repository(Source, MEMORY, other).get("source-01") is None
            await store.provision(other, GRANTS)
            await save(store, source(scope=other, content="Другие синтетические данные."))
            assert (await store.repository(Source, MEMORY, SCOPE).get("source-01")).content == (
                "Вымышленный исходный текст."
            )
            assert (await store.repository(Source, MEMORY, other).get("source-01")).content == (
                "Другие синтетические данные."
            )
            assert await counts(store) == (2, 2, 2, 2)
        finally:
            await store.close()

    run(scenario())


def test_restart_scope_preserves_biography_not_request_context(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source())
            new_run = SCOPE.model_copy(update={"agent_id": "agent-new", "run_id": "run-new"})
            assert await store.repository(Source, MEMORY, new_run).get("source-01") == source()
            with pytest.raises(ContractError) as error:
                async with store.transaction(MEMORY, new_run, command(scope=new_run)):
                    pass
            assert error.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT
        finally:
            await store.close()

    run(scenario())


@pytest.mark.parametrize(
    "principal",
    [
        Principal("worker", "worker", Owner.MEMORY),
        OPERATOR,
        SOCIAL,
    ],
)
def test_worker_operator_foreign_owner_cannot_write(tmp_path, principal):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            with pytest.raises(ContractError) as error:
                await save(store, source(), principal)
            assert error.value.error.code == ErrorCode.ACCESS_DENIED
            assert await counts(store) == (0, 0, 0, 0)
            if principal.role == "worker":
                with pytest.raises(ContractError):
                    store.repository(Source, principal, SCOPE)
        finally:
            await store.close()

    run(scenario())


def test_acl_purpose_before_limit_and_revocation(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source("a-hidden"))
            await save(
                store, source("b-private"), request_id="b", access=(RecordAccess(OPERATOR.id),)
            )
            await save(
                store,
                source("c-api", api_transfer_allowed=True),
                request_id="c",
                access=(RecordAccess(OPERATOR.id, api_transfer=True),),
            )
            repository = store.repository(Source, OPERATOR, SCOPE)
            assert await repository.get("a-hidden") is None
            assert [record.id for record in await repository.list(limit=1)] == ["b-private"]
            assert [
                record.id for record in await repository.list(limit=1, purpose="api_transfer")
            ] == ["c-api"]
            assert await repository.list(purpose="public_disclosure") == []
            await save(
                store,
                source("c-api", version=2),
                request_id="c-revoke",
                expected_version=1,
                access=(),
            )
            assert await repository.get("c-api", version=1) is None
            assert [record.id for record in await repository.list(after_id="b-private")] == []
        finally:
            await store.close()

    run(scenario())


@pytest.mark.parametrize(
    "field", ["read_allowed", "api_transfer_allowed", "public_disclosure_allowed"]
)
def test_current_source_rights_guard_history(tmp_path, field):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            original = source(public_disclosure_allowed=True)
            await save(store, original)
            changed = (
                source(version=2, public_disclosure_allowed=True, **{field: False})
                if (field != "public_disclosure_allowed")
                else source(version=2, public_disclosure_allowed=False)
            )
            await save(store, changed, request_id="deny", expected_version=1)
            purpose = {
                "read_allowed": "private",
                "api_transfer_allowed": "api_transfer",
                "public_disclosure_allowed": "public_disclosure",
            }[field]
            assert (
                await store.repository(Source, MEMORY, SCOPE).get(
                    original.id, version=1, purpose=purpose
                )
                is None
            )
        finally:
            await store.close()

    run(scenario())


def test_expiry_at_transaction_end_and_read(tmp_path):
    async def scenario():
        now = [NOW]
        store = await new_store(
            tmp_path,
            clock=lambda: now[0],
            grants=(replace(GRANTS[0], expires_at=NOW + timedelta(seconds=1)),),
        )
        try:
            with pytest.raises(ContractError) as error:
                async with store.transaction(MEMORY, SCOPE, command()) as unit:
                    await unit.put(source())
                    await unit.complete(
                        Receipt(
                            request_id="request-01",
                            scope=SCOPE,
                            status="completed",
                            resource_id="source-01",
                            version=1,
                        )
                    )
                    now[0] += timedelta(seconds=1)
            assert error.value.error.code == ErrorCode.ACCESS_DENIED
            assert await counts(store) == (0, 0, 0, 0)
            await store.provision(SCOPE, GRANTS)
            await save(store, source())
            now[0] = NOW + timedelta(days=30)
            assert await store.repository(Source, MEMORY, SCOPE).get("source-01") is None
        finally:
            await store.close()

    run(scenario())


def test_source_links_rights_versions_and_tombstone(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        ref = SourceRef(source_id="source-01", version=1, scope=SCOPE, availability="available")
        claim = Claim(
            id="claim-01",
            scope=SCOPE,
            created_at=NOW,
            updated_at=NOW,
            version=1,
            subject_id="synthetic-subject",
            content="Вымышленное утверждение.",
            knowledge_type="observed",
            sources=[ref],
            confidence=0.8,
        )
        try:
            with pytest.raises(ContractError):
                await save(store, claim)
            await save(store, source())
            await save(store, claim, request_id="claim")
            repository = store.repository(Claim, MEMORY, SCOPE)
            assert await repository.get(claim.id) == claim
            await save(
                store,
                source(version=2, content=None, availability="deleted"),
                request_id="delete",
                expected_version=1,
            )
            assert await repository.get(claim.id, version=1) is None
            assert await store.repository(Source, MEMORY, SCOPE).get("source-01", version=1) is None
            with pytest.raises(ContractError):
                await save(store, source(version=3), request_id="resurrect", expected_version=2)
            async with store.connection() as connection:
                payloads = (
                    (
                        await connection.execute(
                            select(db.revisions.c.payload).where(db.revisions.c.kind == "Source")
                        )
                    )
                    .scalars()
                    .all()
                )
                assert len(payloads) == 1 and "Вымышленный исходный текст." not in payloads[0]
        finally:
            await store.close()

    run(scenario())


def test_semantic_source_uniqueness_transaction_rolls_back(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source(external_id="external-event"))
            with pytest.raises(ContractError):
                await save(
                    store,
                    source("second-source", external_id="external-event"),
                    request_id="duplicate",
                )
            assert await counts(store) == (1, 1, 1, 1)
            await save(
                store,
                source(
                    "third-source", external_id="external-event", adapter_id="different-adapter"
                ),
                request_id="different",
            )
            assert await counts(store) == (2, 2, 2, 2)
        finally:
            await store.close()

    run(scenario())


def test_people_confirmed_account_unique_nickname_not_key(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source(), access=(RecordAccess(SOCIAL.id),))
            ref = SourceRef(source_id="source-01", version=1, scope=SCOPE, availability="available")

            def person(record_id, account_id, confirmation="confirmed"):
                return Person(
                    id=record_id,
                    person_id=record_id,
                    scope=SCOPE,
                    created_at=NOW,
                    updated_at=NOW,
                    version=1,
                    accounts=[
                        AccountLink(
                            platform_id="synthetic-platform",
                            account_id=account_id,
                            display_name="Одинаковый вымышленный ник.",
                            confirmation=confirmation,
                            evidence=[ref],
                        )
                    ],
                )

            await save(store, person("person-1", "account-1"), SOCIAL, "person-1")
            await save(store, person("person-2", "account-2"), SOCIAL, "person-2")
            with pytest.raises(ContractError):
                await save(store, person("person-3", "account-1"), SOCIAL, "person-3")
            await save(
                store, person("person-hypothesis", "account-1", "hypothesis"), SOCIAL, "hypothesis"
            )
            assert len(await store.repository(Person, SOCIAL, SCOPE).list()) == 3
        finally:
            await store.close()

    run(scenario())


@pytest.mark.parametrize("limit", [0, 201, True])
def test_read_limit_bounded(tmp_path, limit):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            with pytest.raises(ContractError):
                await store.repository(Source, MEMORY, SCOPE).list(limit=limit)
        finally:
            await store.close()

    run(scenario())


def test_cross_scope_record_transaction_rejected(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            other = SCOPE.model_copy(update={"persona_id": "other-persona"})
            with pytest.raises(ContractError) as error:
                async with store.transaction(MEMORY, SCOPE, command()) as unit:
                    await unit.put(source(scope=other))
            assert error.value.error.code == ErrorCode.ACCESS_DENIED
            assert await counts(store) == (0, 0, 0, 0)
        finally:
            await store.close()

    run(scenario())


def test_exact_promise_survives_raw_source_ttl(tmp_path):
    from ai_agent_contracts.memory import Promise

    async def scenario():
        now = [NOW]
        store = await new_store(tmp_path, clock=lambda: now[0])
        try:
            await save(store, source(), access=(RecordAccess(SOCIAL.id),))
            ref = SourceRef(source_id="source-01", version=1, scope=SCOPE, availability="available")
            promise = Promise(
                id="promise-01",
                scope=SCOPE,
                created_at=NOW,
                updated_at=NOW,
                version=1,
                participant_ids=["synthetic-person"],
                topic_id="topic-synthetic",
                delivered_utterance_id="utterance-synthetic",
                delivery_id="delivery-synthetic",
                sources=[ref],
                content="Синтетическое обещание.",
                completion_criterion="Проверяемый синтетический результат.",
                status="open",
            )
            await save(store, promise, SOCIAL, "promise")
            now[0] += timedelta(days=30)
            assert await store.repository(Source, MEMORY, SCOPE).get("source-01") is None
            repository = store.repository(Promise, SOCIAL, SCOPE)
            retained = await repository.get(promise.id)
            assert retained.status == "open" and retained.content == promise.content
            assert retained.sources[0].availability == "expired"
            assert await repository.get(promise.id, purpose="api_transfer") is None
            # Explicit removal remains distinct from TTL and fulfilment.
            await save(
                store,
                source(version=2, availability="deleted", content=None),
                request_id="delete",
                expected_version=1,
            )
            assert await repository.get(promise.id) is None
            async with store.connection() as connection:
                payload = await connection.scalar(
                    select(db.revisions.c.payload).where(db.revisions.c.kind == "Promise")
                )
                assert json.loads(payload)["status"] == "open"
        finally:
            await store.close()

    run(scenario())


def test_current_source_acl_guards_linked_records_before_limit(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source("a-source"))
            await save(store, source("b-source"), request_id="b", access=(RecordAccess(SOCIAL.id),))
            for prefix in ("a", "b"):
                ref = SourceRef(
                    source_id=prefix + "-source", version=1, scope=SCOPE, availability="available"
                )
                if prefix == "a":
                    with pytest.raises(ContractError):
                        await save(
                            store,
                            topic(prefix + "-topic", sources=[ref.model_dump(mode="json")]),
                            SOCIAL,
                            "topic-" + prefix,
                        )
                else:
                    await save(
                        store,
                        topic(prefix + "-topic", sources=[ref.model_dump(mode="json")]),
                        SOCIAL,
                        "topic-" + prefix,
                    )
            assert [
                item.id for item in await store.repository(Topic, SOCIAL, SCOPE).list(limit=1)
            ] == ["b-topic"]
            await save(
                store,
                source("b-source", version=2),
                request_id="revoke",
                expected_version=1,
                access=(),
            )
            assert await store.repository(Topic, SOCIAL, SCOPE).list() == []
        finally:
            await store.close()

    run(scenario())


def test_one_active_workspace_across_persona_and_test_partitions(tmp_path):
    from ai_agent_contracts.control import RunSession
    from ai_agent_contracts.states import ActivityState, RunState

    supervisor = Principal("supervisor-synthetic", "core_owner", Owner.SUPERVISOR)
    grant = Grant(supervisor, Owner.SUPERVISOR, read=True, write=True)

    async def scenario():
        store = await new_store(tmp_path)
        first_scope = SCOPE.model_copy(
            update={
                "agent_id": "agent-1",
                "run_id": "run-1",
                "session_id": "session-1",
            }
        )
        second_scope = first_scope.model_copy(
            update={
                "persona_id": "persona-2",
                "memory_mode": "work",
                "agent_id": "agent-2",
                "run_id": "run-2",
                "session_id": "session-2",
            }
        )

        def session(record_id, scope, state=RunState.STARTING, version=1):
            return RunSession(
                id=record_id,
                scope=scope,
                created_at=NOW,
                updated_at=NOW,
                version=version,
                agent_id=scope.agent_id,
                run_id=scope.run_id,
                session_id=scope.session_id,
                state=state,
                autonomy="observation",
                activity=ActivityState.OPEN,
                voice_enabled=False,
                gm_paused=True,
                control_epoch=0,
            )

        try:
            await store.provision(first_scope, (grant,))
            await store.provision(second_scope, (grant,))
            await save(store, session("first", first_scope), supervisor, "first")
            with pytest.raises(ContractError):
                await save(store, session("second", second_scope), supervisor, "second")
            await save(
                store,
                session("first", first_scope, RunState.STOPPED, 2),
                supervisor,
                "stop-fixture",
                expected_version=1,
            )
            await save(store, session("second", second_scope), supervisor, "second")
            assert await counts(store) == (2, 3, 3, 3)
        finally:
            await store.close()

    run(scenario())


def test_immutable_execution_receipt_and_exact_attempt_uniqueness(tmp_path):
    from ai_agent_contracts.control import ExecutionResult, Freshness

    owner = Principal("jobs-owner", "core_owner", Owner.JOBS)
    scope = SCOPE.model_copy(update={"agent_id": "agent-1", "run_id": "run-1"})

    async def scenario():
        store = await new_store(
            tmp_path, scope=scope, grants=(Grant(owner, Owner.JOBS, read=True, write=True),)
        )
        try:
            result = ExecutionResult(
                id="result-1",
                scope=scope,
                created_at=NOW,
                type="execution.result",
                command_id="command-1",
                operation_id="operation-1",
                attempt_id="attempt-1",
                lease_version=1,
                freshness=Freshness(
                    run_id="run-1", control_epoch=1, profile_version=1, policy_version=1
                ),
                status="SUCCEEDED",
                completed_at=NOW,
                evidence=[],
            )
            await save(store, result, owner)
            with pytest.raises(ContractError):
                await save(
                    store,
                    result.model_copy(update={"id": "duplicate-result"}),
                    owner,
                    "duplicate-attempt",
                )
            with pytest.raises(ContractError):
                await save(store, result, owner, "overwrite", expected_version=1)
            assert await counts(store) == (1, 1, 1, 1)
        finally:
            await store.close()

    run(scenario())


def test_extension_schema_and_dynamic_state_change_owner(tmp_path):
    from ai_agent_contracts.common import Contract, ExtensionPayload
    from ai_agent_contracts.extensions import ExtensionRegistry
    from ai_agent_contracts.persona import StateChange
    from ai_agent_contracts.states import ChangeState

    class Patch(Contract):
        note: str

    async def scenario():
        store = await new_store(tmp_path)
        registry = ExtensionRegistry()
        registry.register("synthetic-patch", Patch)
        store.extensions = registry
        try:
            await save(store, source())
            change = from_wire(
                TypeAdapter(StateChange),
                json.dumps(
                    dict(
                        id="change-1",
                        scope=SCOPE.model_dump(mode="json"),
                        created_at=NOW.isoformat(),
                        updated_at=NOW.isoformat(),
                        version=1,
                        proposal_id="proposal-1",
                        owner="memory",
                        base_version=1,
                        patch={
                            "schema_id": "synthetic-patch",
                            "data": {"note": "Синтетическая правка."},
                        },
                        evidence=[
                            dict(
                                source_id="source-01",
                                version=1,
                                scope=SCOPE.model_dump(mode="json"),
                                availability="available",
                            )
                        ],
                        interpretation="Синтетическая интерпретация.",
                        uncertainty=0.2,
                        expected_manifestation="Синтетическое ожидание.",
                        status=ChangeState.PROPOSED,
                    )
                ).encode(),
                extensions=registry,
            )
            assert isinstance(change.patch, ExtensionPayload)
            await save(store, change, request_id="change")
            assert await store.repository(StateChange, MEMORY, SCOPE).get(change.id) == change
            store.extensions = None
            with pytest.raises(ContractError):
                await store.repository(StateChange, MEMORY, SCOPE).get(change.id)
        finally:
            await store.close()

    run(scenario())


def test_sql_constraint_bad_json_and_worker_volume_separation(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source())
            with pytest.raises(ContractError):
                async with store.connection(write=True) as connection:
                    await connection.execute(text("UPDATE record_revisions SET payload='bad-json'"))
            assert (await store.repository(Source, MEMORY, SCOPE).get("source-01")) == source()
        finally:
            await store.close()

    run(scenario())
    compose = (ROOT / "deploy/compose.yaml").read_text(encoding="utf-8")
    worker = compose.split("  worker:", 1)[1].split("  redis:", 1)[0]
    assert "core-data:" not in worker
    assert "worker-cache:" in worker


def test_caught_write_failure_cannot_commit_partial_records(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            await save(store, source(external_id="external-1"))
            with pytest.raises(ContractError):
                async with store.transaction(MEMORY, SCOPE, command("mixed")) as unit:
                    await unit.put(source("new-valid", external_id="external-2"))
                    with pytest.raises(ContractError):
                        await unit.put(source("new-invalid", external_id="external-1"))
                    # Catching an integrity failure must not permit committing its partial row.
                    await unit.complete(
                        Receipt(
                            request_id="mixed",
                            scope=SCOPE,
                            status="completed",
                            resource_id="new-valid",
                            version=1,
                        )
                    )
            assert await counts(store) == (1, 1, 1, 1)
        finally:
            await store.close()

    run(scenario())


def test_pending_transaction_cancelled_rolls_back(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        started = asyncio.Event()
        wait = asyncio.Event()

        async def writer():
            async with store.transaction(MEMORY, SCOPE, command()) as unit:
                await unit.put(source())
                started.set()
                await wait.wait()

        try:
            task = asyncio.create_task(writer())
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await counts(store) == (0, 0, 0, 0)
        finally:
            await store.close()

    run(scenario())


def test_expired_receipt_never_reexecutes_and_outbox_has_only_metadata(tmp_path):
    async def scenario():
        now = [NOW]
        store = await new_store(tmp_path, clock=lambda: now[0])
        try:
            await save(store, source())
            async with store.connection() as connection:
                event = (await connection.execute(select(db.outbox))).mappings().one()
                assert event["operation_id"] == "request-01"
                assert event["causal_id"] == "request-01"
                assert from_wire(TypeAdapter(Scope), event["scope"].encode()) == SCOPE
                assert "Вымышленный исходный текст." not in str(dict(event))
            now[0] += timedelta(days=30)
            with pytest.raises(ContractError) as error:
                await save(store, source())
            assert error.value.error.code == ErrorCode.OUTCOME_UNKNOWN
            assert await counts(store) == (1, 1, 1, 1)
            async with store.connection(write=True) as connection:
                await connection.execute(text("UPDATE processed_operations SET receipt=NULL"))
            with pytest.raises(ContractError) as redacted:
                await save(store, source())
            assert redacted.value.error.code == ErrorCode.OUTCOME_UNKNOWN
        finally:
            await store.close()

    run(scenario())


def test_registered_extension_source_scope_and_access_are_checked(tmp_path):
    from ai_agent_contracts.common import Contract
    from ai_agent_contracts.extensions import ExtensionRegistry
    from ai_agent_contracts.memory import Relationship

    class WithSource(Contract):
        source: SourceRef

    async def scenario():
        now = [NOW]
        store = await new_store(tmp_path, clock=lambda: now[0])
        registry = ExtensionRegistry()
        registry.register("synthetic-with-source", WithSource)
        store.extensions = registry
        try:
            await save(store, source(), access=(RecordAccess(SOCIAL.id),))

            def relationship(scope):
                return from_wire(
                    TypeAdapter(Relationship),
                    json.dumps(
                        dict(
                            id="relation-1",
                            scope=SCOPE.model_dump(mode="json"),
                            created_at=NOW.isoformat(),
                            updated_at=NOW.isoformat(),
                            version=1,
                            person_id="person-synthetic",
                            grounds=[],
                            aspects=dict(
                                schema_id="synthetic-with-source",
                                data=dict(
                                    source=dict(
                                        source_id="source-01",
                                        version=1,
                                        scope=scope.model_dump(mode="json"),
                                        availability="available",
                                    )
                                ),
                            ),
                        )
                    ).encode(),
                    extensions=registry,
                )

            other = SCOPE.model_copy(update={"persona_id": "other-persona"})
            with pytest.raises(ContractError) as error:
                await save(store, relationship(other), SOCIAL, "wrong-extension")
            assert error.value.error.code == ErrorCode.ACCESS_DENIED
            valid = relationship(SCOPE)
            await save(store, valid, SOCIAL, "valid-extension")
            assert await store.repository(Relationship, SOCIAL, SCOPE).get(valid.id) == valid
            now[0] += timedelta(days=30)
            retained = await store.repository(Relationship, SOCIAL, SCOPE).get(valid.id)
            assert retained.aspects.data["source"]["availability"] == "expired"
            await save(
                store, source(version=2), request_id="revoke-source", expected_version=1, access=()
            )
            assert await store.repository(Relationship, SOCIAL, SCOPE).get(valid.id) is None
        finally:
            await store.close()

    run(scenario())


def test_recent_creation_order_keeps_authoritative_filters_and_cutoff(tmp_path):
    async def scenario():
        store = await new_store(tmp_path)
        try:
            for i in range(5):
                await save(
                    store,
                    source(
                        f"row-{4 - i}",
                        created_at=(NOW + timedelta(seconds=i)).isoformat(),
                        updated_at=(NOW + timedelta(seconds=i)).isoformat(),
                        api_transfer_allowed=i != 3,
                    ),
                    request_id=f"save-{i}",
                    access=(RecordAccess(OPERATOR.id, api_transfer=True),),
                )
            repo = store.repository(Source, OPERATOR, SCOPE)
            assert [r.id for r in await repo.recent(limit=2, purpose="api_transfer")] == [
                "row-0",
                "row-2",
            ]
            assert [
                r.id
                for r in await repo.recent(
                    limit=2, before=NOW + timedelta(seconds=2), purpose="api_transfer"
                )
            ] == ["row-2", "row-3"]
            assert len(await repo.recent(limit=5)) == 5
            with pytest.raises(ContractError):
                await repo.recent(limit=201)
        finally:
            await store.close()

    asyncio.run(scenario())
