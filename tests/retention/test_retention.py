"""Retention on real SQLite (M5-01): terms in virtual time, deletion, restore gate, metadata."""

import shutil
from datetime import timedelta
from decimal import Decimal

import pytest
from ai_agent_application.memory import ClaimDraft
from ai_agent_application.retention import RetentionPolicy
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import SourceRef
from ai_agent_contracts.control import OperationProjection
from ai_agent_contracts.states import ErrorCode
from ai_agent_contracts.workspace import DeletionTombstone
from retention_support import (
    JOBS,
    LLM,
    MEMORY,
    NOW,
    command,
    make_scope,
    new_world,
    residue,
    rid,
    run,
)
from usage_support import World as UsageWorld
from usage_support import report as attempt_report

A = make_scope("persona-a")
B = make_scope("persona-b")
A_TEST = make_scope("persona-a", "test")
SECRET = "Пурпурный-шифр-ЛиловыйМаяк"


def ref(source, scope=A):
    return SourceRef(
        source_id=source.id, version=source.version, scope=scope, availability="available"
    )


async def stored(world, scope, source_id):
    """What the physical store sees (also TTL-hidden rows)."""
    return await world.physical.source(scope, source_id)


async def tombstones(world, scope):
    return await world.store.repository(DeletionTombstone, MEMORY, scope).list(limit=100)


async def claim(world, scope, claim_id, sources, text="Он живёт у озера"):
    refs = []
    for item in sources:
        current = await world.memory.get_source(scope, item.source_id)
        refs.append(ref(current, scope))
    await world.memory.record_claim(
        command(scope),
        ClaimDraft(claim_id, "subject-1", text, "reported", tuple(refs), 0.5),
    )


async def revisions(world, record_id):
    """Physical revisions of a record, also when storage hides it from its reader."""
    from ai_agent_adapters.storage import schema as db
    from sqlalchemy import func, select

    async with world.store.connection() as connection:
        return await connection.scalar(
            select(func.count()).select_from(db.revisions).where(db.revisions.c.id == record_id)
        )


async def hits(world, scope, query):
    return (await world.ask(scope, query)).fragments


# ---- terms in virtual time ----------------------------------------------------------------


def test_raw_text_leaves_at_thirty_days_and_not_a_day_earlier(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, f"Секретная фраза {SECRET} для проверки.", external_id="m-1")
        assert residue(world.paths, SECRET)  # the detector sees the text before removal
        # registered one day old: it expires 29 days from now
        world.clock.advance(days=28, hours=23)
        early = await world.retention.sweep(A)
        before = await stored(world, A, made.source_id)
        world.clock.advance(hours=2)
        late = await world.retention.sweep(A)
        after = await stored(world, A, made.source_id)
        entries = await world.ledger.entries(A)
        return early, before, late, after, entries, made, world

    early, before, late, after, entries, made, world = run(scenario())
    assert early.expired == () and before.has_content and before.availability == "available"
    assert late.expired == (made.source_id,)
    assert after.availability == "expired" and not after.has_content
    assert [(e.source_id, e.reason) for e in entries] == [(made.source_id, "expired")]
    assert entries[0].purged_at is not None
    assert residue(world.paths, SECRET) == []
    run(world.store.close())


def test_expired_text_is_not_recalled_and_leaves_the_index(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, f"Маяк горит на мысу {SECRET}.", external_id="m-1")
        assert await hits(world, A, "Маяк горит")
        world.clock.advance(days=40)
        await world.retention.sweep(A)
        return await hits(world, A, "Маяк горит"), await world.index.inventory(A), made

    found, inventory, made = run(scenario())
    assert found == [] and made.source_id not in inventory


def test_protected_source_is_kept_but_can_still_be_deleted_on_request(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        made = await world.add(A, f"Защищённая запись {SECRET}.", protected=True)
        world.clock.advance(days=400)
        report = await world.retention.sweep(A)
        kept = await stored(world, A, made.source_id)
        preview = await world.retention.preview_delete(A, [made.source_id])
        done = await world.retention.delete(A, preview)
        gone = await stored(world, A, made.source_id)
        return report, kept, done, gone, world

    report, kept, done, gone, world = run(scenario())
    assert report.expired == () and kept.has_content
    assert done.complete and not gone.has_content and gone.availability == "deleted"
    assert residue(world.paths, SECRET) == []


def test_a_source_without_a_durable_trace_is_reported_not_extended(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        lone = await world.add(A, "Одинокая реплика без выводов.", external_id="m-1")
        kept = await world.add(A, "Реплика, из которой сделан вывод.", external_id="m-2")
        await claim(world, A, "claim-1", [kept])
        world.clock.advance(days=40)
        return await world.retention.sweep(A), lone, kept

    report, lone, kept = run(scenario())
    assert set(report.expired) == {lone.source_id, kept.source_id}  # the term is never extended
    assert report.unconsolidated == (lone.source_id,)  # the gap is visible


def test_test_scope_inactivity_limit_can_be_shorter_than_the_registered_term(tmp_path):
    async def scenario():
        policy = RetentionPolicy(test_source=timedelta(days=7))
        world = await new_world(tmp_path, [A, A_TEST], policy=policy)
        work = await world.add(A, "Рабочая запись для сравнения.", external_id="w-1")
        test = await world.add(A_TEST, f"Тестовая запись {SECRET}.", external_id="t-1")
        world.clock.advance(days=8)  # registered term is 29 days: only the test limit applies
        return (
            await world.retention.sweep(A_TEST),
            await world.retention.sweep(A),
            await stored(world, A, work.source_id),
            await stored(world, A_TEST, test.source_id),
            test,
        )

    test_report, work_report, work, test, made = run(scenario())
    assert test_report.expired == (made.source_id,) and not test.has_content
    assert work_report.expired == () and work.has_content  # work memory has no test limit


def test_policy_must_keep_aggregates_longer_than_attempt_metadata():
    with pytest.raises(ContractError):
        RetentionPolicy(llm_metadata=timedelta(days=90), aggregate=timedelta(days=30))
    with pytest.raises(ContractError):
        RetentionPolicy(conversation=timedelta(0))
    assert RetentionPolicy().aggregate == timedelta(days=365)


# ---- deletion ----------------------------------------------------------------------------


async def deletion_world(tmp_path):
    world = await new_world(tmp_path, [A, B])
    doomed = await world.add(A, f"Он сказал: {SECRET}, вот так.", external_id="d-1")
    shared = await world.add(A, "Вторая беседа о погоде на озере.", external_id="d-2")
    other = await world.add(B, f"Чужая биография {SECRET} в другой области.", external_id="b-1")
    await claim(world, A, "claim-sole", [await world.memory.get_source(A, doomed.source_id)])
    both = [
        await world.memory.get_source(A, doomed.source_id),
        await world.memory.get_source(A, shared.source_id),
    ]
    await claim(world, A, "claim-shared", both, text="Погода на озере изменчива")
    return world, doomed, shared, other


def test_preview_names_the_exact_area_and_goes_stale_when_it_changes(tmp_path):
    async def scenario():
        world, doomed, shared, _ = await deletion_world(tmp_path)
        preview = await world.retention.preview_delete(A, [doomed.source_id])
        await claim(
            world,
            A,
            "claim-late",
            [await world.memory.get_source(A, doomed.source_id)],
            text="Позднее дополнение",
        )
        with pytest.raises(ContractError) as info:
            await world.retention.delete(A, preview)
        return preview, info.value.error.code, doomed

    preview, code, doomed = run(scenario())
    assert preview.source_ids == (doomed.source_id,)
    assert preview.erased_with == (("Claim", "claim-sole"),)
    assert preview.detached == (("Claim", "claim-shared"),)
    assert preview.indexed == (doomed.source_id,)
    assert code is ErrorCode.VERSION_CONFLICT  # a changed area is a new decision


def test_delete_blocks_first_then_removes_text_everywhere_and_keeps_other_scopes(tmp_path):
    async def scenario():
        world, doomed, shared, other = await deletion_world(tmp_path)
        preview = await world.retention.preview_delete(A, [doomed.source_id])
        report = await world.retention.delete(A, preview)
        claims = world.store.repository(
            __import__("ai_agent_contracts.memory", fromlist=["Claim"]).Claim, MEMORY, A
        )
        return world, doomed, shared, other, report, claims

    world, doomed, shared, other, report, claims = run(scenario())

    async def after():
        return (
            [h.source.source_id for h in await hits(world, A, SECRET)],
            await hits(world, B, "Чужая биография"),
            await claims.get("claim-sole"),
            await revisions(world, "claim-shared"),
            await stored(world, A, doomed.source_id),
            await stored(world, A, shared.source_id),
            await world.ledger.entries(A),
            await tombstones(world, A),
        )

    found, foreign, sole, shared_claim, gone, kept, ledger, marks = run(after())
    assert report.complete and report.erased == (("Claim", "claim-sole"),)
    assert doomed.source_id not in found and foreign  # another persona's memory is untouched
    assert sole is None and shared_claim == 1  # only the sole-basis claim is erased
    assert gone.availability == "deleted" and not gone.has_content and kept.has_content
    assert [(e.source_id, e.reason) for e in ledger] == [(doomed.source_id, "operator_delete")]
    assert ledger[0].purged_at is not None
    assert sorted(bool(m.purged_at) for m in marks) == [False, True]  # blocked, then purged
    # the other scope's copy of the same words stays: physical files still hold *its* text,
    # so check the removed scope's own text via an exact phrase
    assert residue(world.paths, f"Он сказал: {SECRET}") == []
    assert residue(world.paths, "Позднее дополнение") == []
    run(world.store.close())


def test_deleted_identity_cannot_be_imported_again(tmp_path):
    async def scenario():
        world, doomed, *_ = await deletion_world(tmp_path)
        await world.retention.delete(A, await world.retention.preview_delete(A, [doomed.source_id]))
        with pytest.raises(ContractError) as info:
            await world.add(A, f"Он сказал: {SECRET}, вот так.", external_id="d-1")
        return info.value.error.code

    assert run(scenario()) is ErrorCode.ACCESS_DENIED


def test_failed_cleanup_leaves_the_source_blocked_and_resume_finishes_it(tmp_path):
    class Broken:
        failing = True

        async def purge_sources(self, scope, source_ids):
            if self.failing:
                raise RuntimeError("export cache unavailable")

    async def scenario():
        world, doomed, *_ = await deletion_world(tmp_path)
        broken = Broken()
        service = world.restarted_service(purgers=(broken,))
        preview = await service.preview_delete(A, [doomed.source_id])
        report = await service.delete(A, preview)
        # not served while pending
        served = [h.source.source_id for h in await hits(world, A, SECRET)]
        pending = [e for e in await world.ledger.entries(A) if e.purged_at is None]
        broken.failing = False
        resumed = await service.resume(A)
        return report, served, pending, resumed, await world.ledger.entries(A), world, doomed

    report, served, pending, resumed, ledger, world, doomed = run(scenario())
    assert not report.complete
    assert report.outcomes[0].state == "blocked_pending_purge"
    assert report.outcomes[0].failed_step == "derived"
    assert doomed.source_id not in served and len(pending) == 1  # blocked, and visibly unfinished
    assert resumed.complete and ledger[0].purged_at is not None
    assert residue(world.paths, f"Он сказал: {SECRET}") == []


def test_a_busy_database_during_compaction_keeps_removal_pending(tmp_path):
    async def scenario():
        world, doomed, *_ = await deletion_world(tmp_path)
        real = world.physical.finalize

        async def busy():
            raise ContractError(ErrorCode.RESOURCE_BUSY)

        world.physical.finalize = busy
        preview = await world.retention.preview_delete(A, [doomed.source_id])
        report = await world.retention.delete(A, preview)
        entries = await world.ledger.entries(A)
        world.physical.finalize = real
        resumed = await world.retention.resume(A)
        return report, entries, resumed, doomed, world

    report, entries, resumed, doomed, world = run(scenario())
    assert not report.complete and entries[0].purged_at is None
    assert resumed.complete


# ---- restore gate ------------------------------------------------------------------------


def test_restoring_an_older_copy_cannot_revive_deleted_or_expired_text(tmp_path):
    async def scenario():
        world, doomed, shared, _ = await deletion_world(tmp_path)
        await world.physical.finalize()
        backup = tmp_path / "backup.sqlite"
        shutil.copy(world.paths[0], backup)  # a copy taken BEFORE the deletion
        preview = await world.retention.preview_delete(A, [doomed.source_id])
        await world.retention.delete(A, preview)
        world.clock.advance(days=45)
        await world.retention.sweep(A)  # the other source expires too
        await world.store.close()

        # restore: the old database comes back, the ledger file is the newer independent one
        from ai_agent_adapters.retention import SQLitePhysicalStore
        from ai_agent_adapters.storage.database import SQLiteStore

        restored_path = tmp_path / "restored.sqlite"
        shutil.copy(backup, restored_path)
        restored = SQLiteStore(restored_path, clock=world.clock, extensions=world.store.extensions)
        physical = SQLitePhysicalStore(restored)
        revived = await physical.source(A, doomed.source_id)
        revived_shared = await physical.source(A, shared.source_id)
        from ai_agent_application.memory import MemoryService
        from ai_agent_application.retention import RetentionService

        memory = MemoryService(restored, MEMORY, restored.extensions, clock=world.clock)
        service = RetentionService(
            restored, MEMORY, memory, physical, world.ledger, policy=world.policy, clock=world.clock
        )
        applied = await service.apply_ledger(A)
        again = await service.apply_ledger(A)
        return revived, revived_shared, applied, again, physical, doomed, shared, restored_path

    revived, revived_shared, applied, again, physical, doomed, shared, path = run(scenario())
    assert revived.has_content and revived_shared.has_content  # the old copy knows nothing
    assert set(applied.reapplied) == {doomed.source_id, shared.source_id}
    assert again.reapplied == () == again.pending  # idempotent

    async def verify():
        return await physical.source(A, doomed.source_id), await physical.source(
            A, shared.source_id
        )

    gone, expired = run(verify())
    assert not gone.has_content and gone.availability == "deleted"
    assert not expired.has_content and expired.availability == "expired"
    assert residue([path], f"Он сказал: {SECRET}") == []


def test_ledger_entries_for_unknown_sources_are_reported_not_invented(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.ledger.block(A, "src-never-seen", 1, "operator_delete", NOW)
        return await world.retention.apply_ledger(A)

    result = run(scenario())
    assert result.missing == ("src-never-seen",) and result.reapplied == ()


def test_ledger_never_weakens_a_stricter_reason_and_stays_content_free(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, B])
        await world.ledger.block(A, "s1", 1, "operator_delete", NOW)
        again = await world.ledger.block(A, "s1", 2, "expired", NOW + timedelta(days=1))
        await world.ledger.block(B, "s1", 1, "expired", NOW)
        return again, await world.ledger.entries(A), await world.ledger.entries(B)

    again, a, b = run(scenario())
    assert again.reason == "operator_delete" and again.version == 2
    assert a[0].blocked_at == NOW  # first block time is kept
    assert [e.reason for e in b] == ["expired"]  # scopes do not share lines


# ---- metadata ----------------------------------------------------------------------------


async def attempts(world, scope, ages):
    from usage_support import draft

    del draft
    for number, age in enumerate(ages):
        world.clock.now = NOW - timedelta(days=age)
        await world.usage.record_attempt(
            command(scope),
            attempt_report(f"op-{number}.a1", operation=f"op-{number}", at=world.clock.now),
        )
    world.clock.now = NOW


def test_old_attempt_metadata_becomes_aggregates_before_it_is_erased(tmp_path):
    async def scenario():
        from ai_agent_application.usage import UsageLedger
        from usage_support import draft

        world = await new_world(tmp_path, [A])
        world.usage = UsageLedger(world.store, LLM, world.store.extensions, clock=world.clock)
        await UsageWorld(world.store, world.usage, world.clock, world.store.extensions).assignment(
            A
        )
        await world.usage.set_tariff(command(A), draft(effective=NOW - timedelta(days=300)))
        await attempts(world, A, [120, 110, 5])
        before = await world.usage.attempts(A)
        for item in before:  # priced today: younger than the attempts they describe
            await world.usage.calculate(command(A), item.attempt_id)
        swept = await world.retention.sweep(A)
        after = await world.usage.attempts(A)
        again = await world.retention.sweep(A)
        return swept, again, before, after, await world.physical.aggregates(A)

    swept, again, before, after, aggregates = run(scenario())
    assert len(before) == 3 and [a.attempt_id for a in after] == ["op-2.a1"]
    assert swept.metadata["llm_attempts"] == 2 and swept.metadata["cost_calculations"] == 2
    assert again.metadata["llm_attempts"] == 0  # idempotent
    assert sum(r["attempts"] for r in aggregates) == 2  # counted once, still present
    assert sum(r["input_tokens"] for r in aggregates) == 2000
    assert all(r["unknown_usage"] == 0 for r in aggregates)
    assert sum(Decimal(r["amount"]) for r in aggregates) == Decimal(
        "10"
    )  # 2 x (1000 in x 2 + 500 out x 6) per 1000


def test_unknown_usage_stays_unknown_in_the_aggregate(tmp_path):
    async def scenario():
        from ai_agent_application.usage import UsageLedger
        from usage_support import UNKNOWN

        world = await new_world(tmp_path, [A])
        world.usage = UsageLedger(world.store, LLM, world.store.extensions, clock=world.clock)
        world.clock.now = NOW - timedelta(days=100)
        await world.usage.record_attempt(
            command(A),
            attempt_report("op-u.a1", operation="op-u", usage_=UNKNOWN, at=world.clock.now),
        )
        world.clock.now = NOW
        await world.retention.sweep(A)
        return await world.physical.aggregates(A)

    (row,) = run(scenario())
    assert row["attempts"] == 1 and row["unknown_usage"] == 1 and row["unknown_amount"] == 1
    assert row["input_tokens"] == 0 and row["currency"] == "-"  # not a priced zero


def test_aggregates_leave_after_twelve_months(tmp_path):
    async def scenario():
        from ai_agent_application.usage import UsageLedger

        world = await new_world(tmp_path, [A])
        world.usage = UsageLedger(world.store, LLM, world.store.extensions, clock=world.clock)
        world.clock.now = NOW - timedelta(days=100)
        await world.usage.record_attempt(command(A), attempt_report("op-1.a1", at=world.clock.now))
        world.clock.now = NOW
        await world.retention.sweep(A)
        kept = await world.physical.aggregates(A)
        world.clock.now = NOW + timedelta(days=400)
        report = await world.retention.sweep(A)
        return kept, report, await world.physical.aggregates(A)

    kept, report, left = run(scenario())
    assert len(kept) == 1 and report.metadata["aggregates"] == 1 and left == []


def test_finished_job_metadata_goes_but_unknown_outcomes_stay(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.put_projection(A, "op-done", "SUCCEEDED", age_days=45)
        await world.put_projection(A, "op-fresh", "FAILED", age_days=5)
        await world.put_projection(A, "op-unknown", "OUTCOME_UNKNOWN", age_days=45)
        report = await world.retention.sweep(A)
        repo = world.store.repository(OperationProjection, JOBS, A)
        return report, sorted(p.id for p in await repo.list(limit=50))

    report, left = run(scenario())
    assert report.metadata["jobs"] == 1 and left == ["op-fresh", "op-unknown"]


def test_expired_receipt_text_is_dropped_but_the_request_is_never_replayed(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A])
        await world.put_projection(A, "op-x", "FAILED", age_days=0, request_id="req-old")
        world.clock.advance(days=29)
        young = await world.retention.sweep(A)  # receipts are valid for 30 days
        world.clock.advance(days=2)
        old = await world.retention.sweep(A)
        with pytest.raises(ContractError) as info:  # the old request id must not run again
            await world.put_projection(A, "op-y", "FAILED", age_days=0, request_id="req-old")
        return young, old, info.value.error.code

    young, old, code = run(scenario())
    assert young.metadata["receipts"] == 0 and old.metadata["receipts"] == 1
    assert code is ErrorCode.OUTCOME_UNKNOWN


def test_sweep_is_scoped_to_one_memory_space(tmp_path):
    async def scenario():
        world = await new_world(tmp_path, [A, B])
        one = await world.add(A, "Запись первой области.", external_id="a-1")
        two = await world.add(B, "Запись второй области.", external_id="b-1")
        world.clock.advance(days=40)
        await world.retention.sweep(A)
        return await stored(world, A, one.source_id), await stored(world, B, two.source_id)

    one, two = run(scenario())
    assert not one.has_content and two.has_content
    assert rid()
