"""Dispose M3 test engines before each short asyncio.run loop closes.

These fixtures return service worlds for later assertions in a new loop. Disposing
the pool preserves the database and lets the next loop open a fresh connection.
"""

import asyncio
from functools import wraps

import pytest


@pytest.fixture(autouse=True)
def close_m3_test_pools(request, monkeypatch):
    owned = {
        "extraction",
        "gamemaster",
        "development",
        "laboratory",
        "m3_review",
        "m3_runtime",
        "integration_dialogue",
        "moderation",
        "dialogue",
    }
    if request.node.path.parent.name not in owned:
        yield
        return
    from ai_agent_adapters.storage.database import SQLiteStore

    stores = []
    original_init = SQLiteStore.__init__
    original_run = asyncio.run

    @wraps(original_init)
    def track(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        stores.append(self)

    @wraps(original_run)
    def run_with_cleanup(awaitable, **kwargs):
        async def checked():
            try:
                return await awaitable
            finally:
                for store in stores:
                    await store.close()

        return original_run(checked(), **kwargs)

    monkeypatch.setattr(SQLiteStore, "__init__", track)
    monkeypatch.setattr(asyncio, "run", run_with_cleanup)
    yield
