"""Explicit offline admission of operator tickets (AW-01); serve never grants rights.

The work scope is provisioned again with exactly `rights.work_grants` plus the TICKETS rows,
then the config flag lets serve open the tickets owner. Nothing else changes; the agent is not
started and no permission is created or widened.
"""

from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore

from ai_agent_core.m3_composition import new_registry

from . import rights
from .init import work_scope
from .layout import HostError, load_config, paths, read_secret, save_config
from .process import exclusive_host


async def enable_tickets(root: Path) -> None:
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
                rights.work_grants(operator_controls=config.operator_controls, tickets=True),
            )
        finally:
            await store.close()
        # Rights first: a crash before this line leaves the flag off and tickets unavailable.
        save_config(host, config.model_copy(update={"workplace_tickets": True}))
