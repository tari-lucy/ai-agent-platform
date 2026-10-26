"""A closed physical type/owner registry; it does not provide business CRUD handlers."""

import hashlib
import json
from dataclasses import dataclass

from ai_agent_application.storage import Owner
from ai_agent_contracts import (
    agent_sessions,
    agent_workplace,
    api,
    control,
    llm,
    memory,
    panel,
    persona,
    workplace_access,
    workspace,
)
from ai_agent_contracts.common import Record


@dataclass(frozen=True)
class Spec:
    model: type[Record]
    owner: Owner | None


SPECS = {
    "Workspace": Spec(workspace.Workspace, Owner.WORKSPACE),
    "Capability": Spec(workspace.Capability, Owner.WORKSPACE),
    "Connection": Spec(llm.Connection, Owner.WORKSPACE),
    "Permission": Spec(api.Permission, Owner.WORKSPACE),
    "Profile": Spec(persona.Profile, Owner.PERSONA),
    "CharacterState": Spec(persona.CharacterState, Owner.PERSONA),
    "StateChange": Spec(persona.StateChange, None),
    "RunSession": Spec(control.RunSession, Owner.SUPERVISOR),
    "Briefing": Spec(persona.Briefing, Owner.SUPERVISOR),
    "Source": Spec(memory.Source, Owner.MEMORY),
    "MessageEnvelope": Spec(control.MessageEnvelope, Owner.MEMORY),
    "Episode": Spec(memory.Episode, Owner.MEMORY),
    "MemoryCycle": Spec(memory.MemoryCycle, Owner.MEMORY),
    "Claim": Spec(memory.Claim, Owner.MEMORY),
    "Summary": Spec(memory.Summary, Owner.MEMORY),
    "DeletionTombstone": Spec(workspace.DeletionTombstone, Owner.MEMORY),
    "Person": Spec(memory.Person, Owner.SOCIAL),
    "Encounter": Spec(memory.Encounter, Owner.SOCIAL),
    "Relationship": Spec(memory.Relationship, Owner.SOCIAL),
    "Topic": Spec(memory.Topic, Owner.SOCIAL),
    "Promise": Spec(memory.Promise, Owner.SOCIAL),
    "ActionProposal": Spec(control.ActionProposal, Owner.BEHAVIOR),
    "ExecutionCommand": Spec(control.ExecutionCommand, Owner.BEHAVIOR),
    "Job": Spec(control.Job, Owner.JOBS),
    "ExecutionResult": Spec(control.ExecutionResult, Owner.JOBS),
    "OperationProjection": Spec(control.OperationProjection, Owner.JOBS),
    "Utterance": Spec(control.Utterance, Owner.GATEWAY),
    "ModerationCheck": Spec(control.ModerationCheck, Owner.MODERATION),
    "Delivery": Spec(control.Delivery, Owner.CHANNEL),
    "StoryArc": Spec(persona.StoryArc, Owner.GM),
    "GmRuntimeState": Spec(persona.GmRuntimeState, Owner.GM),
    "ReflectionCheckpoint": Spec(persona.ReflectionCheckpoint, Owner.PERSONA),
    "OwnerOperation": Spec(panel.OwnerOperation, Owner.WORKSPACE),
    "ApiLimitChange": Spec(panel.ApiLimitChange, Owner.WORKSPACE),
    "MemoryPlanProof": Spec(panel.MemoryPlanProof, Owner.WORKSPACE),
    "RollbackProof": Spec(panel.RollbackProof, Owner.WORKSPACE),
    "SettingsTransferPreview": Spec(panel.SettingsTransferPreview, Owner.WORKSPACE),
    "SettingsTransferOperation": Spec(persona.SettingsTransferOperation, Owner.WORKSPACE),
    "Opportunity": Spec(persona.Opportunity, Owner.GM),
    "LLMAssignment": Spec(llm.LLMAssignment, Owner.LLM),
    "LLMAttempt": Spec(llm.LLMAttempt, Owner.LLM),
    "Tariff": Spec(llm.Tariff, Owner.LLM),
    "CostCalculation": Spec(llm.CostCalculation, Owner.LLM),
    "Artifact": Spec(workspace.Artifact, Owner.CATALOGUE),
    "LaboratoryReport": Spec(workspace.LaboratoryReport, Owner.LABORATORY),
    "LaboratoryScopeReservation": Spec(workspace.LaboratoryScopeReservation, Owner.LABORATORY),
    "ConsequencePlan": Spec(api.ConsequencePlan, None),
    "CapabilityManifest": Spec(agent_workplace.CapabilityManifest, Owner.WORKSPACE),
    "CapabilityBinding": Spec(agent_workplace.CapabilityBinding, Owner.WORKSPACE),
    "WorkTask": Spec(agent_workplace.WorkTask, Owner.ACTIVITY),
    "WorkSession": Spec(agent_sessions.WorkSession, Owner.ACTIVITY),
    "WorkSessionMessage": Spec(agent_sessions.WorkSessionMessage, Owner.ACTIVITY),
    "WakeTimer": Spec(agent_sessions.WakeTimer, Owner.ACTIVITY),
    "WorkAttempt": Spec(agent_workplace.WorkAttempt, Owner.ACTIVITY),
    "ManagedFile": Spec(agent_workplace.ManagedFile, Owner.CATALOGUE),
    "SkillVersion": Spec(agent_workplace.SkillVersion, Owner.SKILLS),
    "Ticket": Spec(agent_workplace.Ticket, Owner.TICKETS),
    "TicketMessage": Spec(agent_workplace.TicketMessage, Owner.TICKETS),
    "PermissionDecisionProof": Spec(agent_workplace.PermissionDecisionProof, Owner.WORKSPACE),
    "InitialAccessProof": Spec(workplace_access.InitialAccessProof, Owner.WORKSPACE),
}


def semantic_keys(record: Record) -> list[tuple[str, str]]:
    """Normalized exact uniqueness; nicknames and matching text never identify people."""
    values: list[tuple[str, object]] = []
    if isinstance(record, agent_sessions.WorkSessionMessage):
        values.append(("session-sequence", [record.session_id, record.sequence]))
        values.append(("session-source", [record.session_id, record.source.source_id]))
    if isinstance(record, memory.Source) and record.external_id is not None:
        values.append(
            ("external-source", [record.source_type, record.adapter_id, record.external_id])
        )
    if isinstance(record, memory.Person):
        values.append(("person", record.person_id))
        values.extend(
            ("platform-account", [link.platform_id, link.account_id])
            for link in record.accounts
            if link.confirmation == "confirmed"
        )
    if isinstance(record, memory.Relationship):
        values.append(("relationship", record.person_id))
    if isinstance(record, memory.Topic):
        values.append(("topic", record.topic_id))
    if isinstance(record, control.Job | control.OperationProjection):
        # Separate projections may share an operation; one row of each kind only.
        values.append((type(record).__name__ + "-operation", record.operation_id))
    if isinstance(record, llm.LLMAttempt):
        values.append(("llm-attempt", record.attempt_id))
    if isinstance(record, control.ExecutionResult):
        values.append(
            ("attempt-result", [record.operation_id, record.attempt_id, record.lease_version])
        )
    if isinstance(record, control.Delivery):
        values.append(("delivery-attempt", [record.channel_id, record.attempt_id]))
    if isinstance(record, persona.CharacterState):
        values.append(("character-state", record.persona_id))
    if isinstance(record, agent_workplace.WorkAttempt):
        if record.session_id is not None:
            values.append(
                ("session-attempt", [record.session_id, record.session_sequence, record.attempt_no])
            )
        else:
            values.append(("work-attempt", [record.task_id, record.attempt_no]))
    if isinstance(record, agent_workplace.ManagedFile):
        # One catalogue name per area; keys follow the current revision (rename frees a path).
        values.append(("managed-file-path", [record.area, record.path]))
    if isinstance(record, agent_workplace.Ticket):
        values.append(("ticket-duplicate", record.duplicate_key))
    if isinstance(record, agent_workplace.TicketMessage):
        values.append(("ticket-message", [record.ticket_id, record.sequence]))
    if isinstance(record, control.RunSession):
        values.append(("session", record.session_id))
    return [
        (
            namespace,
            hashlib.sha256(
                json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        )
        for namespace, value in values
    ]
