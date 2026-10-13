"""Real SQLite record grants, API-transfer and TTL are enforced on HTTP reads."""

import asyncio
from dataclasses import replace

from access_support import Harness
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from operations_support import BASE
from test_storage import MEMORY, new_store, save, source

from ai_agent_core.access import AccessPorts
from ai_agent_core.access.reads import SQLiteReader


def test_sqlite_read_uses_api_transfer_record_acl_and_current_ttl(tmp_path):
    async def scenario():
        operator = Principal("operator", "operator")
        grants = (
            Grant(MEMORY, Owner.MEMORY, read=True, write=True),
            Grant(operator, Owner.MEMORY, read=True, api_transfer=True),
        )
        store = await new_store(tmp_path, grants=grants, scope=BASE)
        accepted = source("permitted", scope=BASE)
        denied = source("denied", scope=BASE)
        private = source("private", scope=BASE, api_transfer_allowed=False)
        expired = source(
            "expired",
            scope=BASE,
            retention={
                "policy_id": "synthetic-policy",
                "expires_at": accepted.created_at.isoformat(),
            },
        )
        for item in (accepted, denied, private, expired):
            await save(
                store,
                item,
                request_id="save-" + item.id,
                access=(
                    RecordAccess(
                        "operator" if item.id != "denied" else "foreign", api_transfer=True
                    ),
                ),
            )
        h = await Harness().setup(ports=AccessPorts(reader=SQLiteReader(store)))
        await h.login()
        response = await h.client.get("/api/v1/sources", params=h.scope_query())
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["items"]] == ["permitted"]
        for identifier in ("denied", "private", "expired", "absent"):
            response = await h.client.get("/api/v1/sources/" + identifier, params=h.scope_query())
            assert response.status_code == 404
            assert identifier not in response.text
        await store.provision(BASE, grants=(grants[0], replace(grants[1], api_transfer=False)))
        response = await h.client.get("/api/v1/sources/permitted", params=h.scope_query())
        assert response.status_code == 404
        await h.close()
        await store.close()

    asyncio.run(scenario())


def test_reader_cannot_return_foreign_partition_projection():
    async def scenario():
        class Wrong:
            async def read(self, *args):
                return source(
                    "foreign-row", scope=BASE.model_copy(update={"persona_id": "foreign"})
                )

        h = await Harness().setup(ports=AccessPorts(reader=Wrong()))
        await h.login()
        response = await h.client.get("/api/v1/sources/foreign-row", params=h.scope_query())
        assert response.status_code == 403
        assert "foreign-row" not in response.text
        await h.close()

    asyncio.run(scenario())
