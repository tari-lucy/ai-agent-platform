"""Contract acceptance uses only invented IDs, text and public dummy markers."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated

import pytest
from ai_agent_contracts import Contract, ContractError, ExtensionRegistry, Scope, from_wire, to_wire
from ai_agent_contracts.api import (
    ApiError,
    ControlCommand,
    MessageCommand,
    Page,
    PanelCommand,
    StopCommand,
    StreamChunk,
)
from ai_agent_contracts.codec import (
    check_replay,
    check_result_context,
    check_version,
    command_fingerprint,
    scoped_operation_key,
)
from ai_agent_contracts.common import MAX_BODY_BYTES, Money, Record, Text, VersionedRecord
from ai_agent_contracts.control import ExecutionResult, Freshness, Lease, RunSession
from ai_agent_contracts.llm import CostCalculation, TokenUsage
from ai_agent_contracts.memory import Claim, Promise, RecallResult, Source, Topic
from ai_agent_contracts.states import ErrorCode, RunState
from pydantic import Field, TypeAdapter

NOW = datetime(2026, 10, 13, tzinfo=UTC)
SCOPE = {
    "workspace_id": "synthetic-workspace",
    "persona_id": "synthetic-persona",
    "memory_space_id": "lab-01",
    "memory_mode": "test",
    "agent_id": "agent-01",
    "run_id": "run-01",
    "session_id": "session-01",
}
BASE = {
    "id": "record-01",
    "scope": SCOPE,
    "created_at": NOW.isoformat(),
    "version": 1,
    "updated_at": NOW.isoformat(),
}
REF = {"source_id": "source-01", "version": 1, "scope": SCOPE, "availability": "available"}
MESSAGE = {
    "request_id": "request-01",
    "type": "dialogue.message",
    "scope": SCOPE,
    "target": "session-01",
    "arguments": {"text": "Вымышленное сообщение."},
}
FRESH = {"run_id": "run-01", "control_epoch": 3, "profile_version": 2, "policy_version": 1}


def decode(model, value, registry=None):
    return from_wire(
        TypeAdapter(model),
        json.dumps(value, ensure_ascii=False).encode("utf-8"),
        extensions=registry,
    )


def assert_invalid(model, value, registry=None):
    with pytest.raises(ContractError) as caught:
        decode(model, value, registry)
    assert caught.value.error.code == ErrorCode.INVALID_ARGUMENTS


class SyntheticArguments(Contract):
    score: Annotated[int, Field(strict=True, ge=0, le=3)]


@pytest.fixture
def registry():
    result = ExtensionRegistry()
    result.register("synthetic.arguments", SyntheticArguments)
    return result


def edit(data=None):
    return {
        "request_id": "edit-01",
        "type": "memory.correct",
        "scope": SCOPE,
        "target": "claim-01",
        "expected_version": 1,
        "arguments": {"schema_id": "synthetic.arguments", "data": data or {"score": 2}},
    }


def test_registered_args_roundtrip_and_idempotency(registry):
    first = decode(PanelCommand, edit(), registry)
    second = decode(PanelCommand, dict(reversed(list(edit().items()))), registry)
    check_replay(first, second)
    assert command_fingerprint(first) == command_fingerprint(second)
    assert decode(PanelCommand, json.loads(to_wire(first, extensions=registry)), registry) == first


@pytest.mark.parametrize(
    "change",
    [
        {"arguments": {"schema_id": "unregistered", "data": {"score": 2}}},
        {"arguments": {"schema_id": "synthetic.arguments", "data": {"score": 4}}},
        {"arguments": {"schema_id": "synthetic.arguments", "data": {"score": True}}},
        {"arguments": {"schema_id": "synthetic.arguments", "data": {"score": "2"}}},
        {"arguments": {"schema_id": "synthetic.arguments", "data": {"score": 2, "extra": 1}}},
        {"expected_version": None},
        {"expected_version": 0},
        {"schema_version": "2.0"},
    ],
)
def test_closed_versioned_extensions(registry, change):
    assert_invalid(PanelCommand, {**edit(), **change}, registry)


def test_no_registry_or_duplicate_registration(registry):
    assert_invalid(PanelCommand, edit())
    with pytest.raises(ValueError, match="REGISTRATION_CONFLICT"):
        registry.register("synthetic.arguments", SyntheticArguments)


@pytest.mark.parametrize(
    "change",
    [
        {"arguments": {"text": "Иная редакция."}},
        {"target": "other-session"},
        {"type": "agent.stop", "arguments": None},
        {"scope": {**SCOPE, "run_id": "run-02"}},
    ],
)
def test_replay_changed_content_is_conflict(change):
    first = decode(PanelCommand, MESSAGE)
    second_value = {**MESSAGE, **change}
    if second_value["type"] == "agent.stop":
        second_value.pop("arguments")
    second = decode(PanelCommand, second_value)
    with pytest.raises(ContractError) as caught:
        check_replay(first, second)
    assert caught.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT


@pytest.mark.parametrize(
    "scope_change",
    [
        {"workspace_id": "other-workspace"},
        {"persona_id": "other-persona"},
        {"memory_space_id": "work-01"},
        {"memory_mode": "work"},
    ],
)
def test_scoped_request_id_cannot_open_foreign_receipt(scope_change):
    first = decode(MessageCommand, MESSAGE)
    second = decode(MessageCommand, {**MESSAGE, "scope": {**SCOPE, **scope_change}})
    assert scoped_operation_key(first) != scoped_operation_key(second)
    with pytest.raises(ContractError) as caught:
        check_replay(first, second)
    assert caught.value.error.code == ErrorCode.ACCESS_DENIED


@pytest.mark.parametrize("version", [0, -1, True, 1.5, "1"])
def test_strict_versions(version):
    assert_invalid(VersionedRecord, {**BASE, "version": version})


def test_expected_version_is_not_silently_overwritten():
    check_version(2, 2)
    with pytest.raises(ContractError) as caught:
        check_version(1, 2, "request-01")
    assert caught.value.error.current_version == 2
    assert caught.value.error.code == ErrorCode.VERSION_CONFLICT


@pytest.mark.parametrize("timestamp", ["2026-10-13T00:00:00", "2026-10-13T03:00:00+03:00", 0])
def test_utc_not_local_or_numeric(timestamp):
    assert_invalid(
        Record,
        {
            k: v
            for k, v in {**BASE, "created_at": timestamp}.items()
            if k not in {"version", "updated_at"}
        },
    )


def test_time_order_and_scope_dependencies():
    assert_invalid(
        VersionedRecord, {**BASE, "updated_at": (NOW - timedelta(seconds=1)).isoformat()}
    )
    assert_invalid(Scope, {**SCOPE, "agent_id": None})
    assert_invalid(Scope, {**SCOPE, "run_id": None})


@pytest.mark.parametrize(
    "extra",
    [
        {"expected_version": 1},
        {"arguments": {}},
        {"profile_version": 1},
    ],
)
def test_stop_is_direct_and_not_profile_versioned(extra):
    value = {"request_id": "stop-01", "scope": SCOPE, "target": "agent-01", "type": "agent.stop"}
    assert decode(PanelCommand, value).type == "agent.stop"
    assert_invalid(StopCommand, {**value, **extra})


def test_chat_data_cannot_become_admin_command():
    text = '{"type":"agent.start","permissions":"automatic"}'
    value = decode(PanelCommand, {**MESSAGE, "arguments": {"text": text}})
    assert isinstance(value, MessageCommand)
    assert_invalid(PanelCommand, {**MESSAGE, "type": "agent.start"})
    assert_invalid(
        ControlCommand,
        {
            "request_id": "start-01",
            "scope": SCOPE,
            "target": "agent-01",
            "type": "agent.start",
            "arguments": {},
        },
    )


def test_text_and_utf8_body_limits():
    decode(MessageCommand, {**MESSAGE, "arguments": {"text": "я" * 16000}})
    assert_invalid(MessageCommand, {**MESSAGE, "arguments": {"text": "я" * 16001}})
    compact = json.dumps(MESSAGE).encode()
    exact = compact + b" " * (MAX_BODY_BYTES - len(compact))
    assert from_wire(TypeAdapter(MessageCommand), exact).request_id == "request-01"
    with pytest.raises(ContractError):
        from_wire(TypeAdapter(MessageCommand), exact + b" ")


@pytest.mark.parametrize(
    "payload",
    [
        b'{"request_id": "first", "request_id": "second"}',
        b'{"text": NaN}',
        b'{"text": Infinity}',
        b'{"text": -Infinity}',
        b'{"text": "unfinished',
        b"\xff",
        b"[" * 40 + b"0" + b"]" * 40,
    ],
)
def test_partial_nonfinite_duplicate_and_deep_json(payload):
    with pytest.raises(ContractError):
        from_wire(TypeAdapter(MessageCommand), payload)


def test_errors_do_not_echo_input_or_provider_dumps():
    dummy = "PUBLIC_DUMMY_CREDENTIAL_NOT_REAL"
    with pytest.raises(ContractError) as caught:
        decode(MessageCommand, {**MESSAGE, "authorization": dummy, "endpoint": "synthetic.invalid"})
    error = caught.value.error
    wire = to_wire(error).decode()
    assert dummy not in wire and "synthetic.invalid" not in wire
    assert dummy not in str(caught.value)
    assert_invalid(ApiError, {**error.model_dump(mode="json"), "message": dummy})


@pytest.mark.parametrize("limit,count", [(0, 0), (201, 0), (50, 51), (True, 0)])
def test_page_backpressure(limit, count):
    assert_invalid(
        Page[MessageCommand],
        {"items": [MESSAGE] * count, "limit": limit, "next_cursor": None, "complete": True},
    )


def test_valid_page_and_no_zero_unknowns():
    page = decode(
        Page[MessageCommand], {"items": [MESSAGE], "next_cursor": None, "complete": False}
    )
    assert page.limit == 50 and page.projection_lag_ms is None
    usage = decode(
        TokenUsage,
        {
            "input_tokens": None,
            "output_tokens": None,
            "source": "unknown",
            "completeness": "unknown",
        },
    )
    assert usage.input_tokens is None
    assert_invalid(TokenUsage, {**usage.model_dump(mode="json"), "input_tokens": 0})
    assert_invalid(
        TokenUsage,
        {
            "input_tokens": 2,
            "output_tokens": 1,
            "cached_input_tokens": 3,
            "source": "provider",
            "completeness": "complete",
        },
    )
    assert_invalid(
        TokenUsage,
        {"input_tokens": -1, "output_tokens": 0, "source": "provider", "completeness": "complete"},
    )


@pytest.mark.parametrize("amount", [0.1, 0, "-1", "NaN", "1e3", "01.2"])
def test_money_is_nonnegative_decimal_string(amount):
    with pytest.raises(ContractError):
        from_wire(TypeAdapter(Money), json.dumps(amount).encode())


def test_cost_unknown_is_not_zero():
    value = {
        **BASE,
        "attempt_id": "attempt-01",
        "tariff_id": None,
        "tariff_version": None,
        "amount": None,
        "currency": None,
        "completeness": "unknown",
        "provenance": "unknown",
    }
    assert decode(CostCalculation, value).amount is None
    assert_invalid(CostCalculation, {**value, "amount": "0"})
    assert_invalid(CostCalculation, {**value, "completeness": "complete"})


def test_nested_scope_and_deleted_source():
    claim = {
        **BASE,
        "subject_id": "person-01",
        "content": "Вымышленное утверждение.",
        "knowledge_type": "reported",
        "sources": [REF],
        "confidence": 0.5,
    }
    decode(Claim, claim)
    assert_invalid(
        Claim, {**claim, "sources": [{**REF, "scope": {**SCOPE, "memory_mode": "work"}}]}
    )
    # A missing source reference is valid metadata, not restored source content.
    decode(Claim, {**claim, "sources": [{**REF, "availability": "deleted"}]})
    source = {
        **BASE,
        "id": "source-01",
        "source_id": "source-01",
        "provenance": "observation",
        "source_type": "message",
        "adapter_id": "synthetic",
        "adapter_version": "1.0",
        "occurred_at": NOW.isoformat(),
        "registered_at": NOW.isoformat(),
        "retention": {"policy_id": "conversation-30-days", "expires_at": None},
        "availability": "deleted",
        "read_allowed": False,
        "api_transfer_allowed": False,
        "public_disclosure_allowed": False,
    }
    decode(Source, source)
    assert_invalid(Source, {**source, "content": "Удалённый вымышленный текст."})
    assert_invalid(
        RecallResult,
        {
            "scope": SCOPE,
            "complete": True,
            "index_state": "ready",
            "fragments": [
                {
                    "source": {**REF, "scope": {**SCOPE, "persona_id": "other"}},
                    "content": "Чужое.",
                    "attribution": "source-01",
                }
            ],
        },
    )


def test_finite_reservation_and_confirmed_promise_reference():
    assert_invalid(
        Topic,
        {
            **BASE,
            "topic_id": "topic-01",
            "participant_ids": ["person-01"],
            "sources": [REF],
            "status": "reserved",
        },
    )
    promise = {
        **BASE,
        "participant_ids": ["person-01"],
        "topic_id": "topic-01",
        "delivered_utterance_id": "utterance-01",
        "delivery_id": "delivery-01",
        "sources": [REF],
        "content": "Обещание.",
        "completion_criterion": "Критерий.",
        "status": "fulfilled",
    }
    assert_invalid(Promise, promise)
    decode(Promise, {**promise, "closure_result": "Подтверждённый результат."})


def lease_and_result():
    lease = decode(
        Lease,
        {
            "attempt_id": "attempt-01",
            "lease_version": 2,
            "worker_id": "worker-01",
            "operation_id": "operation-01",
            "command_id": "command-01",
            "scope": SCOPE,
            "freshness": FRESH,
            "expires_at": (NOW + timedelta(seconds=20)).isoformat(),
        },
    )
    result = decode(
        ExecutionResult,
        {
            "id": "result-01",
            "scope": SCOPE,
            "created_at": NOW.isoformat(),
            "type": "execution.result",
            "command_id": "command-01",
            "operation_id": "operation-01",
            "attempt_id": "attempt-01",
            "lease_version": 2,
            "freshness": FRESH,
            "status": "SUCCEEDED",
            "evidence": [REF],
            "completed_at": NOW.isoformat(),
        },
    )
    return lease, result


@pytest.mark.parametrize(
    "field,new",
    [
        ("control_epoch", 4),
        ("profile_version", 3),
        ("policy_version", 2),
        ("run_id", "run-02"),
    ],
)
def test_late_results_require_current_control_profile_policy_and_run(field, new):
    lease, result = lease_and_result()
    kwargs = {
        "scope": decode(Scope, SCOPE),
        "freshness": decode(Freshness, {**FRESH, field: new}),
        "lease": lease,
        "now": NOW,
    }
    with pytest.raises(ContractError) as caught:
        check_result_context(result, **kwargs)
    assert caught.value.error.code == ErrorCode.STALE_CONTEXT


def test_lease_result_boundary():
    lease, result = lease_and_result()
    kwargs = {"scope": decode(Scope, SCOPE), "freshness": decode(Freshness, FRESH), "lease": lease}
    check_result_context(result, now=NOW, **kwargs)
    with pytest.raises(ContractError) as caught:
        check_result_context(result, now=lease.expires_at, **kwargs)
    assert caught.value.error.code == ErrorCode.EXPIRED
    for change in (
        {"lease_version": 3},
        {"attempt_id": "attempt-02"},
        {"operation_id": "operation-02"},
        {"command_id": "command-02"},
    ):
        old = decode(ExecutionResult, {**result.model_dump(mode="json"), **change})
        with pytest.raises(ContractError) as caught:
            check_result_context(old, now=NOW, **kwargs)
        assert caught.value.error.code == ErrorCode.STALE_CONTEXT


def test_run_dimensions_and_preliminary_stream():
    run = decode(
        RunSession,
        {
            **BASE,
            "agent_id": "agent-01",
            "run_id": "run-01",
            "session_id": "session-01",
            "state": "ACTIVE",
            "autonomy": "observation",
            "activity": "PAUSED",
            "voice_enabled": False,
            "gm_paused": True,
            "control_epoch": 3,
        },
    )
    assert run.state == RunState.ACTIVE and run.activity == "PAUSED"
    chunk = {
        "type": "generation.chunk",
        "id": "chunk-01",
        "scope": SCOPE,
        "created_at": NOW.isoformat(),
        "generation_id": "generation-01",
        "utterance_id": "utterance-01",
        "attempt_id": "attempt-01",
        "sequence": 1,
        "data_type": "json_fragment",
        "data": '{"tool":',
        "preliminary": True,
    }
    value = decode(StreamChunk, chunk)
    assert value.preliminary is True
    assert_invalid(PanelCommand, chunk)
    assert_invalid(StreamChunk, {**chunk, "preliminary": False})


class BigOutput(Contract):
    items: list[Text] = Field(max_length=200)


def test_outbound_limit_and_mutated_nested_values():
    value = BigOutput(items=["x" * 16000] * 70)
    with pytest.raises(ContractError):
        to_wire(value)
    command = decode(MessageCommand, MESSAGE)
    # Models are shallowly frozen; outbound validation still rejects nested mutation.
    page = Page[MessageCommand](items=[command], limit=1, next_cursor=None, complete=True)
    page.items.append(command)
    with pytest.raises(ContractError):
        to_wire(page)


class SyntheticText(Contract):
    content: str


def test_extension_size_limit(registry):
    registry.register("synthetic.text", SyntheticText)
    value = edit()
    value["arguments"] = {"schema_id": "synthetic.text", "data": {"content": "x" * 65537}}
    assert_invalid(PanelCommand, value, registry)


def test_edit_revision_is_part_of_idempotency(registry):
    previous = decode(PanelCommand, edit(), registry)
    incoming = decode(PanelCommand, {**edit(), "expected_version": 2}, registry)
    with pytest.raises(ContractError) as caught:
        check_replay(previous, incoming)
    assert caught.value.error.code == ErrorCode.IDEMPOTENCY_CONFLICT


def test_persona_and_applied_change_versions(registry):
    from ai_agent_contracts.persona import CharacterState, Profile, StateChange

    extension = {"schema_id": "synthetic.arguments", "data": {"score": 2}}
    profile = {
        **BASE,
        "persona_id": SCOPE["persona_id"],
        "canon": "Вымышленный канон.",
        "style": "Пример.",
        "property_definitions": extension,
        "initial_values": extension,
        "development_bounds": extension,
        "accepted_revision_id": "profile-revision-01",
    }
    decode(Profile, profile, registry)
    assert_invalid(Profile, {**profile, "persona_id": "foreign-persona"}, registry)
    state = {
        **BASE,
        "persona_id": SCOPE["persona_id"],
        "profile_version": 1,
        "state_version": 1,
        "values": extension,
    }
    decode(CharacterState, state, registry)
    assert_invalid(CharacterState, {**state, "state_version": 2}, registry)
    change = {
        **BASE,
        "proposal_id": "proposal-01",
        "owner": "persona-owner",
        "base_version": 1,
        "patch": extension,
        "evidence": [REF],
        "interpretation": "Вымышленный опыт.",
        "uncertainty": 0.5,
        "expected_manifestation": "Наблюдаемый эффект.",
        "status": "READY",
    }
    decode(StateChange, change, registry)
    assert_invalid(StateChange, {**change, "new_version": 2}, registry)
    assert_invalid(StateChange, {**change, "status": "APPLIED"}, registry)
    decode(StateChange, {**change, "status": "APPLIED", "new_version": 2}, registry)


def test_waiting_and_running_have_conditions_and_current_lease(registry):
    from ai_agent_contracts.control import Job

    lease, _ = lease_and_result()
    extension = {"schema_id": "synthetic.arguments", "data": {"score": 2}}
    job = {
        **BASE,
        "operation_id": "operation-01",
        "command_id": "command-01",
        "goal": "Вымышленная задача.",
        "capability_id": "synthetic",
        "capability_version": 1,
        "status": "WAITING",
        "deadline": (NOW + timedelta(seconds=30)).isoformat(),
        "conditions": extension,
        "freshness": FRESH,
    }
    assert_invalid(Job, job, registry)
    decode(Job, {**job, "waiting_reason": "resource", "wake_event": "resource.free"}, registry)
    assert_invalid(Job, {**job, "status": "RUNNING"}, registry)
    decode(Job, {**job, "status": "RUNNING", "lease": lease.model_dump(mode="json")}, registry)
    assert_invalid(
        Job,
        {
            **job,
            "status": "RUNNING",
            "lease": {**lease.model_dump(mode="json"), "operation_id": "foreign-operation"},
        },
        registry,
    )


def test_moderation_revision_and_distinct_models():
    from ai_agent_contracts.control import ModerationCheck, Utterance

    check = {
        "id": "check-01",
        "scope": SCOPE,
        "created_at": NOW.isoformat(),
        "utterance_id": "utterance-01",
        "revision": 1,
        "content_hash": "a" * 64,
        "audience_id": "audience-01",
        "policy_version": 1,
        "context_hash": "b" * 64,
        "freshness": FRESH,
        "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
        "subject_model_id": "subject-model",
        "moderator_model_id": "moderator-model",
        "decision": "approved",
        "reason_code": "allowed",
    }
    decode(ModerationCheck, check)
    assert_invalid(ModerationCheck, {**check, "moderator_model_id": "subject-model"})
    utterance = {
        **BASE,
        "utterance_id": "utterance-01",
        "revision": 1,
        "content": "Пример.",
        "content_hash": hashlib.sha256("Пример.".encode()).hexdigest(),
        "audience_id": "audience-01",
        "channel_ids": ["private-panel"],
        "moderation_status": "approved",
    }
    assert_invalid(Utterance, utterance)
    decode(Utterance, {**utterance, "moderation_check_id": "check-01"})


def test_worker_commands_have_separate_discriminator():
    from ai_agent_contracts.api import ServiceCommand

    lease, result = lease_and_result()
    message = {
        "type": "job.result",
        "request_id": "worker-result-01",
        "scope": SCOPE,
        "target": "operation-01",
        "result": result.model_dump(mode="json"),
    }
    decode(ServiceCommand, message)
    assert_invalid(PanelCommand, message)
    assert_invalid(ServiceCommand, MESSAGE)
    assert_invalid(
        ServiceCommand,
        {
            "type": "job.renew",
            "request_id": "lease-01",
            "scope": SCOPE,
            "target": "operation-01",
            "operation_id": "operation-01",
            "lease": {**lease.model_dump(mode="json"), "scope": {**SCOPE, "memory_mode": "work"}},
        },
    )


def test_safe_connection_and_laboratory_scope():
    from ai_agent_contracts.llm import Connection
    from ai_agent_contracts.workspace import LaboratoryReport

    connection = {
        **BASE,
        "name": "Synthetic connection",
        "adapter_id": "synthetic",
        "adapter_version": "1.0",
        "endpoint_setting": "AI_PLATFORM_LLM_ENDPOINT",
        "credential_setting": "AI_PLATFORM_LLM_API_KEY",
        "configured": False,
        "verification": "unverified",
        "capabilities": [],
    }
    decode(Connection, connection)
    assert_invalid(Connection, {**connection, "endpoint_setting": "https://synthetic.invalid"})
    assert_invalid(Connection, {**connection, "api_key": "PUBLIC_DUMMY_NOT_REAL"})
    report = {
        **BASE,
        "scenario_id": "scenario-01",
        "scenario_version": 1,
        "operation_id": "operation-01",
        "status": "pending",
        "report_artifact": None,
        "summary": "Синтетическая проба.",
    }
    decode(LaboratoryReport, report)
    assert_invalid(LaboratoryReport, {**report, "scope": {**SCOPE, "memory_mode": "work"}})


def test_stop_receipt_never_claims_unconfirmed_resource_stopped():
    from ai_agent_contracts.api import StopReceipt

    value = {
        "request_id": "stop-01",
        "scope": SCOPE,
        "status": "accepted",
        "resource_id": "agent-01",
        "state": "STOPPING",
        "control_epoch": 4,
        "run_id": "run-01",
        "unconfirmed_resource_ids": ["resource-01"],
    }
    decode(StopReceipt, value)
    assert_invalid(StopReceipt, {**value, "state": "STOPPED"})
    assert_invalid(StopReceipt, {**value, "state": "ACTIVE", "unconfirmed_resource_ids": []})
    decode(StopReceipt, {**value, "state": "STOPPED", "unconfirmed_resource_ids": []})


def test_worker_outer_scope_and_target_match_result():
    from ai_agent_contracts.api import ServiceCommand

    _, result = lease_and_result()
    value = {
        "type": "job.result",
        "request_id": "result-request",
        "scope": SCOPE,
        "target": "operation-01",
        "result": result.model_dump(mode="json"),
    }
    assert_invalid(ServiceCommand, {**value, "scope": {**SCOPE, "run_id": "old-run"}})
    assert_invalid(ServiceCommand, {**value, "target": "other-operation"})


def test_deleted_recall_fragment_cannot_carry_old_content():
    from ai_agent_contracts.memory import RecallFragment

    value = {"source": REF, "content": "Вымышленный фрагмент.", "attribution": "source-01"}
    decode(RecallFragment, value)
    assert_invalid(RecallFragment, {**value, "source": {**REF, "availability": "deleted"}})


def test_boolean_true_flags_do_not_accept_numeric_one():
    from ai_agent_contracts.common import TrueFlag

    assert from_wire(TypeAdapter(TrueFlag), b"true") is True
    with pytest.raises(ContractError):
        from_wire(TypeAdapter(TrueFlag), b"1")


def test_simulated_source_cannot_enter_work_biography():
    value = {
        **BASE,
        "id": "source-01",
        "source_id": "source-01",
        "provenance": "simulation",
        "source_type": "message",
        "adapter_id": "synthetic",
        "adapter_version": "1.0",
        "occurred_at": NOW.isoformat(),
        "registered_at": NOW.isoformat(),
        "retention": {"policy_id": "test-retention", "expires_at": None},
        "availability": "available",
        "content": "Вымышленная симуляция.",
        "read_allowed": True,
        "api_transfer_allowed": False,
        "public_disclosure_allowed": False,
    }
    decode(Source, value)
    assert_invalid(Source, {**value, "scope": {**SCOPE, "memory_mode": "work"}})
