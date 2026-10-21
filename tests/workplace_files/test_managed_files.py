"""Accepted WorkspaceFiles port, actual SQLite/files/ledger; synthetic subjects only."""

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest
from ai_agent_application.storage import Grant, Owner
from ai_agent_application.workspace_files import FileLimits, ManagedFiles
from ai_agent_contracts.agent_workplace import ManagedFile
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from wf_support import AREA, CAT, MEM, RUN, run


def test_create_read_search_revisions_and_replay(tmp_path):
    async def scenario(w):
        source = await w.source()
        admission = await w.admit([source])
        change, data = w.change()
        first = await w.files.write(admission, change, data)
        assert first.dependencies == [source]
        assert first.access.api_transfer
        assert (await w.files.read(admission, first.id))[1] == data
        assert (await w.files.list(AREA, "knowledge"))[0] == first
        assert (await w.files.search(AREA, "lighthouse"))[0] == first
        assert await w.files.write(admission, change, data) == first
        modified, new_data = w.change(b"A different note.", request="write-2", expected=1)
        second = await w.files.write(admission, modified, new_data)
        assert second.version == 2 and second.dependencies == [source]
        assert (await w.files.read(admission, first.id, 1))[1] == data
        assert (await w.files.read(admission, first.id))[1] == new_data
        with pytest.raises(ContractError) as conflict:
            await w.files.write(
                admission, modified.model_copy(update={"request_id": "third"}), new_data
            )
        assert conflict.value.error.code is ErrorCode.VERSION_CONFLICT
        altered, altered_data = w.change(b"Altered content.")
        with pytest.raises(ContractError) as reused:
            await w.files.write(admission, altered, altered_data)
        assert reused.value.error.code is ErrorCode.IDEMPOTENCY_CONFLICT

    run(tmp_path, scenario)


def test_dependencies_cannot_be_removed_by_a_declared_empty_list(tmp_path):
    async def scenario(w):
        source = await w.source()
        first_admission = await w.admit([source])
        first_change, data = w.change()
        first = await w.files.write(first_admission, first_change, data)
        derived_admission = await w.admit(files=[first.reference])
        derived, derived_data = w.change(b"A derived method.", file_id="method", request="derived")
        stored = await w.files.write(derived_admission, derived, derived_data)
        assert stored.dependencies == [source] and stored.derived_from == [first.reference]
        await w.ledger.block(AREA, source.source_id, 1, "operator_delete", w.now)
        assert await w.files.list(AREA, "knowledge") == []
        with pytest.raises(ContractError):
            await w.files.read(derived_admission, stored.id)

    run(tmp_path, scenario)


def test_stop_during_file_io_does_not_publish_catalogue(tmp_path, monkeypatch):
    async def scenario(w):
        admission = await w.admit()
        change, data = w.change()
        original = w.blobs.put

        async def stop_after_put(*args):
            await original(*args)
            w.active = False

        monkeypatch.setattr(w.blobs, "put", stop_after_put)
        with pytest.raises(ContractError) as stopped:
            await w.files.write(admission, change, data)
        assert stopped.value.error.code is ErrorCode.STALE_CONTEXT
        assert await w.store.repository(ManagedFile, CAT, AREA).get(change.file_id) is None
        assert await w.blobs.used_bytes(AREA) == len(data)  # bounded orphan, not a visible file

    run(tmp_path, scenario)


@pytest.mark.parametrize("during", ["read", "put"])
def test_source_revocation_during_io_closes_release(tmp_path, monkeypatch, during):
    async def scenario(w):
        source = await w.source()
        admission = await w.admit([source])
        change, data = w.change()
        if during == "read":
            await w.files.write(admission, change, data)
        original = getattr(w.blobs, during)

        async def revoke(*args, **kwargs):
            result = await original(*args, **kwargs)
            await w.ledger.block(AREA, source.source_id, 1, "rights_revoked", w.now)
            return result

        monkeypatch.setattr(w.blobs, during, revoke)
        with pytest.raises(ContractError) as denied:
            if during == "read":
                await w.files.read(admission, change.file_id)
            else:
                await w.files.write(admission, change, data)
        assert denied.value.error.code is ErrorCode.ACCESS_DENIED
        assert await w.files.list(AREA, "knowledge") == []

    run(tmp_path, scenario)


def test_actor_and_sql_grants_are_both_required(tmp_path):
    async def scenario(w):
        admission = await w.admit()
        change, data = w.change()
        w.allowed = False
        with pytest.raises(ContractError) as denied:
            await w.files.write(admission, change, data)
        assert denied.value.error.code is ErrorCode.ACCESS_DENIED
        w.allowed = True
        await w.store.provision(AREA, (Grant(MEM, Owner.MEMORY, read=True, write=True),))
        with pytest.raises(ContractError) as denied:
            await w.files.write(admission, change, data)
        assert denied.value.error.code is ErrorCode.ACCESS_DENIED

    run(tmp_path, scenario)


def test_work_test_and_persona_are_physically_separate(tmp_path):
    async def scenario(w):
        admission = await w.admit()
        change, data = w.change()
        await w.files.write(admission, change, data)
        for changes in ({"memory_mode": "test"}, {"persona_id": "someone-else"}):
            other = RUN.model_copy(update=changes)
            with pytest.raises(ContractError):
                await w.files.read(replace(admission, scope=other), change.file_id)
            assert await w.files.list(other, "knowledge") == []
        assert len(list(w.blobs.root.iterdir())) == 1

    run(tmp_path, scenario)


def test_api_right_is_not_inferred_from_private_read(tmp_path):
    async def scenario(w):
        source = await w.source(api=False)
        admission = await w.admit([source])
        change, data = w.change()
        entry = await w.files.write(admission, change, data)
        assert not entry.access.api_transfer
        assert (await w.files.read(admission, entry.id))[1] == data
        with pytest.raises(ContractError):
            await w.guard.verify(admission, (), [entry.reference], "api_transfer")

    run(tmp_path, scenario)


@pytest.mark.parametrize("kind", ["files", "versions", "bytes"])
def test_configurable_quota_survives_hidden_records(tmp_path, kind):
    limits = {
        "files": FileLimits(100, 200, 1, 20),
        "versions": FileLimits(100, 200, 10, 1),
        "bytes": FileLimits(30, 30, 10, 20),
    }[kind]

    async def scenario(w):
        source = await w.source()
        admission = await w.admit([source])
        change, data = w.change()
        await w.files.write(admission, change, data)
        if kind == "files":
            await w.ledger.block(AREA, source.source_id, 1, "operator_delete", w.now)
            assert await w.files.list(AREA, "knowledge") == []
            admission = await w.admit()
        second, payload = w.change(
            b"A new independent payload.",
            file_id="note" if kind == "versions" else "second",
            request="second",
            expected=1 if kind == "versions" else None,
        )
        with pytest.raises(ContractError) as denied:
            await w.files.write(admission, second, payload)
        assert denied.value.error.code is ErrorCode.RESOURCE_BUSY

    run(tmp_path, scenario, limits=limits)


def test_expired_source_does_not_reappear_from_old_version(tmp_path):
    async def scenario(w):
        source = await w.source()
        admission = await w.admit([source])
        change, data = w.change()
        await w.files.write(admission, change, data)
        w.now += timedelta(days=31)
        with pytest.raises(ContractError):
            await w.files.read(admission, change.file_id, 1)
        assert await w.files.search(AREA, "lighthouse") == []

    run(tmp_path, scenario)


def test_concurrent_cas_and_reopened_owner_keep_one_revision(tmp_path):
    async def scenario(w):
        admission = await w.admit()
        change, data = w.change()
        await w.files.write(admission, change, data)
        left, left_data = w.change(b"Left revision.", request="left", expected=1)
        right, right_data = w.change(b"Right revision.", request="right", expected=1)
        outcomes = await asyncio.gather(
            w.files.write(admission, left, left_data),
            w.files.write(admission, right, right_data),
            return_exceptions=True,
        )
        assert sum(isinstance(item, ManagedFile) for item in outcomes) == 1
        failures = [item for item in outcomes if isinstance(item, ContractError)]
        assert len(failures) == 1 and failures[0].error.code is ErrorCode.VERSION_CONFLICT
        reopened = ManagedFiles(
            w.store,
            CAT,
            w.blobs,
            w.guard,
            w,
            w.files.usage,
            w.files.limits,
            authorize=w.authorize,
            clock=w.clock,
        )
        latest, content = await reopened.read(admission, change.file_id)
        assert latest.version == 2 and content in {left_data, right_data}
        assert (await reopened.read(admission, change.file_id, 1))[1] == data

    run(tmp_path, scenario)


def test_same_logical_name_cannot_overwrite_another_file(tmp_path):
    async def scenario(w):
        admission = await w.admit()
        change, data = w.change()
        first = await w.files.write(admission, change, data)
        collision, other = w.change(b"Other.", file_id="other", request="collision")
        collision = collision.model_copy(update={"path": change.path})
        with pytest.raises(ContractError):
            await w.files.write(admission, collision, other)
        assert (await w.files.read(admission, first.id))[1] == data
        assert await w.store.repository(ManagedFile, CAT, AREA).get("other") is None

    run(tmp_path, scenario)
