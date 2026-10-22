"""Operator release uses the file owner and rechecks bases after blob IO."""

import pytest
from ai_agent_contracts.codec import ContractError
from ai_agent_contracts.states import ErrorCode
from wf_support import AREA, run


@pytest.mark.parametrize("api", [True, False])
def test_release_requires_transfer_right_not_only_private_read(tmp_path, api):
    async def scenario(w):
        source = await w.source(api=api)
        admission = await w.admit([source])
        change, data = w.change()
        entry = await w.files.write(admission, change, data)
        if api:
            assert await w.files.released_content(AREA, entry.id, "api_transfer") == (entry, data)
        else:
            with pytest.raises(ContractError) as denied:
                await w.files.released_content(AREA, entry.id, "api_transfer")
            assert denied.value.error.code is ErrorCode.ACCESS_DENIED
        assert (await w.files.released_content(AREA, entry.id, "private"))[1] == data

    run(tmp_path, scenario)


def test_release_rechecks_revocation_after_io(tmp_path, monkeypatch):
    async def scenario(w):
        source = await w.source()
        admission = await w.admit([source])
        change, data = w.change()
        entry = await w.files.write(admission, change, data)
        original = w.blobs.read

        async def revoke(*args):
            result = await original(*args)
            await w.ledger.block(AREA, source.source_id, 1, "rights_revoked", w.now)
            return result

        monkeypatch.setattr(w.blobs, "read", revoke)
        with pytest.raises(ContractError) as denied:
            await w.files.released_content(AREA, entry.id, "api_transfer")
        assert denied.value.error.code is ErrorCode.ACCESS_DENIED

    run(tmp_path, scenario)
