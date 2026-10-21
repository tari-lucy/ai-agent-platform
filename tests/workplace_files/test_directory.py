"""Physical writes use a new private store, no agent supplied directory paths."""

import asyncio
import hashlib

import pytest
from ai_agent_adapters.workspace_files.directory import DirectoryBlobs
from ai_agent_adapters.workspace_files.safe_read import UnsafeBlobError
from ai_agent_contracts.common import Scope


def test_explicit_prepare_immutable_blob_quota_and_partition(tmp_path):
    root = tmp_path / "private-store"
    blobs = DirectoryBlobs.prepare(root)
    with pytest.raises(FileExistsError):
        DirectoryBlobs.prepare(root)
    scope = Scope(
        workspace_id="fiction", persona_id="sample", memory_space_id="work", memory_mode="work"
    )
    content = b"Synthetic private note."
    digest = hashlib.sha256(content).hexdigest()

    async def scenario():
        await blobs.put(scope, digest, content, len(content))
        await blobs.put(scope, digest, content, len(content))
        assert await blobs.used_bytes(scope) == len(content)
        assert await blobs.read(scope, digest, len(content)) == content
        other = b"Another note."
        with pytest.raises(UnsafeBlobError, match="BLOB_QUOTA"):
            await blobs.put(scope, hashlib.sha256(other).hexdigest(), other, len(content))
        with pytest.raises(OSError):
            await blobs.read(scope.model_copy(update={"memory_mode": "test"}), digest, 100)
        # A corrupted blob is refused, never silently overwritten by a retry.
        leaf = next(root.iterdir()) / digest
        leaf.write_bytes(b"Broken")
        with pytest.raises(UnsafeBlobError, match="BLOB_INTEGRITY_FAILED"):
            await blobs.put(scope, digest, content, 100)
        assert leaf.read_bytes() == b"Broken"

    asyncio.run(scenario())
