"""Explicit offline admission of the private interlocutor; serve never grants rights.

`init` already provisions the SOCIAL rows of the current matrix. An older host gets them here:
the work scope is provisioned again with exactly `rights.work_grants` (nothing else is held
there), then the config flag lets serve link the panel login to one person. Sources written
before this keep their record ACL, so only new messages can ground a promise.
"""

from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore

from ai_agent_core.m3_composition import new_registry

from . import rights
from .init import work_scope
from .layout import HostError, load_config, paths, read_secret, save_config
from .process import exclusive_host


async def enable_social(root: Path) -> None:
    host = paths(root)
    if not root.is_dir():
        raise HostError("INIT_INCOMPLETE")
    with exclusive_host(root):
        config = load_config(host)
        read_secret(host.password_hash)  # proves the private prepared host boundary; never printed
        store = SQLiteStore(host.database, extensions=new_registry())
        try:
            await store.provision(
                work_scope(config.workspace_id, config.persona_id, config.work_space_id),
                rights.work_grants(
                    operator_controls=config.operator_controls, tickets=config.workplace_tickets
                ),
            )
        finally:
            await store.close()
        # Rights first: a crash before this line leaves the flag off and dialogue anonymous.
        save_config(host, config.model_copy(update={"private_social": True}))
