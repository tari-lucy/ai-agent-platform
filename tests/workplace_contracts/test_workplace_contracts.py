"""AW-00 contract acceptance: synthetic IDs/text only, real SQLite for owner registration."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from ai_agent_adapters.storage.catalogue import SPECS
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts import Scope
from ai_agent_contracts import agent_workplace as aw
from ai_agent_contracts.api import MessageArguments, MessageCommand, Receipt
from ai_agent_contracts.catalogue import ADAPTERS
from ai_agent_contracts.codec import ContractError, from_wire
from ai_agent_contracts.states import ErrorCode
from pydantic import TypeAdapter, ValidationError

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 21, tzinfo=UTC)
AREA = {
    "workspace_id": "synthetic-workspace",
    "persona_id": "synthetic-persona",
    "memory_space_id": "synthetic-space",
    "memory_mode": "test",
}
RUN = AREA | {"agent_id": "agent-01", "run_id": "run-01"}
OTHER = AREA | {"memory_space_id": "other-space"}
FRESH = {"run_id": "run-01", "control_epoch": 1, "profile_version": 1, "policy_version": 1}
DIGEST = hashlib.sha256(b"synthetic").hexdigest()


def base(record_id, scope=AREA, version=1):
    return {
        "id": record_id,
        "scope": scope,
        "created_at": NOW.isoformat(),
        "version": version,
        "updated_at": NOW.isoformat(),
    }


def ref(record_id="file-01", scope=AREA, version=1):
    return {
        "artifact_id": record_id,
        "version": version,
        "scope": scope,
        "checksum": DIGEST,
        "size_bytes": 9,
        "media_type": "text/markdown",
    }


def source(scope=AREA, availability="available"):
    return {"source_id": "source-01", "version": 1, "scope": scope, "availability": availability}


def decode(model, value):
    return from_wire(TypeAdapter(model), json.dumps(value).encode())


def managed(**changes):
    value = base("file-01") | {
        "reference": ref(),
        "area": "knowledge",
        "path": "notes/first-note.md",
        "title": "Вымышленная заметка",
        "kind": "note",
        "provenance": "generated",
        "dependencies": [source()],
        "access": {"api_transfer": True, "public_disclosure": False},
        "state": "active",
    }
    value.update(changes)
    return decode(aw.ManagedFile, value)


def ticket(**changes):
    value = base("ticket-01") | {
        "ticket_id": "ticket-01",
        "kind": "question",
        "title": "Вымышленный вопрос",
        "summary": "Нужен справочник.",
        "attempted": "Поиск в knowledge.",
        "decision_needed": "Выдать материал или отказать.",
        "state": "waiting_reply",
        "next_turn": "operator",
        "agent_priority": 1,
        "duplicate_key": DIGEST,
        "message_count": 1,
        "operator_read_version": 0,
    }
    value.update(changes)
    return decode(aw.Ticket, value)


def rejected(code, factory, **changes):
    with pytest.raises((ContractError, ValidationError)) as info:
        factory(**changes)
    assert code in str(info.value) or isinstance(info.value, ContractError)


def test_managed_file_identity_path_and_dependencies():
    assert managed().reference.artifact_id == "file-01"
    rejected("ARTIFACT_REFERENCE_MISMATCH", managed, reference=ref(version=2))
    for bad in ("../secret", "/abs", "a//b", "notes/..", "Notes/A.md", "con.txt", "a\\b", "x" * 70):
        rejected("LOGICAL_PATH", managed, path=bad)
    # A deleted source blocks the file instead of being silently dropped.
    rejected("DEPENDENCY_UNAVAILABLE", managed, dependencies=[source(availability="deleted")])
    assert managed(state="blocked", dependencies=[source(availability="deleted")])
    rejected("GROUNDS_REQUIRED", managed, provenance="verified", dependencies=[])
    # Personal areas belong to stable IDs, not to a run; cross-scope references are refused.
    rejected("STABLE_SCOPE_REQUIRED", managed, scope=RUN, reference=ref(scope=RUN), dependencies=[])
    rejected("SCOPE_MISMATCH", managed, dependencies=[source(scope=OTHER)])


def test_file_write_cannot_target_read_only_areas_or_claim_verification():
    value = {
        "request_id": "request-01",
        "file_id": "file-01",
        "expected_version": None,
        "area": "projects",
        "path": "plan.md",
        "title": "План",
        "kind": "plan",
        "provenance": "generated",
        "media_type": "text/markdown",
        "checksum": DIGEST,
        "size_bytes": 9,
    }
    assert decode(aw.FileWrite, value).expected_version is None
    for area in ("reference", "guide"):
        with pytest.raises(ContractError):
            decode(aw.FileWrite, value | {"area": area})
    with pytest.raises(ContractError):
        decode(aw.FileWrite, value | {"provenance": "verified"})


def test_task_and_attempt_are_separate_and_bounded():
    task = base("task-01") | {
        "task_id": "task-01",
        "origin": "own_goal",
        "goal": "Разобрать материал.",
        "reason": "Интерес профиля.",
        "state": "open",
    }
    assert decode(aw.WorkTask, task).state == "open"
    with pytest.raises(ContractError):
        decode(aw.WorkTask, task | {"state": "waiting"})
    with pytest.raises(ContractError):
        decode(aw.WorkTask, task | {"state": "done"})
    attempt = base("attempt-01", RUN) | {
        "task_id": "task-01",
        "attempt_no": 1,
        "operation_id": "operation-01",
        "capability_id": "files.write",
        "capability_version": 1,
        "freshness": FRESH,
        "state": "unknown",
        "inputs": [source(RUN)],
    }
    assert decode(aw.WorkAttempt, attempt).state == "unknown"
    with pytest.raises(ContractError):
        decode(aw.WorkAttempt, attempt | {"state": "succeeded"})
    with pytest.raises(ContractError):
        decode(aw.WorkAttempt, attempt | {"freshness": FRESH | {"run_id": "run-02"}})


def test_skill_status_needs_evidence_and_ticket_turns_are_consistent():
    skill = base("skill-01") | {
        "skill_id": "skill-01",
        "title": "Метод конспекта",
        "applicability": "Длинный текст.",
        "limitations": "Не для таблиц.",
        "method": ref("skill-file-01"),
        "status": "experimental",
        "change_reason": "Первая версия.",
    }
    assert decode(aw.SkillVersion, skill).status == "experimental"
    with pytest.raises(ContractError):
        decode(aw.SkillVersion, skill | {"status": "verified"})
    assert decode(aw.SkillVersion, skill | {"status": "verified", "evidence": [source()]})

    assert ticket().next_turn == "operator"
    rejected("NEXT_TURN", ticket, next_turn="agent")
    rejected("NEXT_TURN", ticket, state="resolved", resolution="Готово.")
    assert ticket(state="resolved", next_turn=None, resolution="Готово.").next_turn is None
    rejected("CAPABILITY_REQUEST_SHAPE", ticket, kind="capability_request")
    request = {"capability_id": "web.fetch", "constraints": "Только чтение."}
    assert ticket(kind="capability_request", request=request).request is not None


def test_binding_ready_requires_verification():
    value = base("binding-01") | {
        "binding_id": "binding-01",
        "capability_id": "files.write",
        "capability_version": 1,
        "adapter_id": "workspace-files",
        "adapter_version": "1",
        "state": "pending",
    }
    assert decode(aw.CapabilityBinding, value).state == "pending"
    with pytest.raises(ContractError):
        decode(aw.CapabilityBinding, value | {"state": "ready"})
    assert decode(aw.CapabilityBinding, value | {"state": "ready", "verified_at": NOW.isoformat()})


def test_wire_catalogue_and_single_owner_registration():
    owners = {
        "CapabilityManifest": Owner.WORKSPACE,
        "CapabilityBinding": Owner.WORKSPACE,
        "WorkTask": Owner.ACTIVITY,
        "WorkAttempt": Owner.ACTIVITY,
        "ManagedFile": Owner.CATALOGUE,
        "SkillVersion": Owner.SKILLS,
        "Ticket": Owner.TICKETS,
        "TicketMessage": Owner.TICKETS,
    }
    for name, owner in owners.items():
        assert name in ADAPTERS
        assert SPECS[name].owner is owner
    # Attempts and rights are not duplicated: jobs and permissions keep their owners.
    assert SPECS["Job"].owner is Owner.JOBS and SPECS["Permission"].owner is Owner.WORKSPACE


def test_owner_storage_cas_cross_owner_and_duplicate_key(tmp_path):
    tickets = Principal("owner-tickets", "core_owner", Owner.TICKETS)
    catalogue = Principal("owner-catalogue", "core_owner", Owner.CATALOGUE)
    scope = Scope.model_validate(AREA)

    async def save(principal, record, request_id, expected=None):
        command = MessageCommand(
            request_id=request_id,
            scope=scope,
            target="synthetic-target",
            type="dialogue.message",
            arguments=MessageArguments(text="Вымышленная команда " + request_id),
        )
        async with store.transaction(principal, scope, command) as unit:
            if unit.replay is not None:
                return unit.replay
            await unit.put(record, expected_version=expected)
            receipt = Receipt(
                request_id=request_id,
                scope=scope,
                status="completed",
                resource_id=record.id,
                version=record.version,
            )
            await unit.complete(receipt)
            return receipt

    async def scenario():
        try:
            await checks()
        finally:
            await store.close()

    async def checks():
        await store.upgrade(ROOT / "migrations")
        await store.provision(
            scope,
            (
                Grant(tickets, Owner.TICKETS, read=True, write=True),
                Grant(catalogue, Owner.CATALOGUE, read=True, write=True),
            ),
        )
        first = await save(tickets, ticket(), "request-01")
        assert first.version == 1
        # Replay returns the stored receipt; stale CAS and other owners are refused.
        assert await save(tickets, ticket(), "request-01") == first
        with pytest.raises(ContractError) as stale:
            await save(tickets, ticket(version=2, message_count=2), "request-02", expected=2)
        assert stale.value.error.code is ErrorCode.VERSION_CONFLICT
        with pytest.raises(ContractError) as foreign:
            await save(catalogue, ticket(id="ticket-02", ticket_id="ticket-02"), "request-03")
        assert foreign.value.error.code is ErrorCode.ACCESS_DENIED
        with pytest.raises(ContractError):
            # Same duplicate key: a repeat appends to the existing ticket, not a second one.
            await save(tickets, ticket(id="ticket-03", ticket_id="ticket-03"), "request-04")
        with pytest.raises(ContractError) as missing:
            # Dependencies are checked against the source owner; an invented source is refused.
            await save(catalogue, managed(), "request-05")
        assert missing.value.error.code is ErrorCode.ACCESS_DENIED
        await save(catalogue, managed(dependencies=[]), "request-06")
        with pytest.raises(ContractError):
            other = managed(id="file-02", reference=ref("file-02"), dependencies=[])
            await save(catalogue, other, "request-07")
        stored = await store.repository(aw.ManagedFile, catalogue, scope).get("file-01")
        assert stored is not None and stored.path == "notes/first-note.md"

    store = SQLiteStore(tmp_path / (uuid4().hex + ".sqlite"), clock=lambda: NOW)
    asyncio.run(scenario())
