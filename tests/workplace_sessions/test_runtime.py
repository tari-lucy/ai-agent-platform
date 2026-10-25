"""Production host work-session scheduling and ACK, synthetic harness/provider only."""

import asyncio

from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkAttempt
from ai_agent_contracts.memory import Episode, Source
from session_support import ack, install
from test_activity import WorkModel, fast, prepared, until
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.m3_composition.state import PRINCIPALS


def test_session_response_waits_for_exact_ack_and_uses_existing_owners(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            fast(rt)
            failures = []
            original_perform = rt._perform

            async def observed(execution):
                try:
                    return await original_perform(execution)
                except Exception as error:
                    failures.append(error)
                    raise

            monkeypatch.setattr(rt, "_perform", observed)
            from ai_agent_core.m3_composition import work_session_delivery

            original_ack = work_session_delivery.acknowledge

            async def observed_ack(*args):
                try:
                    return await original_ack(*args)
                except Exception as error:
                    failures.append(error)
                    raise

            monkeypatch.setattr(work_session_delivery, "acknowledge", observed_ack)

            async def checked_view(identifier):
                if failures:
                    raise failures[0]
                return await transcript(host, identifier)

            await install(rt, host.scope)
            opened = await open_session(host, "session-runtime")
            identifier = opened.json()["resource_id"]
            assert (
                await say(host, "question", identifier, 1, "Synthetic lighthouse question")
            ).status_code == 200
            await asyncio.sleep(0.1)
            assert model.count("activity") == 0
            model.queues["activity"].append({"reply": "Synthetic response"})
            await host.start()
            view = await until(
                lambda: checked_view(identifier),
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "queued session response",
            )
            assert len(view["entries"]) == 1 and view["processed_sequence"] == 0
            assert "Synthetic response" in view["pending_reply"]["text"]
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            assert not any(s.source_type == "work-session-reply" for s in sources)
            original_reply = view["pending_reply"]
            response = await ack(host, view["pending_reply"])
            if failures:
                raise failures[0]
            assert response.status_code == 200, response.text
            again = await ack(host, view["pending_reply"])
            assert again.status_code == 200, again.text
            view = await transcript(host, identifier)
            assert view["pending_reply"] is None and len(view["entries"]) == 2
            assert view["entries"][-1]["speaker"] == "character" and view["processed_sequence"] == 2
            assert view["entries"][-1]["reply_id"] == original_reply["utterance_id"]
            assert view["entries"][-1]["at"] == original_reply["created_at"]
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            assert sum(s.source_type == "work-session-reply" for s in sources) == 1
            episodes = await rt.store.repository(
                Episode, PRINCIPALS[Owner.MEMORY], host.scope
            ).list(limit=200)
            assert (
                sum(
                    e.actual_outcome == "Панель подтвердила получение точной редакции."
                    for e in episodes
                )
                == 1
            )
            attempts = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], host.scope
            ).list(limit=200)
            assert (
                len(attempts) == 1
                and attempts[0].session_id == identifier
                and attempts[0].task_id is None
            )
            assert model.count("activity") == 1
            monkeypatch.setattr(work_session_delivery, "acknowledge", original_ack)

            # A new input supersedes a pending response. Its old ACK cannot enter memory.
            model.queues["activity"].append({"reply": "Superseded synthetic response"})
            assert (
                await say(
                    host, "question-two", identifier, view["session_version"], "Another question"
                )
            ).status_code == 200
            view = await until(
                lambda: checked_view(identifier),
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "second draft",
            )
            stale = view["pending_reply"]
            wrong = dict(stale, revision=stale["revision"] + 1)
            assert (await ack(host, wrong, "wrong-revision")).status_code == 409
            factory = rt.session_harness
            rt.session_harness = None  # hold scheduling while inspecting the superseded result
            assert (
                await say(
                    host, "question-three", identifier, view["session_version"], "Newer question"
                )
            ).status_code == 200
            assert (await ack(host, stale, "superseded-ack")).status_code == 409
            view = await transcript(host, identifier)
            assert view["pending_reply"] is None and len(view["entries"]) == 4

            # STOP revokes ACK for a completed draft, too. Restart never applies it itself.
            model.queues["activity"].append({"reply": "Stopped synthetic response"})
            rt.session_harness = factory
            view = await until(
                lambda: checked_view(identifier),
                lambda v: (
                    v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                ),
                "third draft",
            )
            stopped_draft = view["pending_reply"]
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                json=dict(
                    type="agent.stop",
                    request_id="session-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
                headers=host.env.headers,
            )
            assert stopped.status_code == 200, stopped.text
            assert (await ack(host, stopped_draft, "stopped-ack")).status_code != 200
            sources = await rt.store.repository(Source, PRINCIPALS[Owner.MEMORY], host.scope).list(
                limit=200
            )
            assert sum(s.source_type == "work-session-reply" for s in sources) == 1
            assert model.count("activity") == 3

        async with host.open():
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            view = await transcript(host, identifier)
            assert not view["pending_reply"]["acknowledgeable"]
            assert len(view["entries"]) == 4
            assert (await ack(host, stopped_draft, "restart-ack")).status_code != 200
            assert model.count("activity") == 3

    asyncio.run(scenario())
