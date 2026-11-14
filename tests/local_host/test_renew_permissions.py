"""renew-permissions: the prepared permissions that ended are extended, nothing new is granted."""

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Owner
from ai_agent_contracts.api import Permission
from host_support import command, init

from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, ROLES


def read_ends(root):
    config = load_config(paths(root))
    registry = new_registry()
    store = SQLiteStore(paths(root).database, extensions=registry)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)

    async def read():
        repository = store.repository(Permission, PRINCIPALS[Owner.WORKSPACE], scope)
        return {k: await repository.get("permit-" + k) for k in ROLES}

    permits = asyncio.run(read())
    getattr(store, "close", lambda: None)()
    return config, permits


def test_renew_extends_the_ended_permits_and_grants_nothing_new(tmp_path):
    root = tmp_path / "host"
    done = init(root, "--permission-days", "1")
    assert done.returncode == 0, done.stderr
    _, before = read_ends(root)
    soon = datetime.now(UTC) + timedelta(days=2)
    assert all(p.expires_at < soon for p in before.values() if p is not None)
    result = subprocess.run(
        command("renew-permissions", "--root", str(root), "--days", "60"),
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "Permissions extended:" in result.stdout
    config, after = read_ends(root)
    end = datetime.now(UTC) + timedelta(days=59)
    assert all(p.expires_at > end for p in after.values() if p is not None)
    assert [k for k, p in before.items() if p is None] == [k for k, p in after.items() if p is None]
    assert datetime.fromisoformat(config.permission_expires_at) > end  # host.json moved too
    assert json.loads((root / "host.json").read_text())["permission_expires_at"]
    again = subprocess.run(  # nothing left to extend: a repeat is harmless
        command("renew-permissions", "--root", str(root), "--days", "30"),
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert again.returncode == 0 and "Permissions extended: 0" in again.stdout
    refused = subprocess.run(
        command("renew-permissions", "--root", str(root), "--days", "400"),
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert refused.returncode != 0  # more than 90 days is refused
