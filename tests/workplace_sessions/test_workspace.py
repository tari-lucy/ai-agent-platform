"""Owner bridge lineage/refusals over real SQLite, blobs and deletion ledger."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from ai_agent_application.agent_activity.book import ActivityBook
from ai_agent_application.agent_knowledge import KnowledgeBase
from ai_agent_application.agent_workplace.ports import WorkspaceRequest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from wf_support import ACT, AREA, run

from ai_agent_core.m3_composition import work_session_workspace as module


async def setup(w, monkeypatch, sources=()):
    gate = await w.admit(sources)
    owner = ActivityBook(w.store, w.clock, principal=ACT)
    attempt = await w.attempt(gate.scope, gate.attempt_id)
    await owner.settle(attempt, "submitted")
    rt = SimpleNamespace(
        store=w.store,
        workplace=SimpleNamespace(files=w.files, knowledge=KnowledgeBase(w.files)),
        tickets=object(),
        notify=lambda *a: None,
        autonomous_wake=None,
    )

    async def verify():
        if not w.active:
            raise ContractError(ErrorCode.STALE_CONTEXT)

    monkeypatch.setattr(module, "book", lambda _: owner)
    monkeypatch.setattr(module, "Release", lambda _: w.guard)
    ex = SimpleNamespace(runtime=rt, verify=verify, extra_sources=tuple(sources))
    return module.Workspace(ex, gate), gate, owner, ex


def test_read_before_write_records_all_bases_and_revoke_blocks_next_call(tmp_path, monkeypatch):
    async def scenario(w):
        first, second = await w.source(), await w.source()
        write_gate = await w.admit([first])
        change, data = w.change()
        entry = await w.files.write(write_gate, change, data)
        bridge, gate, owner, ex = await setup(w, monkeypatch, [second])
        text = await bridge.call(gate, WorkspaceRequest("read", "read_file", {"file_id": entry.id}))
        assert data.decode() in json.loads(text)["text"]
        attempt = await owner.attempts_repo(gate.scope).get(gate.attempt_id)
        assert set(r.source_id for r in attempt.inputs) == {first.source_id, second.source_id}
        assert (
            attempt.input_files == [entry.reference] and tuple(attempt.inputs) == ex.extra_sources
        )
        request = WorkspaceRequest(
            "write",
            "write_note",
            dict(
                file_id="derived",
                path="derived.md",
                title="Derived",
                body="Synthetic derived note.",
            ),
        )
        assert json.loads(await bridge.call(gate, request))["id"] == "derived"
        # Exact repeat is owner-idempotent; a different body with that id is refused.
        assert json.loads(await bridge.call(gate, request))["version"] == 1
        with pytest.raises(ContractError):
            await bridge.call(
                gate, replace(request, arguments=dict(request.arguments, body="Changed"))
            )
        derived = next(e for e in await w.files.list(AREA, "knowledge") if e.id == "derived")
        assert {r.source_id for r in derived.dependencies} == {first.source_id, second.source_id}
        await w.ledger.block(AREA, first.source_id, 1, "rights_revoked", w.now)
        with pytest.raises(ContractError):
            await bridge.call(gate, WorkspaceRequest("again", "read_file", {"file_id": "derived"}))

    run(tmp_path, scenario)


@pytest.mark.parametrize("failure", ["private_only", "stop", "foreign", "scope_argument"])
def test_closed_release_and_bound_admission(tmp_path, monkeypatch, failure):
    async def scenario(w):
        ref = await w.source(api=failure != "private_only")
        write_gate = await w.admit([ref])
        change, data = w.change()
        entry = await w.files.write(write_gate, change, data)
        bridge, gate, _, _ = await setup(w, monkeypatch)
        if failure == "stop":
            w.active = False
        if failure == "foreign":
            gate = replace(gate, operation_id="foreign")
        arguments = {"file_id": entry.id}
        if failure == "scope_argument":
            arguments["scope"] = "another-person"
        with pytest.raises(ContractError):
            await bridge.call(gate, WorkspaceRequest("read", "read_file", arguments))

    run(tmp_path, scenario)


def test_uploaded_text_reads_exact_chunks_and_records_file_lineage(tmp_path, monkeypatch):
    async def scenario(w):
        entry = await w.files.operator_write(
            AREA,
            "knowledge",
            "uploaded.txt",
            "Upload",
            "text/plain",
            "Начало. Середина. Конец.".encode(),
            file_id="upload",
            request_id="upload-1",
            authorize=lambda: None,
        )
        bridge, gate, owner, ex = await setup(w, monkeypatch)
        first = json.loads(
            await bridge.call(
                gate,
                WorkspaceRequest(
                    "chunk-1", "read_file", dict(file_id=entry.id, offset="0", length="8")
                ),
            )
        )
        second = json.loads(
            await bridge.call(
                gate,
                WorkspaceRequest(
                    "chunk-2",
                    "read_file",
                    dict(file_id=entry.id, offset=str(first["next_offset"]), length="4000"),
                ),
            )
        )
        assert first["text"] + second["text"] == "Начало. Середина. Конец."
        assert second["next_offset"] is None and not second["truncated"]
        assert first["checksum"] == entry.reference.checksum
        attempt = await owner.attempts_repo(gate.scope).get(gate.attempt_id)
        assert entry.reference in attempt.input_files
        for args in (dict(offset="-1"), dict(length="4001"), dict(version="0")):
            with pytest.raises(ContractError):
                await bridge.call(
                    gate,
                    WorkspaceRequest(
                        "invalid-" + str(args), "read_file", dict(file_id=entry.id, **args)
                    ),
                )

    run(tmp_path, scenario)
