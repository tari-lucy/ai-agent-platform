"""Scope/alias/event isolation and deny coverage of the exact native surface inventory."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from access_support import Harness
from ai_agent_contracts.api import PanelEvent
from ai_agent_contracts.common import Scope

from ai_agent_core.opencode_ui.protocol import PREFIX, directory, invalidation


def test_two_personalities_and_run_identity_do_not_alias():
    first = Scope(
        workspace_id="workspace",
        persona_id="first-person",
        memory_space_id="private",
        memory_mode="work",
    )
    second = first.model_copy(update={"persona_id": "second-person"})
    test = first.model_copy(update={"memory_mode": "test"})
    run = first.model_copy(update={"agent_id": "agent", "run_id": "run"})
    assert len({directory(first), directory(second), directory(test)}) == 3
    assert directory(run) == directory(first)
    now = datetime.now(UTC)
    event = PanelEvent(
        id="event",
        event_id="event",
        scope=first,
        created_at=now,
        occurred_at=now,
        type="work-session.changed",
        resource_id="session",
        resource_version=2,
        causal_id=None,
        operation_id=None,
    )
    assert invalidation(event, second) is None
    notice = invalidation(event, first)
    assert notice is not None and notice.resource_id == "session"
    assert notice.type == "refetch" and "text" not in notice.model_dump()


def test_native_inventory_cannot_be_used_as_passthrough():
    async def scenario():
        h = await Harness().setup()
        try:
            await h.login()
            inventory = json.loads(
                (
                    Path(__file__).resolve().parents[2]
                    / "docs/protocols/opencode-1.19.3-routes.json"
                ).read_text()
            )
            assert len(inventory["routes"]) == 162
            for route in inventory["routes"]:
                assert route["bridge_policy"] == "deny_native_passthrough"
                # Actual native methods, including /api and experimental aliases.
                for method in route["methods"]:
                    if method == "parameters":
                        continue
                    response = await h.client.request(
                        method.upper(),
                        PREFIX + route["path"],
                        headers={"Origin": h.origin, "X-CSRF-Token": h.csrf},
                    )
                    assert response.status_code == 404, (method, route["path"], response.text)
        finally:
            await h.close()

    asyncio.run(scenario())
