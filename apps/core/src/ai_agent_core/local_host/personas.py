"""Offline preparation of an independent, inactive persona. No model or execution rights."""

import hashlib
import json
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.persona import PersonaService, import_markdown
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import CreateCommand
from ai_agent_contracts.codec import from_wire
from pydantic import TypeAdapter, ValidationError

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS

from . import rights
from .init import PROFILE_LIMIT, work_scope
from .layout import (
    PERSONA_PENDING,
    HostConfig,
    HostError,
    InactivePersona,
    load_config,
    paths,
    save_config,
    write_secret,
)
from .process import exclusive_host
from .recovery import no_links, private


async def add_persona(root: Path, *, persona_id: str, profile_markdown: str) -> HostConfig:
    """Idempotent initial import. Repeat never edits a profile or copies another persona."""
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    raw = profile_markdown.encode("utf-8")
    if not raw or len(raw) > PROFILE_LIMIT or import_markdown(profile_markdown)[0] is None:
        raise HostError("PROFILE_INVALID")
    try:
        entry = InactivePersona(
            persona_id=persona_id,
            work_space_id="persona-" + hashlib.sha256(persona_id.encode()).hexdigest()[:32],
            profile_digest=hashlib.sha256(raw).hexdigest(),
        )
    except ValidationError:
        raise HostError("INVALID_PERSONA_ID") from None
    with exclusive_host(root):
        private(root)
        config = load_config(host, allow_persona_pending=True)
        if persona_id == config.persona_id:
            raise HostError("PERSONA_ALREADY_EXISTS")
        marker = root / PERSONA_PENDING
        if marker.exists() or marker.is_symlink():
            private(marker)
            try:
                with marker.open("rb") as handle:
                    pending = handle.read(2049)
                if len(pending) > 2048 or InactivePersona.model_validate_json(pending) != entry:
                    raise ValueError()
            except OSError, ValueError:
                raise HostError("PERSONA_PENDING_DIFFERENT_REQUEST") from None
        found = next((p for p in config.additional_personas if p.persona_id == persona_id), None)
        if found is not None and found != entry:
            raise HostError("PERSONA_ALREADY_EXISTS")
        try:
            updated = (
                HostConfig.model_validate(
                    config.model_dump()
                    | {"additional_personas": (*config.additional_personas, entry)}
                )
                if found is None
                else config
            )
        except ValidationError:
            raise HostError("PERSONA_SCOPE_LIMIT") from None
        scope = work_scope(config.workspace_id, persona_id, entry.work_space_id)
        if not host.database.is_file():
            raise HostError("INIT_INCOMPLETE")
        no_links(host.database)  # the checked private host root protects SQLite's files
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        try:
            await store.check_schema()
            persona = PersonaService(
                store, PRINCIPALS[Owner.PERSONA], registry, reader_access=rights.record_readers()
            )
            if found is None:
                if not marker.exists():
                    write_secret(marker, entry.model_dump_json() + "\n")
                # A new partition, using the canonical matrix. No Permission, Connection,
                # assignments, files, memory, tickets or runtime are copied/created here.
                await store.provision(scope, rights.host_grants(config))
                request = {
                    "request_id": "local-host-add-persona-" + persona_id,
                    "scope": scope.model_dump(mode="json"),
                    "target": persona_id,
                    "type": "persona.create",
                    "arguments": {
                        "schema_id": "persona.create-args",
                        "schema_version": "1.0",
                        "data": {"markdown": profile_markdown},
                    },
                }
                await persona.create(
                    from_wire(
                        TypeAdapter(CreateCommand),
                        json.dumps(request).encode(),
                        extensions=registry,
                    )
                )
                save_config(host, updated)  # publish only after owner commits its profile/state
            if await persona.get_profile(scope) is None or await persona.get_state(scope) is None:
                raise HostError("PERSONA_STATE_INCOMPLETE")
            marker.unlink(missing_ok=True)
            return updated
        finally:
            await store.close()
