"""The opt-in public HTTPS name of a reverse proxy: exact Host/Origin, Secure cookie, no workers."""

import asyncio

import httpx
import pytest
from access_support import PASSWORD, Harness
from ai_agent_adapters.configuration import BootstrapSettings, ConfigurationError, public_origin

PUBLIC = "https://agents.tarigpt.ru"


def public_client(harness, base=PUBLIC):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=harness.app), base_url=base)


async def login(client, origin):
    return await client.post(
        "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": origin}
    )


def test_without_the_setting_a_foreign_host_is_refused_as_before():
    async def scenario():
        harness = await Harness().setup()
        client = public_client(harness)
        assert (await client.get("/api/v1/auth/session")).status_code == 403
        assert (await login(client, PUBLIC)).status_code == 403

    asyncio.run(scenario())


def test_the_public_name_logs_in_with_a_secure_cookie_and_the_loopback_one_still_works():
    async def scenario():
        harness = await Harness().setup(public_origin=PUBLIC)
        client = public_client(harness)
        reply = await login(client, PUBLIC)
        assert reply.status_code == 200, reply.text
        cookie = reply.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        csrf = reply.json()["csrf_token"]
        assert (await client.get("/api/v1/auth/session")).status_code == 200
        out = await client.post(
            "/api/v1/auth/logout", headers={"Origin": PUBLIC, "X-CSRF-Token": csrf}
        )
        assert out.status_code == 204 and "secure" in out.headers["set-cookie"].lower()

        local = await harness.client.post(
            "/api/v1/auth/login",
            json={"password": PASSWORD},
            headers={"Origin": harness.origin},
        )
        assert local.status_code == 200
        assert "secure" not in local.headers["set-cookie"].lower()  # plain http on loopback

    asyncio.run(scenario())


def test_host_and_origin_must_match_each_other_exactly():
    async def scenario():
        harness = await Harness().setup(public_origin=PUBLIC)
        client = public_client(harness)
        refused = [
            # public Host with the loopback Origin, and the other way round
            (PUBLIC, {"Origin": harness.origin}),
            (harness.origin, {"Origin": PUBLIC}),
            # another name, a port, a sibling, a look-alike
            ("https://evil.tarigpt.ru", {"Origin": "https://evil.tarigpt.ru"}),
            ("https://agents.tarigpt.ru:8443", {"Origin": "https://agents.tarigpt.ru:8443"}),
            (
                "https://agents.tarigpt.ru.evil.com",
                {"Origin": "https://agents.tarigpt.ru.evil.com"},
            ),
        ]
        for base, headers in refused:
            other = public_client(harness, base)
            reply = await other.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers=headers
            )
            assert reply.status_code == 403, (base, headers)
        # Proxy headers never widen the allow-list.
        spoofed = await harness.client.get(
            "/api/v1/auth/session", headers={"X-Forwarded-Host": "agents.tarigpt.ru"}
        )
        assert spoofed.status_code == 401  # the loopback rules: just not logged in
        cross = await client.get("/api/v1/auth/session", headers={"Sec-Fetch-Site": "cross-site"})
        assert cross.status_code == 403

    asyncio.run(scenario())


def test_workers_never_come_in_by_the_public_name():
    async def scenario():
        harness = await Harness().setup(public_origin=PUBLIC)
        path = "/internal/v1/jobs/x/claim"
        public = await public_client(harness).post(path, json={})
        assert public.status_code == 404
        local = await harness.client.post(path, json={})
        assert local.status_code == 401  # reaches the service check: no bearer token

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "value",
    [
        "http://agents.tarigpt.ru",
        "https://agents.tarigpt.ru:8443",
        "https://agents.tarigpt.ru/",
        "https://agents.tarigpt.ru/panel",
        "https://user@agents.tarigpt.ru",
        "https://Agents.tarigpt.ru",
        "https://127.0.0.1",
        "https://localhost",
        "https://panel.local",
        "https://agents",
        "agents.tarigpt.ru",
        "https://",
    ],
)
def test_only_an_exact_https_dns_origin_is_accepted(value):
    with pytest.raises(ConfigurationError):
        public_origin(BootstrapSettings(public_origin=value))


def test_a_valid_name_and_none_are_accepted():
    assert public_origin(BootstrapSettings(public_origin=PUBLIC)) == PUBLIC
    assert public_origin(BootstrapSettings()) is None
