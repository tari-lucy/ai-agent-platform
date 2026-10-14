"""Explicit synthetic preparation. Production runtime never provisions these grants."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

from ai_agent_adapters.control.journal import UnavailableJournal, UnavailableReadiness
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import Clock, ControlAuthority
from ai_agent_application.scheduling import ResourceScheduler
from ai_agent_application.storage import Grant, Owner, RecordAccess
from ai_agent_application.telemetry import Telemetry
from ai_agent_contracts.api import ControlArguments, ControlCommand, Permission, StopCommand
from ai_agent_contracts.llm import Connection, LLMAssignment
from ai_agent_contracts.persona import GmRuntimeState
from ai_agent_contracts.workspace import Capability, Workspace
from dev_support import GRANTS, new_world

from ai_agent_core.m3_composition.schemas import register
from ai_agent_core.m3_composition.service import Runtime
from ai_agent_core.m3_composition.state import (
    PRINCIPALS,
    ROLES,
    extension,
    put,
)
from ai_agent_core.m3_composition.state import (
    record as validate_record,
)


async def setup(tmp_path, scripts=(), *, start=True):
    world = await new_world(tmp_path, list(scripts))
    register(world.registry)
    from ai_agent_application.llm import register_llm_extensions

    register_llm_extensions(world.registry)
    store, scope, registry = world.store, world.scope, world.registry
    grants = tuple(
        Grant(
            p, o, read=True, write=True, api_transfer=o in (Owner.MEMORY, Owner.PERSONA, Owner.GM)
        )
        for o, p in PRINCIPALS.items()
    )
    grants += (
        Grant(PRINCIPALS[Owner.JOBS], Owner.BEHAVIOR, read=True),
        Grant(PRINCIPALS[Owner.LLM], Owner.WORKSPACE, read=True),
        Grant(PRINCIPALS[Owner.CATALOGUE], Owner.MEMORY, read=True),
    )
    grants += tuple(
        Grant(PRINCIPALS[o], Owner.MEMORY, read=True, api_transfer=o == Owner.GM)
        for o in (Owner.BEHAVIOR, Owner.JOBS, Owner.PERSONA, Owner.GM)
    )
    readers = tuple(
        RecordAccess(PRINCIPALS[o].id, api_transfer=o == Owner.GM)
        for o in (Owner.BEHAVIOR, Owner.JOBS, Owner.PERSONA, Owner.GM, Owner.CATALOGUE)
    )
    world.memory.reader_access = world.memory.reader_access + readers
    await store.provision(scope, GRANTS + grants)
    now = world.clock()
    common = dict(scope=scope, created_at=now, updated_at=now, version=1)
    records = [
        (
            Owner.GM,
            validate_record(
                registry,
                GmRuntimeState,
                id="gm-runtime",
                **common,
                intensity=0.5,
                active_seconds=0,
                rules_version=1,
                rules=extension(registry, "m3.rules", {"rules": []}),
            ),
        ),
        (
            Owner.WORKSPACE,
            validate_record(
                registry,
                Workspace,
                id=scope.workspace_id,
                **common,
                policy_version=1,
                settings=extension(registry, "m3.empty", {}),
                active_agent_id=None,
                profile_ids=[scope.persona_id],
                connection_ids=["conn-a"],
            ),
        ),
        (
            Owner.WORKSPACE,
            validate_record(
                registry,
                Connection,
                id="conn-a",
                **common,
                name="Synthetic",
                adapter_id="mock",
                adapter_version="1",
                endpoint_setting="AI_PLATFORM_SYNTHETIC_E",
                credential_setting="AI_PLATFORM_SYNTHETIC_K",
                configured=True,
                verification="unverified",
                capabilities=[],
            ),
        ),
    ]
    for role in sorted({r for roles in ROLES.values() for r in roles}):
        records.append(
            (
                Owner.LLM,
                validate_record(
                    registry,
                    LLMAssignment,
                    id="asg-" + role,
                    **common,
                    role_id=role,
                    connection_id="conn-a",
                    model_id="mock-" + role,
                    parameters=extension(
                        registry,
                        "llm.generation-parameters",
                        {"max_output_tokens": 500, "timeout_seconds": 5.0},
                    ),
                    required_capabilities=[],
                    capability_verification="unverified",
                ),
            )
        )
    for kind in ROLES:
        records.extend(
            [
                (
                    Owner.WORKSPACE,
                    validate_record(
                        registry,
                        Capability,
                        id="m3." + kind,
                        **common,
                        adapter_id="m3-core",
                        adapter_version="1",
                        input_schema_id="m3.job",
                        output_schema_id="m3.result",
                        effect="local_write",
                        required_permissions=["permit-" + kind],
                        resource_ids=["background"]
                        if kind == "laboratory"
                        else ["background", "llm"],
                        timeout_ms=180000,
                        cancellation_supported=True,
                        idempotency="supported",
                        health="ready",
                        setting_references=[],
                    ),
                ),
                (
                    Owner.WORKSPACE,
                    validate_record(
                        registry,
                        Permission,
                        id="permit-" + kind,
                        **common,
                        principal_id="operator",
                        capability_id="m3." + kind,
                        operation_type="m3." + kind,
                        binding_id="m3-core",
                        level="automatic",
                        conditions=extension(registry, "m3.empty", {}),
                        limits=extension(registry, "m3.limits", {}),
                        policy_version=1,
                        author_id="operator",
                        expires_at=now + timedelta(hours=1),
                    ),
                ),
            ]
        )
    for owner, record in records:
        await put(
            store,
            registry,
            owner,
            scope,
            "prepare-" + record.id,
            record,
            None,
            (RecordAccess(PRINCIPALS[Owner.LLM].id),),
        )
    elapsed = SimpleNamespace(value=0.0)
    clock = Clock(lambda: elapsed.value, world.clock)

    def build():
        supervisor = Supervisor(
            scope, "primary", UnavailableJournal(), UnavailableReadiness(), clock=clock
        )
        scheduler = ResourceScheduler(supervisor, Telemetry(clock=clock))
        runtime = Runtime(
            store,
            registry,
            supervisor,
            scheduler,
            lambda _: world.provider,
            scopes=(scope,),
            reader_access=readers,
        )
        return runtime

    runtime = build()
    await runtime.open()
    authority = ControlAuthority("operator", frozenset({scope.partition}), 3600)

    async def activate(rt=runtime):
        await rt.supervisor.control(
            authority,
            ControlCommand(
                type="agent.start",
                request_id="start",
                target="primary",
                scope=rt.supervisor.snapshot.scope,
                arguments=ControlArguments(autonomy="autonomous"),
                expected_version=rt.supervisor.snapshot.version,
            ),
        )

    if start:
        await activate()
    return SimpleNamespace(
        world=world,
        runtime=runtime,
        authority=authority,
        build=build,
        activate=activate,
        grants=grants,
        elapsed=elapsed,
    )


def stop(env):
    rt = env.runtime
    return rt.supervisor.stop(
        env.authority,
        StopCommand(
            type="agent.stop",
            request_id="stop",
            target="primary",
            scope=rt.supervisor.snapshot.scope,
        ),
    )


def run(value):
    return asyncio.run(value)
