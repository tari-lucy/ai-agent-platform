"""Explicit offline metadata ACL preparation for older operator-controls hosts."""

from datetime import UTC, datetime
from pathlib import Path

from ai_agent_adapters.storage import schema as db
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_adapters.storage.policy import scope_key
from ai_agent_application.storage import Owner, RecordAccess
from ai_agent_contracts.common import VersionedRecord
from ai_agent_contracts.llm import Connection, LLMAssignment
from sqlalchemy import select

from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import OPERATOR, PRINCIPALS, put

from .init import work_scope
from .layout import HostError, load_config, paths, read_secret
from .process import exclusive_host


async def prepare_llm_panel(root: Path) -> None:
    host = paths(root)
    with exclusive_host(root):
        config = load_config(host)
        read_secret(host.password_hash)
        if not config.operator_controls:
            raise HostError("OPERATOR_CONTROLS_REQUIRED")
        scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
        registry = new_registry()
        store = SQLiteStore(host.database, extensions=registry)
        try:
            # Metadata only: no grants, Permission, credentials or executable bindings change.
            # Partial completion is safe and repeating skips already prepared records.
            for model, owner in ((Connection, Owner.WORKSPACE), (LLMAssignment, Owner.LLM)):
                after = None
                while True:
                    rows: list[VersionedRecord] = list(
                        await store.repository(model, PRINCIPALS[owner], scope).list(
                            limit=50, after_id=after
                        )
                    )
                    for item in rows:
                        async with store.engine.connect() as connection:
                            access = (
                                (
                                    await connection.execute(
                                        select(db.access).where(
                                            db.access.c.scope_id == scope_key(scope),
                                            db.access.c.kind == model.__name__,
                                            db.access.c.id == item.id,
                                        )
                                    )
                                )
                                .mappings()
                                .all()
                            )
                        if any(
                            row["principal_id"] == OPERATOR.id
                            and row["read"]
                            and row["api_transfer"]
                            and row["expires_at"] is None
                            for row in access
                        ):
                            continue
                        readers = tuple(
                            RecordAccess(
                                row["principal_id"],
                                read=row["read"],
                                api_transfer=row["api_transfer"],
                                public_disclosure=row["public_disclosure"],
                                expires_at=(
                                    None
                                    if row["expires_at"] is None
                                    else datetime.fromisoformat(row["expires_at"])
                                ),
                            )
                            for row in access
                            if row["principal_id"] != OPERATOR.id
                        )
                        updated = item.model_copy(
                            update=dict(
                                version=item.version + 1,
                                updated_at=max(datetime.now(UTC), item.updated_at),
                            )
                        )
                        await put(
                            store,
                            registry,
                            owner,
                            scope,
                            f"llm-panel-{model.__name__}-{item.id}-{updated.version}",
                            updated,
                            item.version,
                            (*readers, RecordAccess(OPERATOR.id, api_transfer=True)),
                        )
                    if len(rows) < 50:
                        break
                    after = rows[-1].id
        finally:
            await store.close()
