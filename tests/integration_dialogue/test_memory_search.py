"""Real local recall HTTP bridge: server index, authoritative rights, no generation."""

import asyncio
from datetime import timedelta

from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.states import Provenance
from integration_support import prepared

from ai_agent_core.m3_composition.state import PRINCIPALS, command, put


def test_search_beyond_loaded_page_and_revoked_or_expired_source_stays_closed(tmp_path):
    async def scenario():
        async with prepared(tmp_path) as env:
            rt, scope = env.runtime, env.world.scope
            memory = MemoryService(
                rt.store,
                PRINCIPALS[Owner.MEMORY],
                rt.registry,
                index=env.index,
                reader_access=rt.readers,
                clock=rt.supervisor.clock.utc,
            )
            created = {}
            for index in range(55):
                word = f"указательметка{index}"
                result = await memory.register_source(
                    command(scope, f"search-seed-{index}"),
                    SourceDraft(
                        provenance=Provenance.OPERATOR_INPUT,
                        source_type="search-fixture",
                        adapter_id="fixture",
                        adapter_version="1",
                        occurred_at=rt.supervisor.clock.utc(),
                        content=f"Вымышленный {word}.",
                        read_allowed=True,
                        api_transfer_allowed=True,
                        public_disclosure_allowed=False,
                        external_id=str(index),
                    ),
                )
                created[result.source_id] = word
            q = dict(
                zip(
                    ("workspace_id", "persona_id", "memory_space_id", "memory_mode"),
                    scope.partition,
                    strict=True,
                )
            )
            first = await env.client.get("/api/v1/sources", params=q)
            loaded = {r["id"] for r in first.json()["items"]}
            identifier = next(i for i in created if i not in loaded)

            async def search(**extra):
                body = dict(
                    scope=scope.model_dump(mode="json"),
                    query=created[identifier],
                    limit=12,
                    context_token_limit=2000,
                    purpose="api_transfer",
                )
                body.update(extra)
                return await env.client.post(
                    "/api/v1/memory/recall", json=body, headers=env.headers
                )

            found = await search()
            assert found.status_code == 200, found.text
            assert [f["source"]["source_id"] for f in found.json()["fragments"]] == [identifier]
            assert found.json()["index_state"] == "degraded"  # deliberate FTS-only
            assert found.json()["missing_source_ids"] == []
            source = await memory.get_source(scope, identifier)
            # Leave the candidate in the index while changing authoritative rights/version.
            raw = MemoryService(
                rt.store, PRINCIPALS[Owner.MEMORY], rt.registry, clock=rt.supervisor.clock.utc
            )
            await raw.set_rights(
                command(scope, "revoke-search"),
                identifier,
                source.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            denied = await search()
            assert denied.status_code == 200 and denied.json()["fragments"] == []
            assert identifier not in denied.text and created[identifier] not in denied.text
            assert not env.world.provider.calls
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            # Authentication/CSRF and constrained purpose/scope remain enforced.
            for change in (
                {"purpose": "private_context"},
                {"scope": {**scope.model_dump(mode="json"), "persona_id": "foreign"}},
                {"query": " "},
                {"query": "x" * 1001},
                {"limit": 21},
            ):
                assert (await search(**change)).status_code in (400, 403, 422)
            assert (
                await env.client.post(
                    "/api/v1/memory/recall",
                    json=dict(
                        scope=scope.model_dump(mode="json"), query="x", purpose="api_transfer"
                    ),
                )
            ).status_code == 403
            # Another stale candidate expires before the search; no index rebuild is needed.
            live_id = next(i for i in created if i != identifier)
            live = await memory.get_source(scope, live_id)
            expired = live.model_copy(
                update={
                    "version": live.version + 1,
                    "updated_at": rt.supervisor.clock.utc(),
                    "retention": live.retention.model_copy(
                        update={"expires_at": rt.supervisor.clock.utc() - timedelta(seconds=1)}
                    ),
                }
            )
            await put(
                rt.store,
                rt.registry,
                Owner.MEMORY,
                scope,
                "expire-search",
                expired,
                live.version,
                rt.readers,
            )
            answer = await search(query=created[live_id])
            assert answer.status_code == 200 and answer.json()["fragments"] == []
            assert live_id not in answer.text

    asyncio.run(scenario())


def test_logout_while_search_waits_cannot_return_late_fragments(tmp_path, monkeypatch):
    async def scenario():
        async with prepared(tmp_path) as env:
            entered, release = asyncio.Event(), asyncio.Event()
            original = env.index.search

            async def blocked(*args, **kwargs):
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            monkeypatch.setattr(env.index, "search", blocked)
            scope = env.world.scope.model_dump(mode="json")
            pending = asyncio.create_task(
                env.client.post(
                    "/api/v1/memory/recall",
                    headers=env.headers,
                    json=dict(scope=scope, query="маяк", purpose="api_transfer"),
                )
            )
            await asyncio.wait_for(entered.wait(), 5)
            logged_out = await env.client.post("/api/v1/auth/logout", json={}, headers=env.headers)
            assert logged_out.status_code == 204
            release.set()
            result = await pending
            assert result.status_code == 403  # existing awaited-read revocation contract
            assert "fragments" not in result.text

    asyncio.run(scenario())
