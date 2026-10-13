"""Fixtures for M2-04: scripted providers, invented ids, no network, no credentials."""

import asyncio
from datetime import UTC, datetime, timedelta

from ai_agent_application.llm import (
    ChatMessage,
    GenerationRequest,
    LLMGateway,
    ResolvedAssignment,
    register_llm_extensions,
    resolve_assignment,
)
from ai_agent_contracts.common import Contract, Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import Connection, LLMAssignment, TokenUsage

NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
SCOPE = Scope(
    workspace_id="ws-1", persona_id="persona-a", memory_space_id="mem-1", memory_mode="work"
)
REGISTRY = ExtensionRegistry()
register_llm_extensions(REGISTRY)


class OtherParameters(Contract):
    max_output_tokens: int


REGISTRY.register("llm.other-parameters", OtherParameters)

ENDPOINT_SETTING = "AI_PLATFORM_LLM_ENDPOINT_A"
CREDENTIAL_SETTING = "AI_PLATFORM_LLM_KEY_A"


def run(coroutine):
    return asyncio.run(coroutine)


def connection(
    name: str = "conn-a",
    *,
    adapter: str = "mock",
    configured: bool = True,
    verification: str = "unverified",
    capabilities: tuple[str, ...] = ("chat.stream",),
    endpoint: str = ENDPOINT_SETTING,
    credential: str = CREDENTIAL_SETTING,
    scope: Scope = SCOPE,
) -> Connection:
    return Connection.model_validate(
        {
            "id": name,
            "scope": scope.model_dump(),
            "created_at": NOW,
            "version": 1,
            "updated_at": NOW,
            "name": name,
            "adapter_id": adapter,
            "adapter_version": "1",
            "endpoint_setting": endpoint,
            "credential_setting": credential,
            "configured": configured,
            "verification": verification,
            "capabilities": list(capabilities),
        },
        context={"extensions": REGISTRY},
    )


def assignment(
    name: str = "asg-a",
    *,
    role: str = "character",
    connection_id: str = "conn-a",
    model: str = "model-x",
    required: tuple[str, ...] = ("chat.stream",),
    verification: str = "unverified",
    fallback: str | None = None,
    parameters: dict | None = None,
    schema_id: str = "llm.generation-parameters",
    scope: Scope = SCOPE,
) -> LLMAssignment:
    return LLMAssignment.model_validate(
        {
            "id": name,
            "scope": scope.model_dump(),
            "created_at": NOW,
            "version": 3,
            "updated_at": NOW,
            "role_id": role,
            "connection_id": connection_id,
            "model_id": model,
            "parameters": {
                "schema_id": schema_id,
                "data": parameters or {"max_output_tokens": 256, "timeout_seconds": 5},
            },
            "required_capabilities": list(required),
            "capability_verification": verification,
            "fallback_assignment_id": fallback,
        },
        context={"extensions": REGISTRY},
    )


def resolved(
    name: str = "asg-a",
    *,
    model: str = "model-x",
    adapter: str = "mock",
    parameters: dict | None = None,
    fallback: ResolvedAssignment | None = None,
    role: str = "character",
) -> ResolvedAssignment:
    return resolve_assignment(
        assignment(
            name,
            role=role,
            model=model,
            parameters=parameters,
            fallback=fallback.assignment_id if fallback else None,
        ),
        connection("conn-a", adapter=adapter),
        REGISTRY,
        fallback,
    )


def request(operation: str = "op-1", role: str = "character", result_model=None):
    return GenerationRequest(
        operation_id=operation,
        role_id=role,
        messages=(
            ChatMessage("system", "Ты — вымышленный тестовый персонаж."),
            ChatMessage("user", "Привет"),
        ),
        result_model=result_model,
    )


def usage(input_tokens=10, output_tokens=5, cached=None, reasoning=None) -> TokenUsage:
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_output_tokens=reasoning,
        source="provider",
        completeness="complete",
    )


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=10)
        return self.now


class Sleeps:
    """Records requested pauses instead of waiting."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        await asyncio.sleep(0)


def gateway(provider, assignment_=None, *, reports=None, sleeps=None, **options) -> LLMGateway:
    sleeps = sleeps if sleeps is not None else Sleeps()
    first = assignment_ or resolved()
    return LLMGateway(
        lambda adapter: provider if adapter in ("mock", "other") else _missing(adapter),
        lambda role: first,
        sink=reports.append if reports is not None else None,
        clock=Clock(),
        sleep=sleeps,
        jitter=lambda: 1.0,
        **options,
    )


def _missing(adapter: str):
    raise KeyError(adapter)
