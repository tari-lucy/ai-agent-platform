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
    asyncio.run(store.close())
    return config, permits


def test_renew_extends_the_ended_permits_and_grants_nothing_new(tmp_path):
    root = tmp_path / "host"
    done = init(root, "--operator-controls", "--permission-days", "1")
    assert done.returncode == 0, done.stderr
    _, before = read_ends(root)
    soon = datetime.now(UTC) + timedelta(days=2)
    assert all(p.expires_at < soon for p in before.values() if p is not None)
    # make every permit ended, as on a host whose days ran out (a plain read does not return it)
    import sqlite3

    database = sqlite3.connect(paths(root).database)
    ended = "2020-01-01T00:00:00+00:00"
    database.execute("update records set expires_at=? where kind='Permission'", (ended,))
    database.execute("update record_revisions set expires_at=? where kind='Permission'", (ended,))
    database.commit()
    database.close()
    _, ended_now = read_ends(root)
    assert all(p is None for p in ended_now.values())  # a plain read does not return them
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
    assert sum(p is not None for p in after.values()) == sum(p is not None for p in before.values())
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
