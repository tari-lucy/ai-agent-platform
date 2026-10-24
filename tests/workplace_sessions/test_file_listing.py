"""File discovery inside an admitted session on the production host, no paid calls."""

import asyncio
import json

from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from session_support import install
from test_activity import WorkModel, fast, prepared, until
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_workspace import Workspace


def test_session_lists_empty_areas_and_discovers_its_saved_note(tmp_path, monkeypatch):
    async def scenario():
        model = WorkModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/events.sqlite"
        )
        await enable_workplace_files(host.root)
        results, failures = {}, []
        async with host.open():
            rt, scope = host.runtime, host.scope
            fast(rt)
            await install(rt, scope, areas=["knowledge", "inbox", "projects"])
            original_factory = rt.session_harness

            async def factory(ex, gateway, gate):
                harness = await original_factory(ex, gateway, gate)
                original_turn = harness.turn

                async def turn(admission, instruction):
                    bridge = Workspace(ex, gate)
                    try:
                        for area, expected in (
                            ("", ErrorCode.INVALID_ARGUMENTS),
                            ("knowledge/", ErrorCode.INVALID_ARGUMENTS),
                            ("knowledge,inbox", ErrorCode.INVALID_ARGUMENTS),
                            ("skills", ErrorCode.ACCESS_DENIED),
                        ):
                            try:
                                await bridge.call(
                                    gate, WorkspaceRequest("invalid", "list_files", {"area": area})
                                )
                            except ContractError as error:
                                assert error.error.code == expected
                            else:
                                raise AssertionError("Invalid or ungranted area was accepted")
                        for area in ("knowledge", "inbox", "projects"):
                            results[area] = json.loads(
                                await bridge.call(
                                    gate, WorkspaceRequest(area, "list_files", {"area": area})
                                )
                            )
                        await bridge.call(
                            gate,
                            WorkspaceRequest(
                                "write",
                                "write_note",
                                {
                                    "file_id": "synthetic-note",
                                    "path": "note.md",
                                    "title": "Synthetic note",
                                    "body": "Synthetic field observation.",
                                },
                            ),
                        )
                        results["after"] = json.loads(
                            await bridge.call(
                                gate, WorkspaceRequest("after", "list_files", {"area": "knowledge"})
                            )
                        )
                    except Exception as error:  # noqa: BLE001 - raised in the test below
                        failures.append(error)
                    return await original_turn(admission, instruction)

                harness.turn = turn
                return harness

            rt.session_harness = factory
            session = (await open_session(host, "open-list")).json()["resource_id"]
            await say(host, "list-question", session, 1, "Inspect the synthetic workspace.")
            model.queues["activity"].append({"reply": "Synthetic result."})
            await host.start()
            await until(
                lambda: transcript(host, session),
                lambda v: v.get("pending_reply") is not None,
                "file listing",
                seconds=60,
            )
            if failures:
                raise failures[0]
            assert all(results[area] == [] for area in ("knowledge", "inbox", "projects"))
            assert [entry["id"] for entry in results["after"]] == ["synthetic-note"]

    asyncio.run(scenario())
