"""Binding of a role to a connection and model; refuses silent or unverified-failed bindings."""

from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import Connection, LLMAssignment

from .model import Failure, GenerationParameters, ProviderError, ResolvedAssignment

MAX_FALLBACK_DEPTH = 2


def resolve_assignment(
    assignment: LLMAssignment,
    connection: Connection,
    registry: ExtensionRegistry,
    fallback: ResolvedAssignment | None = None,
) -> ResolvedAssignment:
    """Check one assignment against its connection.

    Raises ``ProviderError(UNSUPPORTED)`` when the binding cannot serve the role: another scope or
    connection, an unconfigured or failed connection, a missing required capability or foreign
    generation parameters. ``unverified`` is allowed (verification needs a live call that
    needs separate authorisation) and stays visible on the result.
    """
    if (
        assignment.connection_id != connection.id
        or assignment.scope.partition != connection.scope.partition
        or not connection.configured
        or connection.verification == "failed"
        or assignment.capability_verification == "failed"
        or not set(assignment.required_capabilities) <= set(connection.capabilities)
        or assignment.parameters.schema_id != "llm.generation-parameters"
    ):
        raise ProviderError(Failure.UNSUPPORTED)
    parameters = registry.validate(
        assignment.parameters.schema_id,
        assignment.parameters.schema_version,
        assignment.parameters.data,
    )
    if not isinstance(parameters, GenerationParameters):
        raise ProviderError(Failure.UNSUPPORTED)
    if fallback is not None and (
        fallback.role_id != assignment.role_id
        or assignment.fallback_assignment_id != fallback.assignment_id
    ):
        raise ProviderError(Failure.UNSUPPORTED)
    return ResolvedAssignment(
        assignment_id=assignment.id,
        assignment_version=assignment.version,
        role_id=assignment.role_id,
        connection_id=connection.id,
        adapter_id=connection.adapter_id,
        model_id=assignment.model_id,
        endpoint_setting=connection.endpoint_setting,
        credential_setting=connection.credential_setting,
        parameters=parameters,
        capability_verification=assignment.capability_verification,
        fallback=fallback,
    )
