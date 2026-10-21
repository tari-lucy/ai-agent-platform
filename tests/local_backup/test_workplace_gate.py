"""Incomplete workplace restore must refuse, including with feature flags disabled."""

import asyncio
import sqlite3
from datetime import UTC, datetime

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.storage import Grant, Owner
from ai_agent_contracts.agent_workplace import WorkTask
from lb_support import cli, digest_of, live_digest, make_host
from test_restore import backup, issue_codes

from ai_agent_core.local_backup.compat import _summary
from ai_agent_core.local_host.layout import save_config
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, put


def test_workplace_restore_refuses_config_and_hidden_history_without_switching(tmp_path):
    async def scenario():
        host = await make_host(tmp_path)
        original = await asyncio.to_thread(backup, host)
        assert (await asyncio.to_thread(issue_codes, host, original))[0] == 0

        enabled = host.config.model_copy(update={"workplace_tickets": True})
        save_config(host.paths, enabled)
        code, found, _ = await asyncio.to_thread(issue_codes, host, original)
        assert code != 0 and "WORKPLACE_RESTORE_NOT_READY" in found

        # A structured operator decision cannot turn the blocking preview into success.
        _, preview, _ = await asyncio.to_thread(
            cli, "preview-restore", *host.args("--backup-id", original)
        )
        before = live_digest(host)
        code, _, _ = await asyncio.to_thread(
            cli,
            "restore",
            *host.args(
                "--backup-id",
                original,
                "--decision-id",
                "synthetic-decision",
                "--digest",
                digest_of(preview),
            ),
        )
        assert code != 0 and live_digest(host) == before

        copied_enabled = await asyncio.to_thread(backup, host)
        save_config(host.paths, host.config)
        code, found, _ = await asyncio.to_thread(issue_codes, host, copied_enabled)
        assert code != 0 and "WORKPLACE_RESTORE_NOT_READY" in found

        # Real owner-path row, then hidden identity: no flag or current visibility
        # is sufficient to certify that historical workplace bodies are clean.
        registry = new_registry()
        store = SQLiteStore(host.paths.database, extensions=registry)
        now = datetime.now(UTC)
        try:
            await store.provision(
                host.scope,
                (Grant(PRINCIPALS[Owner.ACTIVITY], Owner.ACTIVITY, read=True, write=True),),
            )
            await put(
                store,
                registry,
                Owner.ACTIVITY,
                host.scope,
                "synthetic-task",
                WorkTask(
                    id="synthetic-task",
                    task_id="synthetic-task",
                    scope=host.scope,
                    version=1,
                    created_at=now,
                    updated_at=now,
                    origin="assignment",
                    goal="A synthetic goal",
                    reason="Restore gate check",
                    state="open",
                ),
                None,
            )
        finally:
            await store.close()
        with sqlite3.connect(host.paths.database) as connection:
            connection.execute("UPDATE records SET available=0 WHERE kind='WorkTask'")
        assert _summary(host.paths.database)["workplace"]
        code, found, _ = await asyncio.to_thread(issue_codes, host, original)
        assert code != 0 and "WORKPLACE_RESTORE_NOT_READY" in found
        historical = await asyncio.to_thread(backup, host)
        database = host.store_dir / historical / "database.sqlite"
        assert _summary(database)["workplace"]

    asyncio.run(scenario())
