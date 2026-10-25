"""Conversation lifecycle through production owners/HTTP, synthetic data only."""

import asyncio

from test_activity import WorkModel, prepared
from test_http import post, scope_params
from test_work_sessions_http import open_session, say, transcript
from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Source
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.m3_composition.state import PRINCIPALS


def test_archive_restore_hide_and_explicit_multi_source_forgetting(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(host.root, store_dir=tmp_path / "backups",
                              ledger_path=tmp_path / "ledger" / "ledger.sqlite")
        async with host.open():
            opened = (await open_session(host, "manage-session")).json()
            identifier = opened["resource_id"]
            assert (await say(host, "input-one", identifier, 1, "Synthetic first fact")).status_code == 200
            assert (await say(host, "input-two", identifier, 2, "Synthetic second fact")).status_code == 200
            async def manage(key, version, visibility):
                return await post(host.env, dict(type="work.session.manage", request_id=key,
                    scope=host.scope.model_dump(mode="json"), target=identifier,
                    expected_version=version, arguments=dict(schema_id="work.session.manage-args",
                    data=dict(session_id=identifier, visibility=visibility))))
            archived = await manage("archive", 3, "archived")
            assert archived.status_code == 200, archived.text
            assert (await manage("archive", 3, "archived")).json() == archived.json()
            assert (await manage("stale", 3, "active")).status_code == 409
            view = await transcript(host, identifier)
            assert len(view["entries"]) == 2
            refused = await say(host, "cannot-answer-archived", identifier,
                                view["session_version"], "Orphan must be retired")
            assert refused.status_code == 403
            assert (await manage("restore", view["session_version"], "active")).status_code == 200
            view = await transcript(host, identifier)
            assert (await manage("hide", view["session_version"], "hidden")).status_code == 200
            view = await transcript(host, identifier)
            assert len(view["entries"]) == 2  # hide/archive never erase memory
            assert (await manage("restore-hidden", view["session_version"], "active")).status_code == 200
            other = (await open_session(host, "other-session")).json()["resource_id"]
            assert (await say(host, "other-input", other, 1, "Other conversation stays")).status_code == 200
            response = await host.env.client.get("/api/v1/work-session-sources/" + identifier,
                                                params=scope_params(host.scope))
            assert response.status_code == 200, response.text
            selection = response.json()["sources"]
            assert len(selection) == 2  # rejected input and other conversation excluded
            source_ids = [row[0] for row in selection]
            made = await post(host.env, dict(type="memory.delete.preview", request_id="forget-preview",
                scope=host.scope.model_dump(mode="json"), target=selection[0][0],
                expected_version=selection[0][1], arguments=dict(schema_id="memory.delete-args",
                    data=dict(source_ids=source_ids))))
            assert made.status_code == 200, made.text
            plan_id = made.json()["resource_id"]
            execute = dict(type="memory.delete.execute", request_id="forget-execute",
                scope=host.scope.model_dump(mode="json"), target=selection[0][0],
                expected_version=1, arguments=dict(plan_id=plan_id, plan_version=1))
            done = await post(host.env, execute)
            assert done.status_code == 200, done.text
            assert done.json()["status"] == "completed", done.text
            assert (await post(host.env, execute)).json() == done.json()
            sources = await host.runtime.store.repository(Source, PRINCIPALS[Owner.MEMORY],
                                                          host.scope).list(limit=200)
            assert not set(source_ids).intersection(row.id for row in sources)
            other_view = await transcript(host, other)
            assert other_view["entries"][0]["text"] == "Other conversation stays"
            assert model.count("activity") == 0
    asyncio.run(scenario())
