"""Trusted upload ownership, replay and revocation; real owner, no HTTP/model."""

import pytest
from ai_agent_contracts.agent_workplace import ManagedFile
from ai_agent_contracts.codec import ContractError
from wf_support import AREA, CAT, run


def test_upload_read_replay_new_version_and_guard(tmp_path, monkeypatch):
    async def scenario(w):
        active = True

        def authorize():
            if not active:
                from ai_agent_contracts.states import ErrorCode

                raise ContractError(ErrorCode.ACCESS_DENIED)

        async def write(key, body):
            return await w.files.operator_write(
                AREA,
                "knowledge",
                "sample.txt",
                "Sample",
                "text/plain",
                body,
                file_id="uploaded",
                request_id=key,
                authorize=authorize,
            )

        one = await write("one", b"A")
        assert one.access.api_transfer and not one.access.public_disclosure
        assert (await write("same-content", b"A")) == one
        assert (await write("two", b"B")).version == 2
        three = await write("three", b"A")
        assert three.version == 3
        assert (await w.files.released_content(AREA, three.id, "api_transfer"))[1] == b"A"
        assert await write("one", b"A") == one  # exact request replay is still the original receipt
        with pytest.raises(ContractError):
            await write("one", b"different")
        assert [row.id for row in await w.files.operator_materials(AREA)] == [three.id]
        with pytest.raises(ContractError):
            await w.files.operator_write(
                AREA,
                "knowledge",
                "sample.txt",
                "Collision",
                "text/plain",
                b"different file",
                file_id="other-file",
                request_id="collision",
                authorize=authorize,
            )
        original = w.blobs.put

        async def logout(*args):
            nonlocal active
            await original(*args)
            active = False

        monkeypatch.setattr(w.blobs, "put", logout)
        with pytest.raises(ContractError):
            await write("revoked-before-commit", b"never published")
        active = True
        current = await w.store.repository(ManagedFile, CAT, AREA).get(three.id)
        assert current.version == 3

    run(tmp_path, scenario)
