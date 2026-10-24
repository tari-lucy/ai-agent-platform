"""A work conversation whose basis became unavailable: production host, synthetic provider.

Checked through the real build/SQLite/memory/activity owners and the session driver:
- closed earlier history is dropped and the context rebuilt; a draft on a closed basis is
  discarded and answered again in a new generation (the conversation and its tasks stay);
- a revoked, deleted or TTL-expired unprocessed operator message is never executed: the
  owner stops the conversation with a reason, with no job or model call and no retries;
- a revocation during the model call closes the late result; STOP/restart change nothing.
"""

import asyncio
from datetime import timedelta
from functools import partial

from ai_agent_application.agent_workplace.ports import HarnessEvent, ModelRequest
from ai_agent_application.llm import ChatMessage
from ai_agent_application.memory import SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkAttempt
from session_support import ack, install
from test_activity import ACTIVITY_MARKER, WorkModel, fast, prepared, until
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.m3_composition import work_sessions
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command


def ready(view):
    return view.get("pending_reply") is not None and view["pending_reply"]["acknowledgeable"]


def stopped(view):
    return view["state"] == "invalidated"


async def rows(rt, sid):
    return await work_sessions.sessions(rt).messages(rt.supervisor.snapshot.scope, sid, limit=200)


async def revoke(rt, ref, key):
    await memory(rt).set_rights(
        command(rt.supervisor.snapshot.scope, key),
        ref.source_id,
        ref.version,
        read_allowed=True,
        api_transfer_allowed=False,
        public_disclosure_allowed=False,
    )


async def attempts(rt, scope, sid):
    found = await rt.store.repository(WorkAttempt, PRINCIPALS[Owner.ACTIVITY], scope).list(
        limit=200
    )
    return [a for a in found if a.session_id == sid]


def submissions(rt, monkeypatch):
    seen: list[str] = []
    original = rt.submit

    async def spy(authority, request_id, request):
        seen.append(request_id)
        return await original(authority, request_id, request)

    monkeypatch.setattr(rt, "submit", spy)
    return seen


def prompt(model):
    return model.last("activity")["messages"][-1]["content"]


def test_transcript_handles_invalidation_between_its_two_owner_reads(tmp_path, monkeypatch):
    async def scenario():
        from ai_agent_application.agent_activity.sessions import SessionBook

        from ai_agent_core.m3_composition.work_session_runtime import settle_basis

        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            sid = (await open_session(host, "invalidation-read-race")).json()["resource_id"]
            await say(host, "race-input", sid, 1, "Synthetic removed question")
            ref = (await rows(rt, sid))[0].source
            await memory(rt).delete_source(
                command(scope, "remove-race"), ref.source_id, ref.version
            )
            session = await work_sessions.sessions(rt).get(scope, sid)
            original = SessionBook.entries
            changed = False

            def authorize(selected):
                assert selected.partition == scope.partition

            async def raced(book, selected, identifier, **kwargs):
                nonlocal changed
                if identifier == sid and not changed:
                    changed = True
                    await settle_basis(rt, session, "message", authorize)
                return await original(book, selected, identifier, **kwargs)

            monkeypatch.setattr(SessionBook, "entries", raced)
            view = await transcript(host, sid)
            assert changed and view["state"] == "invalidated"
            assert view["invalidated_reason"] == "message_unavailable"
            assert view["entries"] == [] and view["pending_reply"] is None
            assert model.count() == 0

    asyncio.run(scenario())


def test_closed_context_is_rebuilt_and_a_closed_message_stops_the_conversation(
    tmp_path, monkeypatch
):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope)
            sid = (await open_session(host, "rebuild")).json()["resource_id"]
            assert (await say(host, "m1", sid, 1, "Synthetic first question")).status_code == 200
            model.queues["activity"].append({"reply": "Synthetic first answer"})
            await host.start()
            view = await until(lambda: transcript(host, sid), ready, "first draft", seconds=60)
            assert (await ack(host, view["pending_reply"], "a1")).status_code == 200

            # 1. A part of the stored context (the earlier operator message) is revoked: it is
            # dropped, the context is rebuilt and the conversation goes on.
            first = (await rows(rt, sid))[0]
            await revoke(rt, first.source, "revoke-first")
            view = await transcript(host, sid)
            model.queues["activity"].append({"reply": "Synthetic second answer"})
            said = await say(host, "m2", sid, view["session_version"], "Synthetic second question")
            assert said.status_code == 200
            view = await until(lambda: transcript(host, sid), ready, "rebuilt draft", seconds=60)
            assert view["state"] == "open" and model.count("activity") == 2
            assert "Synthetic second question" in prompt(model)
            assert "Synthetic first question" not in prompt(model)
            generation = (await work_sessions.sessions(rt).get(scope, sid)).generation

            # 2. The undelivered draft's basis (the person's earlier reply) closes: the draft is
            # discarded, the context reset by its owner and the same message answered again.
            reply = (await rows(rt, sid))[1]
            assert reply.speaker == "character"
            model.queues["activity"].append({"reply": "Synthetic third answer"})
            await revoke(rt, reply.source, "revoke-reply")
            view = await until(
                lambda: transcript(host, sid),
                lambda v: ready(v) and "third" in v["pending_reply"]["text"],
                "answer in a new generation",
                seconds=60,
            )
            session = await work_sessions.sessions(rt).get(scope, sid)
            assert session.state == "open" and session.generation == generation + 1
            assert model.count("activity") == 3
            assert "Synthetic first answer" not in prompt(model)
            assert (await ack(host, view["pending_reply"], "a3")).status_code == 200
            view = await transcript(host, sid)
            assert view["processed_sequence"] == 4 and view["entries"][-1]["speaker"] == "character"

            # 3. The unprocessed operator message itself is deleted: never executed, the owner
            # stops the conversation with a reason; no job, no model call, no retry loop.
            factory = rt.session_harness
            rt.session_harness = None  # hold scheduling while the message is stored
            said = await say(host, "m3", sid, view["session_version"], "Synthetic deleted question")
            assert said.status_code == 200
            third = (await rows(rt, sid))[-1]
            await memory(rt).delete_source(
                command(scope, "delete-third"), third.source.source_id, third.source.version
            )
            seen = submissions(rt, monkeypatch)
            rt.session_harness = factory
            view = await until(lambda: transcript(host, sid), stopped, "stopped conversation")
            assert view["invalidated_reason"] == "message_unavailable"
            assert view["pending_reply"] is None and view["entries"] == []
            await asyncio.sleep(1.0)  # many scheduler ticks
            assert seen == [] and model.count("activity") == 3
            assert not [a for a in await attempts(rt, scope, sid) if a.session_sequence == 5]
            # The stopped conversation accepts nothing; a new one is the way to continue.
            refused = await say(host, "m4", sid, view["session_version"], "Into a stopped one")
            assert refused.status_code != 200
            fresh = (await open_session(host, "continue")).json()["resource_id"]
            model.queues["activity"].append({"reply": "Synthetic fresh answer"})
            assert (await say(host, "f1", fresh, 1, "Synthetic again")).status_code == 200
            await until(lambda: transcript(host, fresh), ready, "fresh draft", seconds=60)
            assert model.count("activity") == 4

            # 4. TTL: an unprocessed message whose term ends before it is taken stops the same way.
            monkeypatch.setattr(
                work_sessions, "SourceDraft", partial(SourceDraft, term=timedelta(seconds=2))
            )
            rt.session_harness = None
            short = (await open_session(host, "ttl")).json()["resource_id"]
            assert (await say(host, "t1", short, 1, "Synthetic short-lived")).status_code == 200
            await asyncio.sleep(2.5)
            rt.session_harness = factory
            view = await until(lambda: transcript(host, short), stopped, "expired message")
            assert view["invalidated_reason"] == "message_unavailable"
            assert model.count("activity") == 4

    asyncio.run(scenario())


def test_revocation_during_the_model_call_closes_the_late_result(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope)
            called, release = asyncio.Event(), asyncio.Event()

            class Harness:
                def __init__(self, gateway):
                    self.gateway = gateway

                async def turn(self, gate, turn):
                    reply = await self.gateway.generate(
                        gate,
                        ModelRequest(
                            "activity",
                            (
                                ChatMessage("system", ACTIVITY_MARKER),
                                ChatMessage("user", turn.instruction),
                            ),
                            max_output_tokens=64,
                            call_id="session-answer",
                        ),
                    )
                    called.set()
                    await release.wait()  # the operator revokes the message meanwhile
                    return [HarnessEvent("result", reply.text)]

                async def close(self, key):
                    pass

                async def cancel(self, key):
                    pass

            async def factory(execution, gateway, gate):
                return Harness(gateway)

            rt.session_harness = factory
            sid = (await open_session(host, "late")).json()["resource_id"]
            assert (await say(host, "m1", sid, 1, "Synthetic revoked question")).status_code == 200
            model.queues["activity"].append({"reply": "Synthetic late answer"})
            await host.start()
            await asyncio.wait_for(called.wait(), 60)
            await revoke(rt, (await rows(rt, sid))[0].source, "revoke-in-flight")
            release.set()
            view = await until(lambda: transcript(host, sid), stopped, "late result closed")
            assert view["invalidated_reason"] == "message_unavailable"
            assert view["pending_reply"] is None
            # The refused job ends and the next tick reconciles its attempt; it never stays
            # submitted and is not retried.
            settled = await until(
                lambda: attempts(rt, scope, sid),
                lambda found: all(a.state not in ("prepared", "submitted") for a in found),
                "in-flight attempt reconciled",
            )
            assert [a.state for a in settled] == ["cancelled"]  # the late result is refused
            from ai_agent_contracts.control import Utterance

            drafts = await rt.store.repository(Utterance, PRINCIPALS[Owner.GATEWAY], scope).list(
                limit=200
            )
            assert not [u for u in drafts if "late answer" in u.content]
            await asyncio.sleep(1.0)
            assert model.count("activity") == 1  # no retry on a known-closed basis

            run = rt.supervisor.snapshot
            response = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                json=dict(
                    type="agent.stop",
                    request_id="late-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
                headers=host.env.headers,
            )
            assert response.status_code == 200, response.text

        async with host.open():
            rt = host.runtime
            assert rt.supervisor.snapshot.state.value == "STOPPED"
            view = await transcript(host, sid)
            assert view["state"] == "invalidated"
            assert view["invalidated_reason"] == "message_unavailable"
            fresh = (await open_session(host, "after-restart")).json()["resource_id"]
            assert (await say(host, "r1", fresh, 1, "Synthetic stored")).status_code == 200
            await asyncio.sleep(0.5)
            assert model.count("activity") == 1  # restart starts nothing by itself
            assert (await transcript(host, fresh))["state"] == "open"

    asyncio.run(scenario())
