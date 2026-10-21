"""R3 work conversation over real HTTP: production build, SQLite, memory and activity owners.

Synthetic data and a scripted transport that is never called: intake while STOPPED stores the
operator's text with the memory owner and only its reference in the session. Checked: replay
and conflicts by request id and version, the transcript through the release gate, close, and
that nothing starts or calls a model. The person's replies come with the R1 session driver.
"""

import asyncio

from ai_agent_application.storage import Owner
from ai_agent_contracts.memory import Source
from test_activity import WorkModel, prepared
from test_http import post, scope_params

from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command
from ai_agent_core.m3_composition.work_sessions import sessions


def open_session(host, request_id, title="Конспект справочника"):
    return post(
        host.env,
        dict(
            type="work.session.open",
            request_id=request_id,
            target="work",
            scope=host.scope.model_dump(mode="json"),
            arguments=dict(schema_id="work.session.open-args", data={"title": title}),
        ),
    )


def say(host, request_id, session_id, version, text):
    return post(
        host.env,
        dict(
            type="work.session.message",
            request_id=request_id,
            target=session_id,
            scope=host.scope.model_dump(mode="json"),
            expected_version=version,
            arguments=dict(
                schema_id="work.session.message-args",
                data={"session_id": session_id, "text": text},
            ),
        ),
    )


async def transcript(host, session_id):
    response = await host.env.client.get(
        "/api/v1/work-session-transcript/" + session_id, params=scope_params(host.scope)
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_conversation_intake_replay_conflict_release_and_close(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        async with host.open():
            rt = host.runtime
            opened = await open_session(host, "open-1")
            assert opened.status_code == 200, opened.text
            session_id = opened.json()["resource_id"]
            assert (await open_session(host, "open-1")).json() == opened.json()  # replay

            first = await say(host, "say-1", session_id, 1, "Давай начнём с оглавления.")
            assert first.status_code == 200, first.text
            again = await say(host, "say-1", session_id, 1, "Давай начнём с оглавления.")
            assert again.json() == first.json()  # a lost reply retried: one message
            changed = await say(host, "say-1", session_id, 1, "Другой текст.")
            assert changed.status_code == 409, changed.text
            stale = await say(host, "say-2", session_id, 1, "Устаревший вид.")
            assert stale.status_code == 409, stale.text
            # The refused message left no loose text in memory: its source was retired
            # (an expired source keeps no content and is not listed).
            stored = {
                src.content: src.availability
                for src in await rt.store.repository(
                    Source, PRINCIPALS[Owner.MEMORY], host.scope
                ).list(limit=200)
                if src.source_type == "work-session-message"
            }
            assert "Устаревший вид." not in stored
            second = await say(host, "say-3", session_id, 2, "И отметь спорные места.")
            assert second.status_code == 200, second.text

            shown = await transcript(host, session_id)
            assert [(e["sequence"], e["speaker"], e["text"]) for e in shown["entries"]] == [
                (1, "operator", "Давай начнём с оглавления."),
                (2, "operator", "И отметь спорные места."),
            ]
            assert shown["complete"] is True and shown["session_version"] == 3
            # Intake starts nothing and calls no model; the run stays STOPPED.
            assert model.count() == 0
            assert rt.supervisor.snapshot.state.value == "STOPPED"

            # The memory owner withdraws one message's transfer: its text is withheld.
            [row, _] = await sessions(rt).messages(host.scope, session_id)
            await memory(rt).set_rights(
                command(host.scope, "revoke-message"),
                row.source.source_id,
                row.source.version,
                read_allowed=True,
                api_transfer_allowed=False,
                public_disclosure_allowed=False,
            )
            shown = await transcript(host, session_id)
            assert [(e["withheld"], e["text"]) for e in shown["entries"]] == [
                (True, None),
                (False, "И отметь спорные места."),
            ]

            closed = await post(
                host.env,
                dict(
                    type="work.session.close",
                    request_id="close-1",
                    target=session_id,
                    scope=host.scope.model_dump(mode="json"),
                    expected_version=3,
                    arguments=dict(
                        schema_id="work.session.close-args", data={"session_id": session_id}
                    ),
                ),
            )
            assert closed.status_code == 200, closed.text
            late = await say(host, "say-4", session_id, 4, "После закрытия.")
            assert late.status_code >= 400, late.text
            texts = [
                src.content
                for src in await rt.store.repository(
                    Source, PRINCIPALS[Owner.MEMORY], host.scope
                ).list(limit=200)
                if src.source_type == "work-session-message"
            ]
            assert "После закрытия." not in texts
            read = await host.env.client.get(
                "/api/v1/work-sessions", params=scope_params(host.scope)
            )
            assert [(s["id"], s["state"]) for s in read.json()["items"]] == [(session_id, "closed")]
        async with host.open():
            assert host.runtime.supervisor.snapshot.state.value == "STOPPED"
            assert (await transcript(host, session_id))["state"] == "closed"

    asyncio.run(scenario())


def test_conversation_needs_the_activity_contour(tmp_path, monkeypatch):
    async def scenario():
        from quality_support import Model, prepared_host

        host = await prepared_host(tmp_path, Model(), monkeypatch)
        async with host.open():
            refused = await open_session(host, "open-1")
            assert refused.status_code >= 400 and "CAPABILITY_UNAVAILABLE" in refused.text

    asyncio.run(scenario())
