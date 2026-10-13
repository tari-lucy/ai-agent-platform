"""Real password verifier/cookies/ASGI; synthetic identities, no external calls."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from access_support import ORIGIN, PASSWORD, WORKER_TOKEN, Harness, configured_hash
from ai_agent_adapters.configuration import (
    BootstrapSettings,
    ConfigurationError,
    load_configuration,
)
from ai_agent_contracts.states import RunState
from operations_support import BASE

from ai_agent_core.access import AccessLimits, AccessPorts
from ai_agent_core.access.sessions import COOKIE
from ai_agent_core.bootstrap import create_app


def run(test):
    return asyncio.run(test())


def test_login_cookie_safe_projection_and_csrf_recovery():
    async def scenario():
        h = await Harness().setup()
        login = await h.login()
        cookie = login.headers["set-cookie"]
        assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/" in cookie
        assert "Secure" not in cookie
        assert h.cookie not in login.text and PASSWORD not in login.text
        assert WORKER_TOKEN not in login.text
        assert configured_hash().get_secret_value() not in login.text
        assert login.headers["cache-control"] == "no-store"
        session = await h.client.get("/api/v1/auth/session")
        assert session.json()["csrf_token"] == h.csrf
        read = await h.client.get("/api/v1/sessions", params=h.scope_query())
        assert read.status_code == 200
        assert read.json()["items"][0]["state"] == "ACTIVE"
        assert h.cookie not in read.text
        await h.close()

    run(scenario)


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.invalid"},
        {"Origin": "null"},
        {"Origin": ""},
        {"Origin": ORIGIN + "/"},
        {"Host": "evil.invalid"},
        {"Host": "localhost:8765"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
        {"Authorization": "Bearer " + WORKER_TOKEN},
    ],
)
def test_browser_origin_host_purpose_checks(headers):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        epoch = h.supervisor.snapshot.control_epoch
        response = await h.post(
            "/api/v1/agents/synthetic-agent/stop", data=h.stop(), headers=headers
        )
        assert response.status_code == 403
        assert h.supervisor.snapshot.control_epoch == epoch
        assert "evil.invalid" not in response.text and WORKER_TOKEN not in response.text
        await h.close()

    run(scenario)


@pytest.mark.parametrize(
    "headers",
    [
        {"X-CSRF-Token": ""},
        {"X-CSRF-Token": "invalid"},
        {"Origin": ORIGIN},
    ],
)
def test_missing_or_wrong_csrf_blocks_mutation(headers):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        bindings = {"Origin": ORIGIN, **headers}
        response = await h.client.post(
            "/api/v1/agents/synthetic-agent/stop", json=h.stop(), headers=bindings
        )
        assert response.status_code == 403
        assert h.supervisor.snapshot.state == RunState.ACTIVE
        await h.close()

    run(scenario)


def test_unauthenticated_reads_commands_and_cookies_are_denied():
    async def scenario():
        h = await Harness().setup()
        for path in ("/api/v1/sessions", "/api/v1/people", "/api/v1/people/secret-person"):
            response = await h.client.get(path, params=h.scope_query())
            assert response.status_code == 401
        for path in ("/api/v1/commands", "/api/v1/agents/synthetic-agent/stop"):
            response = await h.client.post(path, json=h.stop(), headers={"Origin": ORIGIN})
            assert response.status_code == 401
        for value in ("x" * 43, "malformed", "x" * 1000):
            response = await h.client.get(
                "/api/v1/auth/session", headers={"Cookie": COOKIE + "=" + value}
            )
            assert response.status_code == 401
        await h.close()

    run(scenario)


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "foreign"},
        {"persona_id": "foreign"},
        {"memory_space_id": "foreign"},
        {"memory_mode": "work"},
    ],
)
def test_foreign_read_command_and_work_test_scope_isolation(change):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        other = BASE.model_copy(update=change)
        for path in ("/api/v1/sessions", "/api/v1/people", "/api/v1/people/foreign"):
            response = await h.client.get(path, params=h.scope_query(other))
            assert response.status_code == 403
        epoch = h.supervisor.snapshot.control_epoch
        scope = h.supervisor.snapshot.scope.model_copy(update=change)
        response = await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop(scope))
        assert response.status_code == 403
        assert h.supervisor.snapshot.control_epoch == epoch
        await h.close()

    run(scenario)


def test_logout_revokes_old_cookie_and_restart_requires_login():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        old_cookie = h.cookie
        response = await h.post("/api/v1/auth/logout")
        assert response.status_code == 204 and "Max-Age=0" in response.headers["set-cookie"]
        response = await h.client.get(
            "/api/v1/auth/session", headers={"Cookie": COOKIE + "=" + old_cookie}
        )
        assert response.status_code == 401
        assert h.supervisor.snapshot.state == RunState.ACTIVE  # logout grants no control effect
        await h.login()
        assert h.cookie != old_cookie
        restarted = await Harness().setup()
        restarted.client.cookies.set(COOKIE, h.cookie, domain="127.0.0.1", path="/")
        assert (await restarted.client.get("/api/v1/auth/session")).status_code == 401
        await restarted.login()  # stale cookie must not make reauthentication impossible
        assert restarted.cookie != h.cookie
        await restarted.close()
        await h.close()

    run(scenario)


@pytest.mark.parametrize("advance", [6, 21, -1])
def test_idle_absolute_expiry_and_clock_rollback(advance):
    async def scenario():
        h = await Harness().setup(limits=AccessLimits(session_seconds=20, idle_seconds=5))
        await h.login()
        h.time.advance(advance)
        assert (await h.client.get("/api/v1/auth/session")).status_code == 401
        await h.close()

    run(scenario)


def test_https_cookie_is_secure():
    async def scenario():
        h = await Harness().setup(origin="https://localhost:8765")
        response = await h.login()
        assert "Secure" in response.headers["set-cookie"]
        await h.close()

    run(scenario)


def test_attempt_and_session_tables_are_bounded():
    async def scenario():
        h = await Harness().setup(limits=AccessLimits(sessions=2, login_attempts=3))
        for index in range(3):
            response = await h.client.post(
                "/api/v1/auth/login",
                json={"password": "synthetic-wrong-password"},
                headers={"Origin": ORIGIN},
            )
            assert response.status_code == 403, index
        assert (
            await h.client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
        ).status_code == 429
        h.time.advance(60)
        await h.login()
        await h.login()
        assert (
            await h.client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
        ).status_code == 429
        assert len(h.surface.sessions._sessions) == 2
        await h.close()

    run(scenario)


def test_cancelled_login_cannot_spawn_unbounded_password_workers():
    async def scenario():
        h = await Harness().setup()
        entered, release = threading.Event(), threading.Event()

        def blocked(password):
            entered.set()
            release.wait(5)
            return False

        h.surface.sessions._verify_password = blocked
        task = asyncio.create_task(
            h.client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        next_login = await h.client.post(
            "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
        )
        assert next_login.status_code == 429
        release.set()
        await h.surface.sessions._login_task
        await h.close()

    run(scenario)


def test_direct_stop_works_during_password_verification():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        entered, release = threading.Event(), threading.Event()

        def blocked(password):
            entered.set()
            release.wait(5)
            return False

        h.surface.sessions._verify_password = blocked
        login = asyncio.create_task(
            h.client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
        )
        while not entered.is_set():
            await asyncio.sleep(0.01)
        started = time.perf_counter()
        stop = await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop())
        assert stop.status_code == 200 and stop.json()["state"] == "STOPPED"
        assert time.perf_counter() - started < 0.25
        release.set()
        assert (await login).status_code == 403
        await h.close()

    run(scenario)


def test_blocked_database_queue_does_not_block_stop_and_logout_revokes_awaited_reads():
    async def scenario():
        class Blocked:
            def __init__(self):
                self.entered, self.release = asyncio.Event(), asyncio.Event()
                self.calls = 0

            async def read(self, *args):
                self.calls += 1
                self.entered.set()
                await self.release.wait()
                return None

        reader = Blocked()
        h = await Harness().setup(ports=AccessPorts(reader=reader, limits=AccessLimits(requests=2)))
        await h.login()
        tasks = [
            asyncio.create_task(h.client.get("/api/v1/people", params=h.scope_query()))
            for _ in range(2)
        ]
        while reader.calls != 2:
            await asyncio.sleep(0.01)
        assert (await h.client.get("/api/v1/people", params=h.scope_query())).status_code == 429
        started = time.perf_counter()
        stop = await h.post("/api/v1/agents/synthetic-agent/stop", data=h.stop())
        assert stop.status_code == 200 and time.perf_counter() - started < 0.25
        # Revoke in RAM while reads are suspended (trusted logout equivalent).
        authority = h.surface.sessions.authorize(h.cookie)
        h.surface.sessions.logout(authority)
        reader.release.set()
        assert all(r.status_code == 403 for r in await asyncio.gather(*tasks))
        assert h.surface._requests == 0
        await h.close()

    run(scenario)


def test_safe_errors_do_not_echo_credentials_or_provider_exceptions(caplog):
    async def scenario():
        class Broken:
            async def read(self, *args):
                raise RuntimeError(PASSWORD + WORKER_TOKEN + configured_hash().get_secret_value())

        h = await Harness().setup(ports=AccessPorts(reader=Broken()))
        await h.login()
        response = await h.client.get("/api/v1/people", params=h.scope_query())
        assert response.status_code == 503
        payload = json.dumps({"password": PASSWORD, "service_token": WORKER_TOKEN})
        malformed = await h.client.post(
            "/api/v1/commands",
            content=payload,
            headers={"Origin": ORIGIN, "X-CSRF-Token": h.csrf, "Content-Type": "application/json"},
        )
        assert malformed.status_code == 400
        for canary in (PASSWORD, WORKER_TOKEN, configured_hash().get_secret_value(), h.cookie):
            assert canary not in response.text + malformed.text + caplog.text
        await h.close()

    run(scenario)


@pytest.mark.parametrize(
    "extra",
    [
        {"limit": "201"},
        {"limit": "0"},
        {"cursor": "x" * 129},
        {"token": WORKER_TOKEN},
    ],
)
def test_query_limits_and_url_credentials_rejected(extra):
    async def scenario():
        h = await Harness().setup()
        await h.login()
        response = await h.client.get("/api/v1/sessions", params=h.scope_query(**extra))
        assert response.status_code == 400
        assert WORKER_TOKEN not in response.text
        await h.close()

    run(scenario)


def test_duplicate_headers_cookie_and_json_fail_closed():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        for name, value in (
            ("Origin", ORIGIN),
            ("Host", "127.0.0.1:8765"),
            ("X-CSRF-Token", h.csrf),
        ):
            response = await h.client.post(
                "/api/v1/agents/synthetic-agent/stop",
                json=h.stop(),
                headers=[
                    ("Host", "127.0.0.1:8765"),
                    ("Origin", ORIGIN),
                    ("X-CSRF-Token", h.csrf),
                    (name, value),
                ],
            )
            assert response.status_code == 403
        response = await h.client.get(
            "/api/v1/auth/session",
            headers={"Cookie": COOKIE + "=" + h.cookie + "; " + COOKIE + "=" + h.cookie},
        )
        assert response.status_code == 401
        raw = json.dumps(h.stop())
        raw = '{"type":"agent.stop",' + raw[1:]
        response = await h.client.post(
            "/api/v1/commands",
            content=raw,
            headers={"Origin": ORIGIN, "X-CSRF-Token": h.csrf, "Content-Type": "application/json"},
        )
        assert response.status_code == 400
        await h.close()

    run(scenario)


def test_body_content_length_chunking_and_foreign_path_target():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        response = await h.post("/api/v1/agents/foreign/stop", data=h.stop())
        assert response.status_code == 403
        headers = {"Origin": ORIGIN, "X-CSRF-Token": h.csrf, "Content-Type": "application/json"}

        async def chunks():
            for _ in range(5):
                yield b"x" * 1024

        response = await h.client.post(
            "/api/v1/agents/synthetic-agent/stop", content=chunks(), headers=headers
        )
        assert response.status_code == 413
        response = await h.client.post(
            "/api/v1/commands", content=b"x" * (1024 * 1024 + 1), headers=headers
        )
        assert response.status_code == 413
        response = await h.post(
            "/api/v1/commands", data=h.stop(), headers={"Content-Encoding": "gzip"}
        )
        assert response.status_code == 400
        assert h.supervisor.snapshot.state == RunState.ACTIVE
        await h.close()

    run(scenario)


def test_disabled_auth_closed_bootstrap_and_safe_configuration():
    async def scenario():
        settings = BootstrapSettings(memory_scope="test")
        app = create_app(settings)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        ) as client:
            response = await client.post(
                "/api/v1/auth/login", json={"password": PASSWORD}, headers={"Origin": ORIGIN}
            )
            assert response.status_code == 503
            assert (await client.get("/api/v1/auth/session")).status_code == 401
        assert app.state.supervisor.snapshot.state == RunState.STOPPED
        assert app.state.bootstrap_manifest["effects_enabled"] is False
        assert not hasattr(app.state, "settings")
        for key, value in (
            ("AI_PLATFORM_OPERATOR_PASSWORD_HASH", PASSWORD),
            ("AI_PLATFORM_WORKER_SERVICE_TOKEN", PASSWORD),
            ("AI_PLATFORM_OPERATOR_ORIGIN", "http://evil.invalid:8765"),
        ):
            with pytest.raises(ConfigurationError) as error:
                load_configuration(environ={key: value})
            assert value not in str(error.value)
        configured = load_configuration(
            environ={
                "AI_PLATFORM_OPERATOR_PASSWORD_HASH": configured_hash().get_secret_value(),
                "AI_PLATFORM_WORKER_SERVICE_TOKEN": WORKER_TOKEN,
            }
        )
        assert PASSWORD not in repr(configured) and WORKER_TOKEN not in repr(configured)
        assert WORKER_TOKEN not in json.dumps(app.state.bootstrap_manifest)

    run(scenario)


def test_closed_owner_commands_do_not_start_unaccepted_m2_paths():
    async def scenario():
        h = await Harness().setup()
        await h.login()
        response = await h.post(
            "/api/v1/commands",
            data={
                "request_id": "message",
                "type": "dialogue.message",
                "target": "synthetic-agent",
                "scope": h.stop()["scope"],
                "arguments": {"text": "synthetic"},
            },
        )
        assert response.status_code == 503
        response = await h.client.get("/api/v1/people", params=h.scope_query())
        assert response.status_code == 503
        await h.close()

    run(scenario)
