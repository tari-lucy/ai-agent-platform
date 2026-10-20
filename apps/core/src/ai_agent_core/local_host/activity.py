"""Explicit offline switch of the workplace activity contour (AW-05); serve never grants it.

Enabling creates the ``m3.activity`` capability and its permit (the same shape init writes for
the other m3 kinds) and binds the ``activity`` role to the prepared live connection; then the
config flag lets serve run activity steps under the shared daily limit. Disabling returns the
role to its mock binding; the agent stays STOPPED either way. Requires operator tickets, since
a missing capability is asked for by ticket. Repeating after an interruption finishes it.
"""

from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.api import Permission
from ai_agent_contracts.llm import LLMAssignment
from ai_agent_contracts.workspace import Capability
from pydantic import ValidationError

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, ROLES, extension, put, record

from . import rights
from .gm import MOCK_PARAMETERS
from .init import work_scope
from .layout import (
    ActivityConfig,
    HostError,
    LiveConfig,
    load_config,
    paths,
    read_secret,
    save_config,
)
from .process import exclusive_host

KIND = "activity"


async def configure_activity(root: Path, **changes: object) -> ActivityConfig:
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host)
        read_secret(host.password_hash)  # proves the private prepared host boundary; never printed
        if config.live is None or not config.operator_controls or not config.workplace_tickets:
            raise HostError("ACTIVITY_CONTROLS_REQUIRED")
        try:
            settings = ActivityConfig.model_validate(
                {**config.live.activity.model_dump(), **changes}
            )
            live = LiveConfig.model_validate(
                {**config.live.model_dump(), "activity": settings.model_dump()}
            )
        except ValidationError:
            raise HostError("DAILY_ACTIVITY_BUDGET_REQUIRED") from None
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        now = datetime.now(UTC)
        common = dict(scope=scope, created_at=now, updated_at=now, version=1)
        llm_access = (RecordAccess(PRINCIPALS[Owner.LLM].id),)
        operator = (RecordAccess("operator", api_transfer=True),)
        try:
            workspace = PRINCIPALS[Owner.WORKSPACE]
            if await store.repository(Capability, workspace, scope).get("m3." + KIND) is None:
                capability = record(
                    registry,
                    Capability,
                    id="m3." + KIND,
                    **common,
                    adapter_id="m3-core",
                    adapter_version="1",
                    input_schema_id="m3.job",
                    output_schema_id="m3.result",
                    effect="local_write",
                    required_permissions=["permit-" + KIND],
                    resource_ids=["background", "llm"],
                    timeout_ms=180000,
                    cancellation_supported=True,
                    idempotency="supported",
                    health="ready",
                    setting_references=[],
                )
                await put(
                    store,
                    registry,
                    Owner.WORKSPACE,
                    scope,
                    "activity-capability",
                    capability,
                    None,
                    llm_access,
                )
            if await store.repository(Permission, workspace, scope).get("permit-" + KIND) is None:
                permit = record(
                    registry,
                    Permission,
                    id="permit-" + KIND,
                    **common,
                    principal_id="operator",
                    capability_id="m3." + KIND,
                    operation_type="m3." + KIND,
                    binding_id="m3-core",
                    level="automatic",
                    conditions=extension(registry, "m3.empty", {}),
                    limits=extension(registry, "m3.limits", {}),
                    policy_version=1,
                    author_id="operator",
                    expires_at=datetime.fromisoformat(config.permission_expires_at),
                )
                await put(
                    store,
                    registry,
                    Owner.WORKSPACE,
                    scope,
                    "activity-permit",
                    permit,
                    None,
                    rights.record_readers(),
                )
            to_live = settings.enabled
            parameters = extension(
                registry,
                "llm.generation-parameters",
                {
                    "max_output_tokens": live.max_output_tokens,
                    "timeout_seconds": live.timeout_seconds,
                }
                if to_live
                else dict(MOCK_PARAMETERS),
            )
            repo = store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope)
            for role in ROLES[KIND]:
                target = ("conn-live", live.model_id) if to_live else ("conn-mock", "mock-" + role)
                old = await repo.get("asg-" + role)
                if old is None:
                    item = record(
                        registry,
                        LLMAssignment,
                        id="asg-" + role,
                        **common,
                        role_id=role,
                        connection_id=target[0],
                        model_id=target[1],
                        parameters=parameters,
                        required_capabilities=[],
                        capability_verification="unverified",
                    )
                    await put(
                        store,
                        registry,
                        Owner.LLM,
                        scope,
                        f"activity-binding-{role}-1",
                        item,
                        None,
                        (*llm_access, *operator),
                    )
                elif (old.connection_id, old.model_id) != target or old.parameters != parameters:
                    if old.fallback_assignment_id is not None or (
                        old.connection_id,
                        old.model_id,
                    ) not in (
                        ("conn-mock", "mock-" + role),
                        ("conn-live", live.model_id),
                    ):
                        raise HostError("ACTIVITY_BINDING_CONFLICT")
                    item = old.model_copy(
                        update=dict(
                            connection_id=target[0],
                            model_id=target[1],
                            parameters=parameters,
                            version=old.version + 1,
                            updated_at=now,
                        )
                    )
                    await put(
                        store,
                        registry,
                        Owner.LLM,
                        scope,
                        f"activity-binding-{role}-{item.version}",
                        item,
                        old.version,
                    )
            updated = config.model_copy(update={"live": live})
            await store.provision(scope, rights.host_grants(updated))
            # Rights and records first: a crash before this line leaves the flag off.
            save_config(host, updated)
        finally:
            await store.close()
        return settings
