"""Offline packaging proof on isolated synthetic data; executable inside core image."""

import asyncio
import hashlib
import importlib
import json
import sqlite3
import time
from importlib.metadata import metadata, version
from pathlib import Path
from uuid import uuid4

from ai_agent_adapters.configuration import BootstrapSettings
from ai_agent_adapters.control.journal import SQLiteRunJournal
from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.control import Supervisor
from ai_agent_application.control.ports import ContextApproval, ControlAuthority
from ai_agent_application.storage import Grant, Owner, Principal
from ai_agent_contracts.api import ControlArguments, ControlCommand, StopCommand
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.common import Scope
from ai_agent_contracts.states import ErrorCode, RunState

from ai_agent_core.bootstrap import create_app


class SyntheticReadiness:
    async def check(self, run):
        return ContextApproval(run.scope, 1, 1, time.monotonic() + 20)

    async def reconcile(self, previous):
        return ()  # This fixture has no remote executor.


async def main():
    modules = [
        "ai_agent_application.control.ports",
        "ai_agent_application.control.supervisor",
        "ai_agent_adapters.control.journal",
        "ai_agent_core.bootstrap",
    ]
    hashes = {}
    for name in modules:
        path = Path(importlib.import_module(name).__file__)
        hashes[name] = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    migrations = {}
    for relative in ("env.py", "versions/0001_scopes.py", "versions/0002_records.py"):
        path = Path("/opt/platform/migrations") / relative
        migrations[relative] = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert importlib.util.find_spec("pytest") is None
    assert importlib.util.find_spec("playwright") is None
    assert version("greenlet") == "3.5.6"
    assert any(
        "sqlalchemy[asyncio]" in requirement.lower()
        for requirement in metadata("ai-agent-platform").get_all("Requires-Dist")
    )
    app = create_app(BootstrapSettings(workspace_id="synthetic-packaging"))
    assert app.routes == []
    assert app.state.supervisor.snapshot.state == RunState.STOPPED
    scope = Scope(
        workspace_id="synthetic-packaging",
        persona_id="synthetic-persona",
        memory_space_id="synthetic-space",
        memory_mode="test",
    )
    principal = Principal("synthetic-supervisor", "core_owner", Owner.SUPERVISOR)
    store = SQLiteStore(Path("/var/lib/platform/core") / ("synthetic-" + uuid4().hex + ".db"))
    try:
        await store.upgrade(Path("/opt/platform/migrations"))
        await store.check_schema()
        await store.provision(scope, (Grant(principal, Owner.SUPERVISOR, read=True, write=True),))
        journal = SQLiteRunJournal(store, principal, (scope,))
        owner = Supervisor(scope, "synthetic-agent", journal, SyntheticReadiness())
        authority = ControlAuthority(
            "synthetic-operator",
            frozenset({scope.partition}),
            time.monotonic() + 60,
        )
        await owner.recover()
        await owner.control(
            authority,
            ControlCommand(
                request_id="start",
                type="agent.start",
                scope=owner.snapshot.scope,
                target="synthetic-agent",
                arguments=ControlArguments(autonomy="autonomous"),
                expected_version=owner.snapshot.version,
            ),
        )
        admission = owner.admit("activity")
        receipt = owner.stop(
            authority,
            StopCommand(
                request_id="stop",
                type="agent.stop",
                scope=owner.snapshot.scope,
                target="synthetic-agent",
            ),
        )
        assert receipt.state == RunState.STOPPED
        applied = []
        try:
            owner.finish(admission, lambda: applied.append("forbidden"))
        except ContractError as error:
            assert error.error.code == ErrorCode.STALE_CONTEXT
        else:
            raise AssertionError("STALE_ACTION_ALLOWED")
        assert applied == []
        await owner.flush()
        assert await journal.load() == []
        restarted = Supervisor(scope, "synthetic-agent", journal, SyntheticReadiness())
        await restarted.recover()
        assert restarted.snapshot.state == RunState.STOPPED
    finally:
        await store.close()
    print(
        json.dumps(
            {
                "probe": "M1-03-offline-core-packaging",
                "schema_head": "0002",
                "sqlite": sqlite3.sqlite_version,
                "source_sha256_lf": hashes,
                "migration_sha256_lf": migrations,
                "versions": {
                    name: version(name)
                    for name in (
                        "ai-agent-platform",
                        "SQLAlchemy",
                        "aiosqlite",
                        "alembic",
                        "greenlet",
                    )
                },
                "status": "passed",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
