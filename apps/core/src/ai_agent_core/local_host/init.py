"""Explicit one-time preparation of a new local host directory (trusted operator command).

Applies the existing migrations and prepares only what the accepted owners need: profile and
state through PersonaService, Workspace/GM/Connection/LLMAssignment/Capability/Permission through
their owner transaction helper, scope grants, and a pool of empty TEST scopes. It never
overwrites anything and never runs the agent. HTTP cannot reach any of it.
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.persona import PersonaService
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.api import CreateCommand, Permission
from ai_agent_contracts.codec import from_wire
from ai_agent_contracts.common import Record, Scope
from ai_agent_contracts.extensions import ExtensionRegistry
from ai_agent_contracts.llm import Connection, LLMAssignment
from ai_agent_contracts.persona import GmRuntimeState
from ai_agent_contracts.workspace import Capability, Workspace
from pydantic import SecretStr, TypeAdapter, ValidationError

from ai_agent_core.access import password_hash
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import (
    PRINCIPALS,
    ROLES,
    extension,
    put,
    record,
)

from . import rights
from .layout import (
    MAX_TEST_SCOPES,
    HostConfig,
    HostError,
    LiveConfig,
    live_roles,
    new_password,
    paths,
    private_dir,
    write_secret,
)

MAX_PERMISSION_DAYS = 90
PROFILE_LIMIT = 128 * 1024


def default_migrations() -> Path:
    """Repository migrations next to the installed sources; never cwd-relative."""
    found = Path(__file__).resolve().parents[5] / "migrations"
    if not (found / "env.py").is_file():
        raise HostError("MIGRATIONS_NOT_FOUND")
    return found


def work_scope(workspace_id: str, persona_id: str, space_id: str) -> Scope:
    return Scope(
        workspace_id=workspace_id,
        persona_id=persona_id,
        memory_space_id=space_id,
        memory_mode="work",
    )


def test_scope(work: Scope, space_id: str) -> Scope:
    return work.model_copy(update={"memory_mode": "test", "memory_space_id": space_id})


async def initialise(
    root: Path,
    *,
    profile_markdown: str,
    workspace_id: str = "local",
    persona_id: str = "persona",
    port: int = 8765,
    test_scopes: int = 4,
    permission_days: int = 30,
    migrations: Path | None = None,
    operator_controls: bool = False,
    live: LiveConfig | None = None,
) -> HostConfig:
    if not 1 <= test_scopes <= MAX_TEST_SCOPES or not 1 <= permission_days <= MAX_PERMISSION_DAYS:
        raise HostError("INVALID_OPTION")
    if not profile_markdown.strip() or len(profile_markdown) > PROFILE_LIMIT:
        raise HostError("PROFILE_INVALID")
    host = paths(root)
    if host.root.exists() and any(host.root.iterdir()):
        raise HostError("ROOT_NOT_EMPTY")  # never overwrite a profile, database or secret
    directory = migrations or default_migrations()
    now = datetime.now(UTC)
    expires = now + timedelta(days=permission_days)
    work_id = persona_id + "-work"
    test_ids = tuple(f"{persona_id}-test-{n}" for n in range(1, test_scopes + 1))
    settings_id = persona_id + "-settings-test" if operator_controls else None
    try:
        config = HostConfig(
            workspace_id=workspace_id,
            persona_id=persona_id,
            work_space_id=work_id,
            test_space_ids=test_ids,
            port=port,
            permission_expires_at=expires.isoformat(),
            operator_controls=operator_controls,
            settings_space_id=settings_id,
            live=live,
        )
    except ValidationError:
        raise HostError("INVALID_OPTION") from None
    work = work_scope(workspace_id, persona_id, work_id)
    tests = tuple(test_scope(work, space) for space in test_ids)

    private_dir(host.root)
    write_secret(host.unfinished, "init in progress; this directory is not usable\n")
    private_dir(host.secrets)
    private_dir(host.database.parent)
    private_dir(host.lab_indexes)
    private_dir(host.reports)
    password = new_password()
    write_secret(host.password, password + "\n")
    write_secret(host.password_hash, password_hash(SecretStr(password)).get_secret_value() + "\n")

    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    try:
        await store.upgrade(directory)
        await store.provision(work, rights.work_grants(preparing=True))
        readers = rights.record_readers()
        persona = PersonaService(store, PRINCIPALS[Owner.PERSONA], registry, reader_access=readers)
        create = {
            "request_id": "local-host-create-profile",
            "scope": work.model_dump(mode="json"),
            "target": persona_id,
            "type": "persona.create",
            "arguments": {
                "schema_id": "persona.create-args",
                "schema_version": "1.0",
                "data": {"markdown": profile_markdown},
            },
        }
        await persona.create(
            from_wire(TypeAdapter(CreateCommand), json.dumps(create).encode(), extensions=registry)
        )
        await _prepare_records(store, registry, work, expires, live=live)
        # Only explicit init controls mode retains WORKSPACE writes for reviewed plans.
        await store.provision(work, rights.work_grants(operator_controls=operator_controls))
        if settings_id is not None:
            settings = test_scope(work, settings_id)
            await store.provision(settings, rights.settings_grants())
            # A new independent initial state: no WORK memory/relationships/live values copied.
            sample = dict(
                create,
                request_id="local-host-settings-profile",
                scope=settings.model_dump(mode="json"),
            )
            await persona.create(
                from_wire(
                    TypeAdapter(CreateCommand), json.dumps(sample).encode(), extensions=registry
                )
            )
            now = datetime.now(UTC)
            gm = record(
                registry,
                GmRuntimeState,
                id="gm-runtime",
                scope=settings,
                version=1,
                created_at=now,
                updated_at=now,
                intensity=0.5,
                active_seconds=0,
                rules_version=1,
                rules=extension(registry, "m3.rules", {"rules": []}),
            )
            await put(
                store, registry, Owner.GM, settings, "local-host-settings-gm", gm, None, readers
            )
        for scope in tests:
            await store.provision(scope, rights.lab_scope_grants())
    finally:
        await store.close()
    temporary = host.root / "host.json.tmp"
    write_secret(temporary, config.model_dump_json(indent=1) + "\n")
    os.replace(temporary, host.config)
    host.unfinished.unlink()  # last step: only now is the directory a finished host
    return config


async def _prepare_records(
    store: SQLiteStore,
    registry: ExtensionRegistry,
    scope: Scope,
    expires: datetime,
    *,
    live: LiveConfig | None = None,
) -> None:
    now = datetime.now(UTC)
    common = dict(scope=scope, created_at=now, updated_at=now, version=1)
    readers = rights.record_readers()
    llm_access = (RecordAccess(PRINCIPALS[Owner.LLM].id),)
    records: list[tuple[Owner, Record, tuple[RecordAccess, ...]]] = [
        (
            Owner.GM,
            record(
                registry,
                GmRuntimeState,
                id="gm-runtime",
                **common,
                intensity=0.5,
                active_seconds=0,
                rules_version=1,
                rules=extension(registry, "m3.rules", {"rules": []}),
            ),
            readers,
        ),
        (
            Owner.WORKSPACE,
            record(
                registry,
                Workspace,
                id=scope.workspace_id,
                **common,
                policy_version=1,
                settings=extension(registry, "m3.empty", {}),
                active_agent_id=None,
                profile_ids=[scope.persona_id],
                connection_ids=["conn-mock", *(["conn-live"] if live else [])],
            ),
            llm_access,
        ),
        (
            Owner.WORKSPACE,
            record(
                registry,
                Connection,
                id="conn-mock",
                **common,
                name="Technical mock",
                adapter_id="mock",
                adapter_version="1",
                # Names of settings only; this host never reads their values.
                endpoint_setting="AI_PLATFORM_MOCK_ENDPOINT",
                credential_setting="AI_PLATFORM_MOCK_CREDENTIAL",
                configured=True,
                verification="unverified",
                capabilities=[],
            ),
            llm_access,
        ),
    ]
    if live is not None:
        from ai_agent_adapters.llm import ADAPTER_ID

        records.append(
            (
                Owner.WORKSPACE,
                record(
                    registry,
                    Connection,
                    id="conn-live",
                    **common,
                    name="Private API pilot",
                    adapter_id=ADAPTER_ID,
                    adapter_version="1",
                    endpoint_setting="AI_PLATFORM_LLM_ENDPOINT",
                    credential_setting="AI_PLATFORM_LLM_API_KEY",
                    configured=True,
                    verification="unverified",
                    capabilities=[],
                ),
                llm_access,
            )
        )
    for role in sorted({r for roles in ROLES.values() for r in roles}):
        selected = live if role in live_roles(live) else None
        records.append(
            (
                Owner.LLM,
                record(
                    registry,
                    LLMAssignment,
                    id="asg-" + role,
                    **common,
                    role_id=role,
                    connection_id="conn-live" if selected else "conn-mock",
                    model_id=selected.model_id if selected else "mock-" + role,
                    parameters=extension(
                        registry,
                        "llm.generation-parameters",
                        {
                            "max_output_tokens": selected.max_output_tokens if selected else 500,
                            "timeout_seconds": selected.timeout_seconds if selected else 5.0,
                        },
                    ),
                    required_capabilities=[],
                    capability_verification="unverified",
                ),
                llm_access,
            )
        )
    for kind in ROLES:
        records.append(
            (
                Owner.WORKSPACE,
                record(
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
                    resource_ids=["background"] if kind == "laboratory" else ["background", "llm"],
                    timeout_ms=180000,
                    cancellation_supported=True,
                    idempotency="supported",
                    health="ready",
                    setting_references=[],
                ),
                llm_access,
            )
        )
        records.append(
            (
                Owner.WORKSPACE,
                record(
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
                    expires_at=expires,
                ),
                readers,
            )
        )
    for owner, item, access in records:
        await put(store, registry, owner, scope, "local-host-" + item.id, item, None, access)
