"""Skills in a working session (MCP operations) on the production composition.

Real build, SQLite, deletion ledger, file owner, R1 session driver with a synthetic harness
over the shared gateway (scripted transport, no network, no paid calls). The harness calls the
workspace bridge inside real session turns, as OpenCode does through MCP.

Turn 1 saves a method (experimental; replay is the same, another save on the old version
conflicts). Turn 2 finds and reads it, then improves it (CAS). The read method is recorded in
the attempt before the text is returned and the written method among its results. Revoking the
basis of the method (the operator's message) stops that conversation; in a new one the
method is neither shown nor read. After STOP an old admission does nothing.
"""

import asyncio
import json

import pytest
from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from ai_agent_application.storage import Owner
from ai_agent_contracts.agent_workplace import WorkAttempt
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from session_support import ack, install
from test_activity import WorkModel, fast, prepared, until
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.dialogue import memory
from ai_agent_core.m3_composition.state import PRINCIPALS, command
from ai_agent_core.m3_composition.work_session_workspace import Workspace

METHOD = "1. Контур башни.\n2. Свет фонаря.\n"
BETTER = "1. Контур башни.\n2. Свет фонаря.\n3. Тень на воде.\n"


def save(call_id, method, expected=None, **extra):
    arguments = dict(
        skill_id="sketch",
        title="Набросок маяка",
        applicability="Быстро зарисовать маяк по памяти.",
        method=method,
        **extra,
    )
    if expected is not None:
        arguments["expected_version"] = str(expected)
    return WorkspaceRequest(call_id, "save_skill", arguments)


def test_session_saves_finds_reads_and_improves_its_own_skill(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        seen: dict[int, dict] = {}
        gates: list = []
        failures: list = []

        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope, areas=["skills"])
            original_factory = rt.session_harness

            async def factory(ex, gateway, gate):
                harness = await original_factory(ex, gateway, gate)
                original_turn = harness.turn

                async def turn(admission, instruction):
                    bridge, number = Workspace(ex, gate), len(seen) + 1
                    gates.append((bridge, gate))
                    out: dict = {}
                    try:
                        if number == 1:
                            out["saved"] = json.loads(await bridge.call(gate, save("s1", METHOD)))
                            out["replay"] = json.loads(await bridge.call(gate, save("s1", METHOD)))
                            try:
                                await bridge.call(gate, save("s1b", "Другое."))
                            except ContractError as error:
                                out["conflict"] = error.error.code
                        elif number == 2:
                            found = await bridge.call(
                                gate, WorkspaceRequest("f", "find_skills", {"query": "маяк"})
                            )
                            out["found"] = json.loads(found)
                            read = await bridge.call(
                                gate, WorkspaceRequest("r", "read_skill", {"skill_id": "sketch"})
                            )
                            out["read"] = json.loads(read)
                            out["better"] = json.loads(
                                await bridge.call(gate, save("s2", BETTER, expected=1))
                            )
                            try:
                                await bridge.call(gate, save("s3", "Устаревшее.", expected=1))
                            except ContractError as error:
                                out["stale"] = error.error.code
                        elif number == 3:
                            out["found"] = json.loads(
                                await bridge.call(
                                    gate, WorkspaceRequest("f3", "find_skills", {"query": "маяк"})
                                )
                            )
                            try:
                                await bridge.call(
                                    gate,
                                    WorkspaceRequest("r3", "read_skill", {"skill_id": "sketch"}),
                                )
                            except ContractError as error:
                                out["read"] = error.error.code
                    except Exception as error:  # noqa: BLE001 - surfaced by the test below
                        failures.append(error)
                    seen[number] = out
                    return await original_turn(admission, instruction)

                harness.turn = turn
                return harness

            rt.session_harness = factory
            session_id = (await open_session(host, "skills-session")).json()["resource_id"]

            async def exchange(number, text, reply, session_id=session_id):
                view = await transcript(host, session_id)
                said = await say(host, f"say-{number}", session_id, view["session_version"], text)
                assert said.status_code == 200, said.text
                model.queues["activity"].append({"reply": reply})
                if number == 1:
                    await host.start()
                draft = await until(
                    lambda: transcript(host, session_id),
                    lambda v: (
                        v.get("pending_reply") is not None and v["pending_reply"]["acknowledgeable"]
                    ),
                    f"draft {number}",
                    seconds=60,
                )
                if failures:
                    raise failures[0]
                assert (await ack(host, draft["pending_reply"], f"ack-{number}")).status_code == 200

            await exchange(1, "Заведи метод наброска маяка.", "Метод сохранён.")
            first = seen[1]
            assert first["saved"] == {"skill_id": "sketch", "version": 1, "status": "experimental"}
            assert first["replay"] == first["saved"]  # same call: owner replay, no second write
            assert first["conflict"] is ErrorCode.VERSION_CONFLICT
            skill = await rt.workplace.skills.get(scope, "sketch")
            assert (skill.version, skill.status, skill.method.version) == (1, "experimental", 1)

            await exchange(2, "Найди свой метод и улучши его.", "Метод улучшен.")
            second = seen[2]
            assert [s["skill_id"] for s in second["found"]] == ["sketch"]
            assert second["read"]["method"] == METHOD and second["read"]["status"] == "experimental"
            assert second["better"] == {
                "skill_id": "sketch",
                "version": 2,
                "status": "experimental",
            }
            assert second["stale"] is ErrorCode.VERSION_CONFLICT
            improved = await rt.workplace.skills.get(scope, "sketch")
            assert (improved.version, improved.method.version) == (2, 2)
            assert improved.status == "experimental"  # a model's save never verifies a skill
            # The read method went into the attempt's inputs, the written one into its results.
            attempts = await rt.store.repository(
                WorkAttempt, PRINCIPALS[Owner.ACTIVITY], scope
            ).list(limit=50)
            assert any(
                skill.method in a.input_files and improved.method in a.result_files
                for a in attempts
            )

            # Revoke the basis the method was written from (the operator's message): the skill
            # is no longer shown, read or used.
            [method_file] = await rt.workplace.files.list(scope, "skills", limit=10)
            assert method_file.dependencies  # the method keeps what it was written from
            for basis in method_file.dependencies:
                await memory(rt).set_rights(
                    command(scope, "revoke-" + basis.source_id[-8:]),
                    basis.source_id,
                    basis.version,
                    read_allowed=True,
                    api_transfer_allowed=False,
                    public_disclosure_allowed=False,
                )
            # The conversation built on the revoked message gets no further turn (session owner,
            # R1); the check of the skill continues in a new conversation.
            # A new conversation: the method made from the revoked basis is neither shown nor read.
            fresh = (await open_session(host, "skills-session-2")).json()["resource_id"]
            await exchange(3, "Что с методом?", "Метода нет.", session_id=fresh)
            third = seen[3]
            assert third["found"] == [] and third["read"] in (
                ErrorCode.ACCESS_DENIED,
                ErrorCode.STALE_CONTEXT,
            )

            # STOP: an old admission cannot act any more.
            run = rt.supervisor.snapshot
            stopped = await host.env.client.post(
                f"/api/v1/agents/{run.agent_id}/stop",
                json=dict(
                    type="agent.stop",
                    request_id="skills-stop",
                    target=run.agent_id,
                    scope=run.scope.model_dump(mode="json"),
                ),
                headers=host.env.headers,
            )
            assert stopped.status_code == 200, stopped.text
            bridge, gate = gates[-1]
            with pytest.raises(ContractError):
                await bridge.call(gate, save("late", "Поздно.", expected=2))
            assert (await rt.workplace.skills.get(scope, "sketch")).version == 2

    asyncio.run(scenario())
