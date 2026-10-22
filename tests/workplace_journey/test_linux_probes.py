"""R5 Linux/headless narrow probes of the work conversation (AW-11 acceptance list).

Production build, real owners, R1 synthetic session harness over the shared gateway, scripted
transport (no network, no paid calls).

- No budget: with the activity share spent, an operator message is stored and waits; the
  person's session makes no model call and shows no draft; with room again it answers.
- Cross-scope: a TEST partition neither opens a work conversation (no activity rows there)
  nor reads a WORK conversation's transcript.
"""

import asyncio
from dataclasses import replace

from session_support import install
from test_activity import WorkModel, fast, prepared, until
from test_http import scope_params
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.layout import load_config, paths


def test_spent_activity_share_holds_the_conversation_without_a_model_call(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            fast(rt)
            await install(rt, host.scope)
            rt.private_api_budget = replace(rt.private_api_budget, activity_daily_calls=1)
            session_id = (await open_session(host, "open-1")).json()["resource_id"]
            await say(host, "say-1", session_id, 1, "Первый вопрос.")
            model.queues["activity"].append({"reply": "Первый ответ."})
            await host.start()
            first = await until(
                lambda: transcript(host, session_id),
                lambda v: v.get("pending_reply") is not None,
                "first draft",
                seconds=30,
            )
            assert "Первый ответ." in first["pending_reply"]["text"]
            assert model.count("activity") == 1

            # The share (1 call today) is spent: the next message is stored and waits.
            superseding = await say(host, "say-2", session_id, first["session_version"], "Ещё?")
            assert superseding.status_code == 200, superseding.text
            await asyncio.sleep(2.0)
            held = await transcript(host, session_id)
            assert model.count("activity") == 1
            assert held["pending_reply"] is None
            assert held["entries"][-1]["speaker"] == "operator"
            assert held["processed_sequence"] < held["entries"][-1]["sequence"]
            status = await host.env.client.get(
                "/api/v1/private-api-status", params=scope_params(host.scope)
            )
            assert status.json()["activity_calls_used"] == 1
            assert status.json()["activity_daily_calls"] == 1

            # Room again (configuration, not a bypass): the waiting message is answered.
            model.queues["activity"].append({"reply": "Второй ответ."})
            rt.private_api_budget = replace(rt.private_api_budget, activity_daily_calls=5)
            second = await until(
                lambda: transcript(host, session_id),
                lambda v: v.get("pending_reply") is not None,
                "second draft",
                seconds=30,
            )
            assert "Второй ответ." in second["pending_reply"]["text"]
            assert model.count("activity") == 2

    asyncio.run(scenario())


def test_a_test_partition_neither_opens_nor_reads_work_conversations(tmp_path, monkeypatch):
    async def scenario():
        host = await prepared(tmp_path, monkeypatch, WorkModel())
        config = load_config(paths(host.root))
        lab = host.scope.model_copy(
            update={"memory_space_id": config.test_space_ids[0], "memory_mode": "test"}
        )
        async with host.open():
            session_id = (await open_session(host, "open-1")).json()["resource_id"]
            work = host.scope
            host.scope = lab
            try:
                refused = await open_session(host, "open-lab")
                assert refused.status_code >= 400, refused.text
                read = await host.env.client.get(
                    "/api/v1/work-session-transcript/" + session_id, params=scope_params(lab)
                )
                assert read.status_code >= 400, read.text
                listed = await host.env.client.get(
                    "/api/v1/work-sessions", params=scope_params(lab)
                )
                assert listed.status_code >= 400 or listed.json()["items"] == []
            finally:
                host.scope = work
            assert (await transcript(host, session_id))["session_id"] == session_id

    asyncio.run(scenario())
