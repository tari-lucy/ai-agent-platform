"""Real HTTP/SQLite keyset paging: ties, inserts, scope binding and current privacy."""

import asyncio
from dataclasses import replace
from datetime import timedelta

from access_support import Harness
from ai_agent_application.storage import Grant, Owner, Principal, RecordAccess
from operations_support import BASE
from test_storage import MEMORY, new_store, save, source

from ai_agent_core.access import AccessPorts
from ai_agent_core.access.reads import SQLiteReader


def test_chronological_pages_survive_new_inserts_and_check_rights(tmp_path):
    async def scenario():
        grants = (
            Grant(MEMORY, Owner.MEMORY, read=True, write=True),
            Grant(Principal("operator", "operator"), Owner.MEMORY, read=True, api_transfer=True),
        )
        store = await new_store(tmp_path, grants=grants, scope=BASE)
        access = (RecordAccess("operator", api_transfer=True),)
        template = source("template", scope=BASE)
        for identifier, seconds in [("z-old", -2), ("a-tie", 0), ("z-tie", 0), ("hidden", 1)]:
            item = source(
                identifier,
                scope=BASE,
                occurred_at=(template.created_at + timedelta(seconds=seconds)).isoformat(),
            )
            await save(
                store, item, request_id=identifier, access=access if identifier != "hidden" else ()
            )
        h = await Harness().setup(ports=AccessPorts(reader=SQLiteReader(store)))
        await h.login()

        async def read(**params):
            return await h.client.get(
                "/api/v1/sources", params=h.scope_query(order="desc", limit=1, **params)
            )

        first = await read()
        assert first.status_code == 200, first.text
        assert [v["id"] for v in first.json()["items"]] == ["z-tie"]
        cursor = first.json()["next_cursor"]
        await save(
            store,
            source(
                "new",
                scope=BASE,
                occurred_at=(template.created_at + timedelta(seconds=5)).isoformat(),
            ),
            request_id="new",
            access=access,
        )
        second = await read(cursor=cursor)
        assert second.status_code == 200, second.text
        assert [v["id"] for v in second.json()["items"]] == ["a-tie"]
        third = await read(cursor=second.json()["next_cursor"])
        assert [v["id"] for v in third.json()["items"]] == ["z-old"]
        assert [v["id"] for v in (await read()).json()["items"]] == ["new"]
        # Subsecond times still sort correctly across the whole-second boundary.
        for identifier, micros in [("fraction-low", 1), ("fraction-high", 999999)]:
            await save(
                store,
                source(
                    identifier,
                    scope=BASE,
                    occurred_at=(template.created_at + timedelta(microseconds=micros)).isoformat(),
                ),
                request_id=identifier,
                access=access,
            )
        fraction = await h.client.get(
            "/api/v1/sources", params=h.scope_query(order="desc", limit=10)
        )
        assert [v["id"] for v in fraction.json()["items"]][:3] == [
            "new",
            "fraction-high",
            "fraction-low",
        ]
        # Cursor cannot be reused for another resource/filter or a malformed date.
        other = await h.client.get(
            "/api/v1/utterances", params=h.scope_query(order="desc", cursor=cursor)
        )
        assert other.status_code == 400
        assert (await read(cursor="t1.invalid")).status_code == 400
        # Permission checked again on the next page, rather than baked into a cursor.
        await store.provision(BASE, grants=(grants[0], replace(grants[1], api_transfer=False)))
        revoked = await read(cursor=cursor)
        assert revoked.status_code == 200 and revoked.json()["items"] == []
        await h.close()
        await store.close()

    asyncio.run(scenario())


def test_reject_unsupported_sort_and_filter():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        for path, params in [
            ("sources", {"order": "asc"}),
            ("claims", {"order": "desc"}),
            ("sources", {"order": "desc", "utterance_id": "x"}),
            ("deliveries", {"utterance_id": "x"}),
        ]:
            response = await h.client.get("/api/v1/" + path, params=h.scope_query(**params))
            assert response.status_code == 400
        await h.close()

    asyncio.run(scenario())


def test_delivery_filter_is_applied_before_paging_and_keeps_acl(tmp_path):
    from ai_agent_contracts.control import Delivery
    from ai_agent_contracts.states import DeliveryState
    from test_storage import NOW

    async def scenario():
        channel = Principal("channel", "core_owner", Owner.CHANNEL)
        operator = Principal("operator", "operator")
        store = await new_store(
            tmp_path,
            scope=BASE,
            grants=(
                Grant(channel, Owner.CHANNEL, read=True, write=True),
                Grant(operator, Owner.CHANNEL, read=True, api_transfer=True),
            ),
        )
        for index in range(55):
            row = Delivery(
                id=f"d-{index:03}",
                scope=BASE,
                created_at=NOW,
                updated_at=NOW,
                version=1,
                utterance_id="target" if index in (0, 1, 54) else "other",
                revision=1,
                channel_id="operator-panel",
                attempt_id=f"attempt-{index}",
                status=DeliveryState.DELIVERED,
            )
            await save(
                store,
                row,
                principal=channel,
                request_id=row.id,
                access=(RecordAccess("operator" if index != 54 else "foreign", api_transfer=True),),
            )
        h = await Harness().setup(ports=AccessPorts(reader=SQLiteReader(store)))
        await h.login()
        params = h.scope_query(order="desc", utterance_id="target", limit=1)
        first = await h.client.get("/api/v1/deliveries", params=params)
        assert first.status_code == 200, first.text
        assert [r["id"] for r in first.json()["items"]] == ["d-001"]
        params["cursor"] = first.json()["next_cursor"]
        second = await h.client.get("/api/v1/deliveries", params=params)
        assert [r["id"] for r in second.json()["items"]] == ["d-000"]
        params["utterance_id"] = "other"
        assert (await h.client.get("/api/v1/deliveries", params=params)).status_code == 400
        await h.close()
        await store.close()

    asyncio.run(scenario())
