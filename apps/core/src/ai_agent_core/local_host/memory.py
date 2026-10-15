"""Explicit offline admission of automatic private memory roles; serve never grants rights."""

from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_contracts.llm import LLMAssignment

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import MEMORY_ROLES, PRINCIPALS, extension, put

from .init import work_scope
from .layout import (
    HostError,
    LiveConfig,
    load_config,
    paths,
    read_secret,
    save_config,
    write_secret,
)
from .process import exclusive_host


async def enable_memory(root: Path) -> None:
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host, allow_memory_pending=True)
        read_secret(host.password_hash)  # proves the private prepared host boundary; never printed
        if config.live is None or not config.operator_controls:
            raise HostError("MEMORY_CONTROLS_REQUIRED")
        try:
            live = LiveConfig.model_validate(
                {**config.live.model_dump(), "memory_processing": True}
            )
        except ValueError:
            raise HostError("DAILY_MEMORY_BUDGET_REQUIRED") from None
        updated = config.model_copy(update={"live": live})
        marker = root / "MEMORY-ENABLE-PENDING"
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        try:
            pending: list[tuple[LLMAssignment, LLMAssignment]] = []
            for role in sorted(MEMORY_ROLES):
                old = await store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope).get(
                    "asg-" + role
                )
                if old is None or old.role_id != role or old.fallback_assignment_id is not None:
                    raise HostError("MEMORY_BINDING_CONFLICT")
                desired = extension(
                    registry,
                    "llm.generation-parameters",
                    {
                        "max_output_tokens": live.max_output_tokens,
                        "timeout_seconds": live.timeout_seconds,
                    },
                )
                if (
                    old.connection_id == "conn-live"
                    and old.model_id == live.model_id
                    and old.parameters == desired
                ):
                    continue
                if old.connection_id != "conn-mock" or old.model_id != "mock-" + role:
                    raise HostError("MEMORY_BINDING_CONFLICT")
                item = old.model_copy(
                    update=dict(
                        connection_id="conn-live",
                        model_id=live.model_id,
                        parameters=desired,
                        version=old.version + 1,
                        updated_at=datetime.now(UTC),
                    )
                )
                pending.append((old, item))
            if not marker.exists():
                write_secret(marker, "Memory admission incomplete; rerun enable-memory offline.\n")
            for old, item in pending:
                await put(
                    store,
                    registry,
                    Owner.LLM,
                    scope,
                    f"memory-binding-{old.role_id}-{item.version}",
                    item,
                    old.version,
                )
            save_config(host, updated)
            marker.unlink()
        finally:
            await store.close()
