"""Open an initialised host directory STOPPED on loopback. No migration, grant or auto-start."""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import uvicorn
from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.llm import ADAPTER_ID
from ai_agent_adapters.recall import SQLiteRecallIndex
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.laboratory import LabRunner
from ai_agent_application.memory.ports import RecallIndex
from ai_agent_application.storage import RecordAccess
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode
from fastapi import FastAPI
from pydantic import SecretStr

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.app import create_m3_app
from ai_agent_core.m3_composition.artifacts import Reports
from ai_agent_core.m3_composition.service import PrivateAPIBudget
from ai_agent_core.m3_composition.state import OPERATOR

from . import rights
from .init import test_scope, work_scope
from .layout import HostConfig, HostError, HostPaths, load_config, paths, read_secret
from .panel import mount, validate
from .provider import provider_factory

LOOPBACK = "127.0.0.1"


async def _no_scope(_label: str) -> Scope:
    # Test scopes are reserved from the prepared pool; the runner never allocates its own.
    raise ContractError(ErrorCode.CAPABILITY_UNAVAILABLE)


def build(
    host: HostPaths,
    config: HostConfig,
    *,
    provider: str,
    port: int | None = None,
    panel_dir: Path | None = None,
    env_file: Path | None = None,
) -> tuple[FastAPI, Callable[[], None]]:
    """Compose the app. Returns it with a closer for the indexes this function opened."""
    if port is not None and not 1024 <= port <= 65535:
        raise HostError("INVALID_OPTION")
    factory = provider_factory(provider, live=config.live, env_file=env_file)
    # Credential/config refusals above happen before opening the database or indexes.
    if not host.database.is_file():
        raise HostError("INIT_INCOMPLETE")
    panel = None if panel_dir is None else validate(panel_dir, host.root)
    number = port or config.port
    secret = read_secret(host.password_hash)
    work = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    scopes = (work, *(test_scope(work, space) for space in config.test_space_ids))
    if config.settings_space_id is not None:
        scopes += (test_scope(work, config.settings_space_id),)
    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    opened: list[SQLiteRecallIndex] = []

    def lab_index() -> RecallIndex:
        index = SQLiteRecallIndex(host.lab_indexes / (uuid4().hex + ".sqlite"))
        opened.append(index)
        return index

    dialogue = SQLiteRecallIndex(host.dialogue_index)
    opened.append(dialogue)
    lab = LabRunner(
        store,
        _no_scope,
        lab_index,
        report_access=(RecordAccess(OPERATOR.id, api_transfer=True),),
    )
    reports = Reports(
        host.reports,
        store,
        registry,
        readers=rights.report_readers(),
        api_transfer_allowed=True,
    )
    settings = BootstrapSettings(
        workspace_id=config.workspace_id,
        persona_id=config.persona_id,
        core_host_port=number,
        operator_origin=f"http://{LOOPBACK}:{number}",
        operator_password_hash=SecretStr(secret),
    )
    app = create_m3_app(
        settings,
        store,
        registry,
        providers=factory,
        allowed_adapters=frozenset({"mock", ADAPTER_ID} if config.live else {"mock"}),
        private_api_budget=None
        if config.live is None
        else PrivateAPIBudget("conn-live", config.live.attempt_limit, config.live.max_prompt_chars),
        scopes=scopes,
        reader_access=rights.record_readers(),
        dialogue_index=dialogue,
        lab=lab,
        reports=reports,
    )

    if panel is not None:
        mount(app, panel)

    def close() -> None:
        for index in opened:
            index.close()

    return app, close


def permissions_expired(config: HostConfig) -> bool:
    return datetime.fromisoformat(config.permission_expires_at) <= datetime.now(UTC)


def serve(
    root: Path,
    *,
    provider: str,
    port: int | None = None,
    panel_dir: Path | None = None,
    env_file: Path | None = None,
) -> None:
    host = paths(root)
    config = load_config(host)
    app, close = build(
        host, config, provider=provider, port=port, panel_dir=panel_dir, env_file=env_file
    )
    number = port or config.port
    try:
        uvicorn.run(
            app,
            host=LOOPBACK,  # fixed: no public or remote listener
            port=number,
            workers=1,
            access_log=False,
            log_level="warning",
            ws="websockets",
            ws_per_message_deflate=False,
            ws_max_size=4096,
            ws_max_queue=1,
            backlog=128,
            timeout_keep_alive=5,
        )
    finally:
        close()
