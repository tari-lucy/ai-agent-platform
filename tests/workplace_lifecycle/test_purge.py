"""Real SQLite/ledger/files and existing RetentionService, only synthetic content."""

import asyncio
import hashlib
import runpy
from dataclasses import replace
from pathlib import Path

import pytest
from ai_agent_adapters.retention.physical import SQLitePhysicalStore
from ai_agent_adapters.workspace_files.purge import WorkplacePurger
from ai_agent_application.retention import RetentionService
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from ai_agent_contracts.agent_sessions import WorkSession, WorkSessionMessage
from ai_agent_contracts.agent_workplace import SkillVersion, Ticket, TicketMessage, WorkAttempt
from ai_agent_contracts.api import Receipt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.control import Utterance

SUPPORT = runpy.run_path(str(Path(__file__).parents[1] / "workplace_files" / "wf_support.py"))
AREA, RUN, CAT, MEM, ACT = (SUPPORT[key] for key in ("AREA", "RUN", "CAT", "MEM", "ACT"))
TICK = Principal("tickets", "core_owner", Owner.TICKETS)
SKILL = Principal("skills", "core_owner", Owner.SKILLS)
GATEWAY = Principal("gateway", "core_owner", Owner.GATEWAY)
MARKER = "Synthetic observation about a lighthouse."


async def put(w, principal, record, request, expected=None):
    command = w.command(record.scope, request)
    async with w.store.transaction(principal, record.scope, command) as unit:
        await unit.put(
            record,
            expected_version=expected,
            access=(RecordAccess(CAT.id),) if expected is None else None,
        )
        await unit.complete(
            Receipt(
                request_id=request,
                scope=record.scope,
                status="completed",
                resource_id=record.id,
                version=record.version if hasattr(record, "version") else 1,
            )
        )


async def prepare(directory):
    w = await SUPPORT["World"].create(directory)
    await w.store.provision(
        AREA,
        (
            Grant(CAT, Owner.CATALOGUE, read=True, write=True, api_transfer=True),
            Grant(CAT, Owner.MEMORY, read=True, api_transfer=True),
            Grant(CAT, Owner.ACTIVITY, read=True),
            Grant(MEM, Owner.MEMORY, read=True, write=True),
            Grant(ACT, Owner.ACTIVITY, read=True, write=True),
            Grant(ACT, Owner.MEMORY, read=True),
            Grant(TICK, Owner.TICKETS, read=True, write=True),
            Grant(SKILL, Owner.SKILLS, read=True, write=True),
            Grant(GATEWAY, Owner.GATEWAY, read=True, write=True),
        ),
    )
    source = await w.source()
    admission = await w.admit([source])
    change, data = w.change(MARKER.encode())
    first = await w.files.write(admission, change, data)
    change, data = w.change(
        b"A later revision without the marker.", request="new-version", expected=1
    )
    await w.files.write(admission, change, data)
    independent = replace(
        admission, attempt_id="independent-attempt", operation_id="independent-op"
    )
    attempt = await w.attempt(RUN, admission.attempt_id)
    await put(
        w,
        ACT,
        attempt.model_copy(
            update={
                "id": independent.attempt_id,
                "task_id": "independent-task",
                "operation_id": independent.operation_id,
                "inputs": [],
                "input_files": [],
                "attempt_no": 99,
            }
        ),
        "separate-task",
    )
    change, data = w.change(b"Independent work.", file_id="independent", request="independent")
    other = await w.files.write(independent, change, data)
    now = w.now
    skill = SkillVersion(
        id="method",
        skill_id="method",
        scope=AREA,
        version=1,
        created_at=now,
        updated_at=now,
        title="A synthetic method",
        applicability=MARKER,
        limitations="Synthetic",
        method=first.reference,
        status="experimental",
        change_reason="Test",
    )
    await put(w, SKILL, skill, "make-skill")
    ticket = Ticket(
        id="ticket",
        ticket_id="ticket",
        scope=AREA,
        version=1,
        created_at=now,
        updated_at=now,
        kind="question",
        title="Synthetic question",
        summary=MARKER,
        attempted="Test",
        decision_needed="Test",
        state="waiting_reply",
        next_turn="operator",
        materials=[first.reference],
        agent_priority=0,
        duplicate_key="b" * 64,
        message_count=1,
        operator_read_version=0,
    )
    await put(w, TICK, ticket, "make-ticket")
    message = TicketMessage(
        id="ticket-msg",
        scope=AREA,
        created_at=now,
        ticket_id="ticket",
        sequence=1,
        author="agent",
        author_id="fictional",
        body=MARKER,
        ticket_version=1,
    )
    await put(w, TICK, message, "make-message")
    orphan = (MARKER + " interrupted save").encode()
    async with w.store.connection(write=True):
        await w.blobs.put(AREA, hashlib.sha256(orphan).hexdigest(), orphan, 65536)
    return w, source, admission, other


def test_deletion_erases_old_files_material_skills_messages_and_orphans(tmp_path):
    async def scenario():
        w, source, admission, other = await prepare(tmp_path)
        try:
            # A resumable session is also a derivative, including all referenced history.
            await put(
                w,
                ACT,
                WorkSession(
                    id="session",
                    scope=AREA,
                    version=1,
                    created_at=w.now,
                    updated_at=w.now,
                    title=MARKER,
                    last_sequence=1,
                    context_sources=[source],
                ),
                "make-session",
            )
            await put(
                w,
                ACT,
                WorkSessionMessage(
                    id="session-message",
                    scope=AREA,
                    version=1,
                    created_at=w.now,
                    updated_at=w.now,
                    session_id="session",
                    sequence=1,
                    speaker="operator",
                    source=source,
                ),
                "make-session-message",
            )
            await put(
                w,
                GATEWAY,
                Utterance(
                    id="work-reply",
                    utterance_id="work-reply",
                    scope=RUN,
                    version=1,
                    revision=1,
                    created_at=w.now,
                    updated_at=w.now,
                    content=MARKER,
                    content_hash=hashlib.sha256(MARKER.encode()).hexdigest(),
                    audience_id="operator-work-panel",
                    channel_ids=["operator-work-panel"],
                    moderation_status="draft",
                ),
                "make-reply",
            )
            await put(
                w,
                ACT,
                WorkAttempt(
                    id="session-attempt",
                    scope=RUN,
                    version=1,
                    created_at=w.now,
                    updated_at=w.now,
                    session_id="session",
                    session_sequence=1,
                    attempt_no=1,
                    operation_id="session-operation",
                    capability_id="session-harness",
                    capability_version=1,
                    freshness=admission.freshness,
                    state="succeeded",
                    result_id="work-reply",
                ),
                "make-session-attempt",
            )
            approved = None
            quiesced = []

            async def confirm(scope, plan):
                assert scope.partition == AREA.partition and plan.source_ids == approved.source_ids
                accepted = {
                    (kind, identifier): token for kind, identifier, token in approved.records
                }
                assert all(
                    accepted.get((kind, identifier)) == token
                    for kind, identifier, token in plan.records
                )

            async def quiesce(scope, ids):
                quiesced.append(tuple(ids))

            purger = WorkplacePurger(w.store, w.blobs, w.ledger, confirm=confirm, quiesce=quiesce)
            approved = await purger.preview(AREA, [source.source_id])
            identities = set(approved.identities)
            assert {
                ("ManagedFile", "note"),
                ("SkillVersion", "method"),
                ("Ticket", "ticket"),
                ("TicketMessage", "ticket-msg"),
                ("WorkSession", "session"),
                ("WorkSessionMessage", "session-message"),
                ("WorkAttempt", "session-attempt"),
                ("Utterance", "work-reply"),
            } <= identities
            assert ("ManagedFile", "independent") not in identities
            physical = SQLitePhysicalStore(w.store)
            await physical.install()
            retention = RetentionService(
                w.store, MEM, w.memory, physical, w.ledger, purgers=(purger,), clock=w.clock
            )
            deleted = await retention.delete(
                AREA, await retention.preview_delete(AREA, [source.source_id])
            )
            assert deleted.complete and quiesced == [(source.source_id,)]
            assert (await w.ledger.entries(AREA))[0].purged_at is not None
            assert await w.files.search(AREA, "lighthouse") == []
            assert (await w.files.list(AREA, "knowledge"))[0].id == other.id
            # Source, all catalogue revisions, ticket/skill text and orphan bodies are gone.
            assert MARKER.encode() not in Path(w.store.engine.url.database).read_bytes()
            for leaf in w.blobs.root.rglob("*"):
                if leaf.is_file():
                    assert MARKER.encode() not in leaf.read_bytes()
            await purger.purge_sources(AREA, [source.source_id])  # idempotent retry with no bodies
        finally:
            await w.close()

    asyncio.run(scenario())


def test_purge_refuses_unblocked_source_and_keeps_preview_read_only(tmp_path):
    async def scenario():
        w, source, _, _ = await prepare(tmp_path)
        try:

            async def forbid(*args):
                raise AssertionError("must not reach mutation hooks")

            purger = WorkplacePurger(w.store, w.blobs, w.ledger, confirm=forbid, quiesce=forbid)
            plan = await purger.preview(AREA, [source.source_id])
            again = await purger.preview(AREA, [source.source_id])
            assert plan == again and plan.records
            with pytest.raises(RuntimeError, match="WORKPLACE_SOURCE_NOT_BLOCKED"):
                await purger.purge_sources(AREA, [source.source_id])
            assert any(
                MARKER.encode() in p.read_bytes() for p in w.blobs.root.rglob("*") if p.is_file()
            )
        finally:
            await w.close()

    asyncio.run(scenario())


def test_failed_physical_purge_stays_blocked_then_resumes(tmp_path, monkeypatch):
    async def scenario():
        w, source, admission, _ = await prepare(tmp_path)
        try:

            async def confirm(*args):
                pass  # synthetic operator has approved the complete impact

            async def quiesce(*args):
                pass

            physical = SQLitePhysicalStore(w.store)
            purger = WorkplacePurger(w.store, w.blobs, w.ledger, confirm=confirm, quiesce=quiesce)
            retention = RetentionService(
                w.store, MEM, w.memory, physical, w.ledger, purgers=(purger,), clock=w.clock
            )
            original = w.blobs.prune

            async def locked(*args, **kwargs):
                raise PermissionError("synthetic file lock")

            monkeypatch.setattr(w.blobs, "prune", locked)
            done = await retention.delete(
                AREA, await retention.preview_delete(AREA, [source.source_id])
            )
            assert not done.complete and done.outcomes[0].failed_step == "derived"
            assert (await w.ledger.entries(AREA))[0].purged_at is None
            assert await w.store.repository(Ticket, TICK, AREA).get("ticket") is None
            assert await w.store.repository(TicketMessage, TICK, AREA).get("ticket-msg") is None
            with pytest.raises(ContractError):
                await w.files.read(admission, "note")
            monkeypatch.setattr(w.blobs, "prune", original)
            assert (await retention.resume(AREA)).complete
            assert MARKER.encode() not in Path(w.store.engine.url.database).read_bytes()
        finally:
            await w.close()

    asyncio.run(scenario())


def test_old_snapshot_cannot_revive_deleted_workplace_text(tmp_path):
    import shutil

    from ai_agent_adapters.backup.sqlite import SQLiteSnapshotter
    from ai_agent_adapters.storage.database import SQLiteStore
    from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
    from ai_agent_application.memory import MemoryService
    from ai_agent_application.workspace_files import FileReleaseGuard
    from ai_agent_contracts.agent_workplace import ManagedFile

    async def scenario():
        w, source, _, _ = await prepare(tmp_path)
        staged = None
        try:

            async def confirm(scope, plan):
                assert set(plan.source_ids) == {source.source_id}

            async def quiesce(*args):
                pass

            snapshot = tmp_path / "old.sqlite"
            copied_blobs = tmp_path / "old-blobs"
            await SQLiteSnapshotter(Path(w.store.engine.url.database)).snapshot(snapshot)
            shutil.copytree(
                w.blobs.root, copied_blobs
            )  # new synthetic destination, offline snapshot
            original_physical = SQLitePhysicalStore(w.store)
            retention = RetentionService(
                w.store,
                MEM,
                w.memory,
                original_physical,
                w.ledger,
                purgers=(
                    WorkplacePurger(w.store, w.blobs, w.ledger, confirm=confirm, quiesce=quiesce),
                ),
                clock=w.clock,
            )
            assert (
                await retention.delete(
                    AREA, await retention.preview_delete(AREA, [source.source_id])
                )
            ).complete
            staged = SQLiteStore(snapshot, clock=w.clock)
            old_file = await staged.repository(ManagedFile, CAT, AREA).get("note", version=1)
            assert old_file is not None  # the old backup really contains the text's provenance
            with pytest.raises(ContractError):
                await FileReleaseGuard(staged, CAT, w.ledger, clock=w.clock).check(
                    AREA, (), [old_file.reference]
                )
            physical = SQLitePhysicalStore(staged)
            purge = WorkplacePurger(
                staged, DirectoryBlobs(copied_blobs), w.ledger, confirm=confirm, quiesce=quiesce
            )
            memory = MemoryService(staged, MEM, w.registry, clock=w.clock)
            restored = RetentionService(
                staged, MEM, memory, physical, w.ledger, purgers=(purge,), clock=w.clock
            )
            outcome = await restored.apply_ledger(AREA)
            assert source.source_id in outcome.reapplied and not outcome.pending
            assert MARKER.encode() not in snapshot.read_bytes()
            assert all(
                MARKER.encode() not in leaf.read_bytes()
                for leaf in copied_blobs.rglob("*")
                if leaf.is_file()
            )
            assert await staged.repository(ManagedFile, CAT, AREA).get("independent") is not None
        finally:
            if staged is not None:
                await staged.close()
            await w.close()

    asyncio.run(scenario())


def test_scan_limit_and_foreign_partition_do_not_widen_deletion(tmp_path):
    async def scenario():
        w, source, _, _ = await prepare(tmp_path)
        try:

            async def forbid(*args):
                raise AssertionError("no mutation expected")

            small = WorkplacePurger(
                w.store, w.blobs, w.ledger, confirm=forbid, quiesce=forbid, max_rows=1
            )
            with pytest.raises(RuntimeError, match="WORKPLACE_SCAN_LIMIT"):
                await small.preview(AREA, [source.source_id])
            foreign = AREA.model_copy(update={"memory_mode": "test", "memory_space_id": "isolated"})
            normal = WorkplacePurger(w.store, w.blobs, w.ledger, confirm=forbid, quiesce=forbid)
            assert (await normal.preview(foreign, [source.source_id])).records == ()
            with pytest.raises(RuntimeError, match="WORKPLACE_SOURCE_NOT_BLOCKED"):
                await normal.purge_sources(foreign, [source.source_id])
        finally:
            await w.close()

    asyncio.run(scenario())


def test_clean_source_stub_does_not_skip_new_derivatives_on_restore(tmp_path):
    async def scenario():
        w, source, _, _ = await prepare(tmp_path)
        try:
            physical = SQLitePhysicalStore(w.store)
            await w.ledger.block(AREA, source.source_id, 1, "operator_delete", w.now)
            await physical.erase_source(AREA, source.source_id, "deleted")
            await w.ledger.mark_purged(AREA, source.source_id, w.now)

            async def confirm(scope, plan):
                assert plan.source_ids == (source.source_id,)

            async def quiesce(*args):
                pass

            purger = WorkplacePurger(w.store, w.blobs, w.ledger, confirm=confirm, quiesce=quiesce)
            retention = RetentionService(
                w.store, MEM, w.memory, physical, w.ledger, purgers=(purger,), clock=w.clock
            )
            assert (await purger.preview(AREA, [source.source_id])).records  # genuinely dirty copy
            assert not (await retention.apply_ledger(AREA)).pending
            assert (await purger.preview(AREA, [source.source_id])).records == ()
            assert MARKER.encode() not in Path(w.store.engine.url.database).read_bytes()
        finally:
            await w.close()

    asyncio.run(scenario())
