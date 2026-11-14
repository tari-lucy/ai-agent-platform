"""Offline renewal of the prepared permissions (the m3 kinds init seeds, and activity).

``init`` gives the prepared permissions a lifetime (30 days by default, at most 90). When it ends
the memory, reflection, game-master and dialogue jobs are refused (ACCESS_DENIED) and the persona
stops developing. This command writes a new version of every ``permit-*`` that exists with a later
end and moves ``permission_expires_at`` of the host config. It grants nothing new: only kinds that
already have a permit are touched, and it never shortens one. The agent stays STOPPED.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_contracts.api import Permission
from ai_agent_contracts.codec import from_wire
from pydantic import TypeAdapter

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, ROLES, put

from . import rights
from .init import MAX_PERMISSION_DAYS, work_scope
from .layout import HostError, load_config, paths, read_secret, save_config
from .process import exclusive_host


async def renew_permissions(root: Path, days: int = MAX_PERMISSION_DAYS) -> tuple[int, datetime]:
    """Returns (how many permits were extended, the new end)."""
    if not 1 <= days <= MAX_PERMISSION_DAYS:
        raise HostError("INVALID_OPTION")
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host)
        read_secret(host.password_hash)  # proves the private prepared host boundary
        if not config.operator_controls:
            raise HostError("CONTROLS_REQUIRED")  # the workspace owner may write only with them
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        now = datetime.now(UTC)
        end = now + timedelta(days=days)
        readers = rights.record_readers()
        changed = 0
        try:
            for kind in ROLES:
                # An ended permit is not returned by a plain read (that is what renewal is for):
                # it is read as stored, and the new version is written through the owner as usual.
                existing = _stored(host.database, scope_key(scope), "permit-" + kind, registry)
                if existing is None or (
                    existing.expires_at is not None and existing.expires_at >= end
                ):
                    continue  # no such permit (never granted), or it already lasts longer
                await put(
                    store,
                    registry,
                    Owner.WORKSPACE,
                    scope,
                    f"renew-permit-{kind}-{existing.version}",
                    existing.model_copy(
                        update={
                            "expires_at": end,
                            "version": existing.version + 1,
                            "updated_at": now,
                        }
                    ),
                    existing.version,
                    readers,
                )
                changed += 1
        finally:
            await store.close()
        later = datetime.fromisoformat(config.permission_expires_at)
        if later < end:
            save_config(host, config.model_copy(update={"permission_expires_at": end.isoformat()}))
        return changed, end


def _stored(database: Path, key: str, name: str, registry: object) -> Permission | None:
    """The current version of a permit, ended or not (read only; the file is the host's own)."""
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        row = connection.execute(
            "select r.version, v.payload from records r join record_revisions v "
            "on v.scope_id = r.scope_id and v.kind = r.kind and v.id = r.id and "
            "v.version = r.version where r.scope_id = ? and r.kind = 'Permission' and r.id = ?",
            (key, name),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return from_wire(TypeAdapter(Permission), row[1].encode(), extensions=registry)  # type: ignore[arg-type]
