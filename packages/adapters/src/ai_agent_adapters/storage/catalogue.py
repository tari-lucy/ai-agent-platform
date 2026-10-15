"""A closed physical type/owner registry; it does not provide business CRUD handlers."""

import hashlib
import json
from dataclasses import dataclass

from ai_agent_application.storage import Owner
from ai_agent_contracts import api, control, llm, memory, panel, persona, workspace
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
}


def semantic_keys(record: Record) -> list[tuple[str, str]]:
    """Normalized exact uniqueness; nicknames and matching text never identify people."""
    values: list[tuple[str, object]] = []
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
