"""Offline configuration and ASGI transport ownership; no external requests."""

import asyncio

import httpx
import pytest
from ai_agent_adapters.llm import ADAPTER_ID, OpenAICompatibleProvider
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE
from test_live import binding

from ai_agent_core.local_host import serve
from ai_agent_core.local_host.http_transport import configure_http_transport
from ai_agent_core.local_host.init import initialise
from ai_agent_core.local_host.layout import HostError, LiveConfig, load_config, paths
from ai_agent_core.local_host.process import exclusive_host


def test_offline_transport_preserves_everything_else(tmp_path):
    root = tmp_path / "host"
    asyncio.run(
        initialise(
            root, profile_markdown=REFERENCE_PROFILE, live=LiveConfig(model_id="fixture-model")
        )
    )
    host = paths(root)
    before = load_config(host)
    assert before.live.http_keepalive_seconds == 120
    legacy = before.live.model_dump(exclude={"http_keepalive_seconds"})
    assert LiveConfig.model_validate(legacy).http_keepalive_seconds == 120
    database = host.database.read_bytes()
    with exclusive_host(root), pytest.raises(HostError, match="HOST_ALREADY_RUNNING"):
        configure_http_transport(root, keepalive_seconds=30)
    for value in (0, 120, 3600):
        configure_http_transport(root, keepalive_seconds=value)
        updated = load_config(host)
        assert updated.live.http_keepalive_seconds == value
        restored = updated.live.model_copy(update={"http_keepalive_seconds": 120})
        assert updated.model_copy(update={"live": restored}) == before
    for value in (-1, 3601, True, 1.5):
        with pytest.raises(HostError, match="HTTP_KEEPALIVE_INVALID"):
            configure_http_transport(root, keepalive_seconds=value)
    assert host.database.read_bytes() == database


@pytest.mark.parametrize("fail_startup", [False, True])
def test_real_host_lifespan_closes_owned_transport(tmp_path, monkeypatch, fail_startup):
    async def scenario():
        root = tmp_path / "host"
        await initialise(
            root, profile_markdown=REFERENCE_PROFILE, live=LiveConfig(model_id="fixture-model")
        )
        host = paths(root)
        factories = []
        original = serve.provider_factory

        def capture(*args, **kwargs):
            factory = original(*args, **kwargs)
            factories.append(factory)
            return factory

        monkeypatch.setattr(serve, "provider_factory", capture)
        app, closer = serve.build(
            host, load_config(host), provider="live", env_file=binding(tmp_path / "fixture.env")
        )
        adapter = factories[0](ADAPTER_ID).provider
        client, per_call = adapter._http(10)
        assert not per_call and adapter._http(3)[0] is client
        try:
            if fail_startup:

                async def broken():
                    raise RuntimeError("synthetic-startup")

                monkeypatch.setattr(app.state.runtime, "open", broken)
                with pytest.raises(RuntimeError, match="synthetic-startup"):
                    async with app.router.lifespan_context(app):
                        pytest.fail("startup must fail")
            else:
                close_runtime = app.state.runtime.close

                async def checked_close():
                    assert not client.is_closed
                    await close_runtime()
                    assert not client.is_closed

                monkeypatch.setattr(app.state.runtime, "close", checked_close)
                async with app.router.lifespan_context(app):
                    assert not client.is_closed
            assert client.is_closed
        finally:
            await factories[0].aclose()
            closer()

    asyncio.run(scenario())


def test_injected_http_client_remains_owned_by_caller():
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        provider = OpenAICompatibleProvider({}, client)
        provider.configure_keepalive(120)
        assert provider._http(5) == (client, False)
        await provider.aclose()
        assert not client.is_closed
        await client.aclose()

    asyncio.run(scenario())
