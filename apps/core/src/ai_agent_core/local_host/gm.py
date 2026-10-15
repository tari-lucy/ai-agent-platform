"""Explicit offline switch of the bounded internal game master; serve never grants rights.

Enabling binds only the game-master and story-screen roles to the already prepared live
connection, under the host lock and a marker that keeps `serve` closed until the change is
complete. Disabling returns every role that is no longer admitted to its mock binding. The rule of
which role may be live is `layout.live_roles`, shared with init, the provider and restore checks.
No Permission, grant or memory setting is changed here.
"""

from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_contracts.llm import LLMAssignment
from pydantic import JsonValue, ValidationError

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, ROLES, extension, put

from .init import work_scope
from .layout import (
    GM_PENDING,
    GmConfig,
    HostError,
    LiveConfig,
    live_roles,
    load_config,
    paths,
    read_secret,
    save_config,
    write_secret,
)
from .process import exclusive_host

MOCK_PARAMETERS: dict[str, JsonValue] = {
    "max_output_tokens": 500,
    "timeout_seconds": 5.0,
}  # as written by init


async def configure_gm(root: Path, **changes: object) -> GmConfig:
    """Apply GM setting changes offline. Repeating after an interruption finishes the change."""
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host, allow_gm_pending=True)
        read_secret(host.password_hash)  # proves the private prepared host boundary; never printed
        if config.live is None or not config.operator_controls:
            raise HostError("GM_CONTROLS_REQUIRED")
        try:
            settings = GmConfig.model_validate({**config.live.gm.model_dump(), **changes})
        except ValidationError:
            raise HostError("INVALID_OPTION") from None
        if settings.enabled and settings.line_direction is None:
            raise HostError("GM_LINE_REQUIRED")
        try:
            live = LiveConfig.model_validate(
                {**config.live.model_dump(), "gm": settings.model_dump()}
            )
        except ValidationError:
            raise HostError("DAILY_GM_BUDGET_REQUIRED") from None
        updated = config.model_copy(update={"live": live})
        wanted = live_roles(live)
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        marker = root / GM_PENDING
        try:
            pending: list[tuple[LLMAssignment, LLMAssignment]] = []
            for role in sorted(ROLES["gm"]):
                old = await store.repository(LLMAssignment, PRINCIPALS[Owner.LLM], scope).get(
                    "asg-" + role
                )
                if old is None or old.role_id != role or old.fallback_assignment_id is not None:
                    raise HostError("GM_BINDING_CONFLICT")
                to_live = role in wanted
                values: dict[str, JsonValue] = (
                    {
                        "max_output_tokens": live.max_output_tokens,
                        "timeout_seconds": live.timeout_seconds,
                    }
                    if to_live
                    else dict(MOCK_PARAMETERS)
                )
                parameters = extension(registry, "llm.generation-parameters", values)
                target = ("conn-live", live.model_id) if to_live else ("conn-mock", "mock-" + role)
                if (old.connection_id, old.model_id) == target and old.parameters == parameters:
                    continue
                if (old.connection_id, old.model_id) not in (
                    ("conn-mock", "mock-" + role),
                    ("conn-live", live.model_id),
                ):
                    raise HostError("GM_BINDING_CONFLICT")
                pending.append(
                    (
                        old,
                        old.model_copy(
                            update=dict(
                                connection_id=target[0],
                                model_id=target[1],
                                parameters=parameters,
                                version=old.version + 1,
                                updated_at=datetime.now(UTC),
                            )
                        ),
                    )
                )
            if not marker.exists():
                write_secret(marker, "GM change incomplete; rerun configure-gm offline.\n")
            for old, item in pending:
                await put(
                    store,
                    registry,
                    Owner.LLM,
                    scope,
                    f"gm-binding-{old.role_id}-{item.version}",
                    item,
                    old.version,
                )
            save_config(host, updated)
            marker.unlink()
        finally:
            await store.close()
        return live.gm
