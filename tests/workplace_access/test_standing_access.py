"""D78: rights are granted until revoked by default; a term is the operator's explicit choice.

Synthetic host and model; the ordinary preview/execute of the initial access set.
"""

import asyncio

from ai_agent_contracts.workplace_access import STANDING_UNTIL
from test_activity import WorkModel, prepared
from test_http import post, scope_params
from test_initial_access import execute_command, preview_command, rights

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.workplace_files import enable_workplace_files


async def host_with(tmp_path, monkeypatch, ceiling_days):
    host = await prepared(tmp_path, monkeypatch, WorkModel())
    await enable_recovery(
        host.root, store_dir=tmp_path / "backups", ledger_path=tmp_path / "ledger/e.sqlite"
    )
    await enable_workplace_files(host.root, ceiling_days=ceiling_days)
    return host


def test_without_a_term_the_set_is_granted_until_revoked(tmp_path, monkeypatch):
    async def scenario():
        host = await host_with(tmp_path, monkeypatch, 0)  # 0: no term limit on this host
        async with host.open():
            rt, scope, env = host.runtime, host.scope, host.env
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            assert status["standing"] is True
            planned = await post(env, preview_command(host, "standing", None))
            assert planned.status_code == 200, planned.text
            done = await post(
                env, execute_command(host, "standing-go", planned.json()["resource_id"])
            )
            assert done.status_code == 200, done.text
            granted = await rights(rt, scope)
            assert granted and all(p.expires_at == STANDING_UNTIL for p in granted.values())
            # An explicit term is still the operator's choice on the same host.
            planned = await post(env, preview_command(host, "term", 3 * 86400))
            assert planned.status_code == 200, planned.text

    asyncio.run(scenario())


def test_a_host_with_a_term_limit_refuses_until_revoked(tmp_path, monkeypatch):
    async def scenario():
        host = await host_with(tmp_path, monkeypatch, 7)
        async with host.open():
            env, scope = host.env, host.scope
            status = (
                await env.client.get("/api/v1/workplace-access", params=scope_params(scope))
            ).json()
            assert status["standing"] is False and status["max_duration_seconds"] == 7 * 86400
            refused = await post(env, preview_command(host, "standing", None))
            assert refused.status_code == 403, refused.text
            assert (await post(env, preview_command(host, "week", 7 * 86400))).status_code == 200

    asyncio.run(scenario())
