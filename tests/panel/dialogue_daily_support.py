"""Fictional long history for the dialogue browser scenario, written through the memory owner.

The host is initialised by the real `init` and not yet served. Records are registered with
occurred_at times that do NOT follow their (hash) ids, so a client that trusts id order or
arrival order shows the conversation scrambled.
"""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_agent_adapters.storage.database import SQLiteStore
from ai_agent_application.memory import MemoryService, SourceDraft
from ai_agent_application.storage import Owner
from ai_agent_contracts.control import Delivery
from ai_agent_contracts.states import DeliveryState, Provenance

from ai_agent_core.local_host import rights
from ai_agent_core.local_host.init import work_scope
from ai_agent_core.local_host.layout import load_config, paths
from ai_agent_core.m3_composition import new_registry
from ai_agent_core.m3_composition.state import PRINCIPALS, command, put


def label(rank: int) -> str:
    return f"Вымышленная реплика {rank:03}"


async def _seed(root: Path, count: int) -> list[str]:
    host = paths(root)
    config = load_config(host)
    scope = work_scope(config.workspace_id, config.persona_id, config.work_space_id)
    registry = new_registry()
    store = SQLiteStore(host.database, extensions=registry)
    try:
        memory = MemoryService(
            store, PRINCIPALS[Owner.MEMORY], registry, reader_access=rights.record_readers()
        )
        start = datetime.now(UTC) - timedelta(hours=6)
        order = []
        for index in range(count):
            # Registration order is scrambled against chronological rank.
            rank = (index * 97) % count
            await memory.register_source(
                command(scope, f"seed-{index}"),
                SourceDraft(
                    provenance=Provenance.OPERATOR_INPUT,
                    source_type="operator-message",
                    adapter_id="dialogue",
                    adapter_version="1",
                    occurred_at=start + timedelta(minutes=rank),
                    content=label(rank),
                    read_allowed=True,
                    api_transfer_allowed=True,
                    public_disclosure_allowed=False,
                    external_id=f"seed-{index}",
                ),
            )
            order.append(label(rank))
        # More than four delivery pages, unrelated to current replies. A new visible answer
        # must still be acknowledged via its exact lookup, without exhausting this history.
        for index in range(205):
            await put(
                store,
                registry,
                Owner.CHANNEL,
                scope,
                f"seed-delivery-{index}",
                Delivery(
                    id=f"old-delivery-{index}",
                    scope=scope,
                    version=1,
                    created_at=start,
                    updated_at=start,
                    utterance_id=f"old-reply-{index}",
                    revision=1,
                    channel_id="operator-panel",
                    attempt_id=f"old-attempt-{index}",
                    status=DeliveryState.DELIVERED,
                ),
                expected=None,
                access=rights.record_readers(),
            )
        return order
    finally:
        await store.close()


def seed_history(root: Path, count: int) -> list[str]:
    """Registers `count` operator messages; returns their texts in registration order."""
    # The Playwright sync API owns this thread's event loop: seed from a worker thread.
    result: list[list[str]] = []
    worker = threading.Thread(target=lambda: result.append(asyncio.run(_seed(root, count))))
    worker.start()
    worker.join()
    return result[0]
