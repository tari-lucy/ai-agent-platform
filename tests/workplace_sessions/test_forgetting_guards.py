"""Full forgetting is pinned, bounded and resumes a partial ledger block."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from test_activity import WorkModel, prepared
from test_http import post, scope_params
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import command


async def manage(host, session_id, visibility, key):
    view = await transcript(host, session_id)
    return await post(
        host.env,
        dict(
            type="work.session.manage",
            request_id=key,
            scope=host.scope.model_dump(mode="json"),
            target=session_id,
            expected_version=view["session_version"],
            arguments=dict(
                schema_id="work.session.manage-args",
                data=dict(session_id=session_id, visibility=visibility),
            ),
        ),
    )


@asynccontextmanager
async def world(tmp_path, monkeypatch):
    host = await prepared(tmp_path, monkeypatch, WorkModel())
    await enable_recovery(
        host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger" / "ledger.sqlite"
    )
    async with host.open():
        identifier = (await open_session(host, "forget-guards")).json()["resource_id"]
        assert (
            await say(host, "guard-one", identifier, 1, "Synthetic first fact")
        ).status_code == 200
        assert (
            await say(host, "guard-two", identifier, 2, "Synthetic second fact")
        ).status_code == 200
        assert (await manage(host, identifier, "archived", "archive-guards")).status_code == 200
        selection = (
            await host.env.client.get(
                "/api/v1/work-session-sources/" + identifier, params=scope_params(host.scope)
            )
        ).json()
        assert selection["complete"] and len(selection["sources"]) == 2
        # Reverse the owner order: retention canonicalises the exact confirmed set.
        rows = list(reversed(selection["sources"]))
        preview = await post(
            host.env,
            dict(
                type="memory.delete.preview",
                request_id="guards-preview",
                scope=host.scope.model_dump(mode="json"),
                target=rows[0][0],
                expected_version=rows[0][1],
                arguments=dict(
                    schema_id="memory.delete-args",
                    data=dict(
                        source_ids=[row[0] for row in rows],
                        session_ref=[identifier, selection["session_version"]],
                    ),
                ),
            ),
        )
        assert preview.status_code == 200, preview.text
        execute = dict(
            type="memory.delete.execute",
            request_id="guards-execute",
            scope=host.scope.model_dump(mode="json"),
            target=rows[0][0],
            expected_version=1,
            arguments=dict(plan_id=preview.json()["resource_id"], plan_version=1),
        )
        yield host, identifier, rows, execute


@pytest.mark.parametrize("failure", ["during_batch", "after_commit"])
def test_atomic_region_and_retry_after_maintenance(tmp_path, monkeypatch, failure):
    async def scenario():
        async with world(tmp_path, monkeypatch) as (host, identifier, rows, execute):
            rt = host.runtime
            ledger = rt.retention.ledger
            before = await ledger.identity()
            if failure == "during_batch":
                # Abort the second insertion inside the real SQLite transaction.
                second = sorted(row[0] for row in rows)[1]
                ledger.raw._db.execute(
                    "CREATE TEMP TRIGGER synthetic_batch_failure "
                    "BEFORE INSERT ON deletion_ledger WHEN NEW.source_id = '" + second + "' "
                    "BEGIN SELECT RAISE(ABORT, 'synthetic batch interruption'); END"
                )
            else:
                original = ledger.block_many

                async def fail_after_commit(*args, **kwargs):
                    await original(*args, **kwargs)
                    raise OSError("synthetic lost acknowledgement after commit")

                monkeypatch.setattr(ledger, "block_many", fail_after_commit)
            first = await post(host.env, execute)
            assert first.json()["code"] == "OUTCOME_UNKNOWN", first.text
            if failure == "during_batch":
                assert await ledger.raw.entries(host.scope) == []
                assert await ledger.raw.identity() == before
                # The anti-rollback floor was reserved before the failed write.
                # Availability stays closed for explicit operator recovery; no prefix exists.
                assert ledger.failed
                return
            else:
                entries = await ledger.entries(host.scope)
                assert {entry.source_id for entry in entries} == {row[0] for row in rows}
                monkeypatch.setattr(ledger, "block_many", original)
                # Maintenance/startup is allowed to physically purge before execute retries.
                assert (await rt.retention.resume(host.scope)).complete
                assert all(
                    entry.purged_at is not None for entry in await ledger.entries(host.scope)
                )
            done = await post(host.env, execute)
            assert done.status_code == 200 and done.json()["status"] == "completed", done.text
            assert (await post(host.env, execute)).json() == done.json()
            entries = await ledger.entries(host.scope)
            assert {entry.source_id for entry in entries} == {row[0] for row in rows}
            assert all(entry.purged_at is not None for entry in entries)
            assert (await transcript(host, identifier))["entries"] == []

    asyncio.run(scenario())


@pytest.mark.parametrize("changed", ["session", "source"])
def test_changed_session_or_source_cannot_consume_forgetting_plan(tmp_path, monkeypatch, changed):
    async def scenario():
        async with world(tmp_path, monkeypatch) as (host, identifier, rows, execute):
            rt = host.runtime
            if changed == "session":
                assert (
                    await manage(host, identifier, "active", "restore-after-preview")
                ).status_code == 200
            else:
                await memory(rt).set_rights(
                    command(host.scope, "revoke-forget-basis"),
                    rows[1][0],
                    rows[1][1],
                    read_allowed=True,
                    api_transfer_allowed=False,
                    public_disclosure_allowed=False,
                )
            result = await post(host.env, execute)
            assert result.status_code in (403, 409), result.text
            assert await rt.retention.ledger.entries(host.scope) == []

    asyncio.run(scenario())
