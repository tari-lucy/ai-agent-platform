"""Explicit offline preparation of managed files, skills and knowledge (AW-03).

Creates the private blob directory (owner-only, never inside the database or the panel), the
manifests and ready bindings of the two library capabilities, and the SKILLS/catalogue rows of
the rights matrix; then the config lets serve build the file owner. No Permission is written:
the agent asks by ticket and the operator decides through the structured permission decision,
within the optional ceiling given here. Requires the deletion ledger (recovery), since the
release guard checks it. The agent stays STOPPED; repeating after an interruption finishes it.
"""

from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import (
    CapabilityBinding,
    CapabilityManifest,
    WorkplaceArea,
)
from pydantic import ValidationError

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, put

from . import rights
from .init import work_scope
from .layout import (
    HostError,
    PermissionCeiling,
    WorkplaceFilesConfig,
    load_config,
    paths,
    read_secret,
    save_config,
)
from .process import exclusive_host

ADAPTER = "workplace-library"
# Capability, its areas and what the agent is told it does. Generic, no persona content.
CAPABILITIES: dict[str, tuple[tuple[WorkplaceArea, ...], str, str]] = {
    "workplace.skills": (
        ("skills",),
        "Библиотека навыков: найти, прочитать, сохранить или улучшить свой метод.",
        "Аргументы: op=find (query) | read (skill_id) | save (skill_id, title, applicability, "
        "limitations, method, change_reason, expected_version). Новый или изменённый метод "
        "остаётся экспериментальным до подтверждённого результата; текст навыка не даёт прав.",
    ),
    "workplace.knowledge": (
        ("knowledge", "inbox"),
        "База знаний: найти, прочитать или записать заметку либо гипотезу.",
        "Аргументы: op=search (query) | read (file_id) | write (file_id, path, title, "
        "kind=note|hypothesis, body, expected_version). Заметка — собственный текст, гипотеза "
        "— предположение; ни то, ни другое не является подтверждённым опытом.",
    ),
}


async def enable_workplace_files(
    root: Path,
    *,
    blob_dir: Path | None = None,
    ceiling_days: int | None = None,
    **limits: int,
) -> WorkplaceFilesConfig:
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host)
        read_secret(host.password_hash)  # proves the private prepared host boundary; never printed
        if config.recovery is None or not config.operator_controls:
            raise HostError("WORKPLACE_FILES_RECOVERY_REQUIRED")
        if ceiling_days is not None and not config.workplace_tickets:
            raise HostError("WORKPLACE_FILES_TICKETS_REQUIRED")
        target = (blob_dir or root / "workplace-files").resolve()
        try:
            settings = WorkplaceFilesConfig(blob_dir=str(target), **limits)
        except ValidationError:
            raise HostError("WORKPLACE_FILE_LIMITS") from None
        previous = config.workplace_files
        if previous is not None and previous.blob_dir != settings.blob_dir:
            raise HostError("WORKPLACE_FILES_DIR_FIXED")
        if target.exists():
            DirectoryBlobs(target)  # an interrupted run left it; refuses a link or a file
        else:
            DirectoryBlobs.prepare(target)
        ceilings = dict(config.workplace_permissions or {})
        if ceiling_days is not None:
            ceilings.update(
                {
                    key: PermissionCeiling(level="automatic", max_days=ceiling_days)
                    for key in CAPABILITIES
                }
            )
        updated = config.model_copy(
            update={
                "workplace_files": settings,
                "workplace_permissions": ceilings or None,
            }
        )
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        now = datetime.now(UTC)
        workspace = PRINCIPALS[Owner.WORKSPACE]
        try:
            for capability_id, (areas, summary, instructions) in CAPABILITIES.items():
                manifest = CapabilityManifest(
                    id="manifest-" + capability_id,
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    capability_id=capability_id,
                    capability_version=1,
                    summary=summary,
                    instructions=instructions,
                    input_schema_id="workplace.tool-arguments",
                    output_schema_id="workplace.tool-result",
                    effect="local_write",
                    areas=list(areas),
                    network="none",
                    runner_required=False,
                )
                binding = CapabilityBinding(
                    id=ADAPTER + "." + capability_id,
                    scope=scope,
                    created_at=now,
                    updated_at=now,
                    version=1,
                    binding_id=ADAPTER + "." + capability_id,
                    capability_id=capability_id,
                    capability_version=1,
                    adapter_id=ADAPTER,
                    adapter_version="1",
                    state="ready",
                    verified_at=now,
                )
                readers = rights.record_readers()
                for name, row in (("manifest", manifest), ("binding", binding)):
                    if await store.repository(type(row), workspace, scope).get(row.id):
                        continue  # written by an interrupted earlier run; never replaced here
                    await put(
                        store,
                        registry,
                        Owner.WORKSPACE,
                        scope,
                        f"workplace-{name}-{capability_id}",
                        row,
                        None,
                        readers,
                    )
            await store.provision(scope, rights.host_grants(updated))
        finally:
            await store.close()
        # Records and rights first: a crash before this line leaves the flag off.
        save_config(host, updated)
        return settings
