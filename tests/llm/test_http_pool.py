"""Real loopback HTTP/1.1 pooling, cookies, expiry and cancellation. Synthetic payloads only."""

import asyncio

import pytest
from ai_agent_adapters.llm import ADAPTER_ID, OpenAICompatibleProvider
from ai_agent_application.llm import Finished, ProviderCall, ProviderError
from llm_support import CREDENTIAL_SETTING, ENDPOINT_SETTING, request, resolved
from test_openai_adapter import FULL


class Loopback:
    def __init__(self, *, blocked=None, close_first=False):
        self.blocked, self.close_first = blocked, close_first
        self.connections = 0
        self.requests = []
        self.writers, self.tasks = set(), set()
        self.entered, self.cancelled = asyncio.Event(), asyncio.Event()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.url = f"http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}/v1"
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()
        for writer in tuple(self.writers):
            writer.close()
        await asyncio.gather(*tuple(self.tasks), return_exceptions=True)

    async def handle(self, reader, writer):
        self.connections += 1
        connection = self.connections
        self.writers.add(writer)
        task = asyncio.current_task()
        self.tasks.add(task)
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                headers = {
                    line.split(b":", 1)[0].lower(): line.split(b":", 1)[1].strip()
                    for line in head.split(b"\r\n")[1:]
                    if b":" in line
                }
                await reader.readexactly(int(headers[b"content-length"]))
                self.requests.append((connection, headers))
                first = len(self.requests) == 1
                if first and self.blocked == "headers":
                    self.entered.set()
                    await reader.read()
                    self.cancelled.set()
                    return
                close = first and self.close_first
                size = len(FULL) + (1 if first and self.blocked == "done" else 0)
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                    + f"Content-Length: {size}\r\n".encode()
                    + b"Set-Cookie: synthetic-session=must-not-persist; Path=/\r\n"
                    + (b"Connection: close\r\n" if close else b"")
                    + b"\r\n"
                )
                await writer.drain()
                if first and self.blocked == "body":
                    self.entered.set()
                    await reader.read()
                    self.cancelled.set()
                    return
                writer.write(FULL)
                await writer.drain()
                if first and self.blocked == "done":
                    await reader.read()
                    self.cancelled.set()
                    return
                if close:
                    return
        except asyncio.IncompleteReadError, ConnectionError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.writers.discard(writer)
            self.tasks.discard(task)


def adapter(url, seconds):
    provider = OpenAICompatibleProvider(
        {ENDPOINT_SETTING: url, CREDENTIAL_SETTING: "synthetic-key"}
    )
    provider.configure_keepalive(seconds)
    return provider


async def collect(provider, timeout=60):
    call = ProviderCall(
        resolved(
            adapter=ADAPTER_ID, parameters={"timeout_seconds": timeout, "max_output_tokens": 64}
        ),
        request().messages,
    )
    events = [event async for event in provider.stream(call)]
    assert any(isinstance(event, Finished) for event in events)


@pytest.mark.parametrize("expiry", [0, 120])
def test_shared_client_reuses_tcp_only_with_keepalive_and_never_keeps_cookies(expiry):
    async def scenario():
        async with Loopback() as server:
            provider = adapter(server.url, expiry)
            try:
                await collect(provider)
                shared = provider._client
                assert shared is not None and not shared.is_closed
                timeouts = []

                async def captured(request):
                    timeouts.append(request.extensions["timeout"])

                shared.event_hooks["request"].append(captured)
                await collect(provider, timeout=7)
                assert provider._client is shared
                assert server.connections == (1 if expiry else 2)
                assert len(server.requests) == 2
                assert all(b"cookie" not in headers for _, headers in server.requests)
                assert "authorization" not in shared.headers and not list(shared.cookies)
                assert timeouts == [{"connect": 7, "read": 7, "write": 7, "pool": 7}]
            finally:
                await provider.aclose()
            assert shared.is_closed
            await provider.aclose()  # idempotent
            with pytest.raises(ProviderError):
                await collect(provider)

    asyncio.run(scenario())


def test_expired_idle_connection_and_provider_close_get_a_new_socket_without_replay():
    async def scenario():
        async with Loopback() as server:
            provider = adapter(server.url, 0.02)
            try:
                await collect(provider)
                shared = provider._client
                await asyncio.sleep(0.06)
                await collect(provider)
                assert provider._client is shared and server.connections == 2
                assert len(server.requests) == 2
            finally:
                await provider.aclose()
        async with Loopback(close_first=True) as server:
            provider = adapter(server.url, 120)
            try:
                await collect(provider)
                await collect(provider)
                assert server.connections == len(server.requests) == 2
            finally:
                await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("blocked", ["headers", "body"])
def test_cancelling_one_request_closes_its_socket_but_not_the_shared_client(blocked):
    async def scenario():
        async with Loopback(blocked=blocked) as server:
            provider = adapter(server.url, 120)
            pending = asyncio.create_task(collect(provider))
            try:
                await asyncio.wait_for(server.entered.wait(), timeout=3)
                shared = provider._client
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
                await asyncio.wait_for(server.cancelled.wait(), timeout=3)
                assert shared is not None and not shared.is_closed
                await collect(provider)
                assert provider._client is shared and len(server.requests) == 2
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                await provider.aclose()

    asyncio.run(scenario())


def test_done_does_not_wait_for_missing_http_eof():
    async def scenario():
        async with Loopback(blocked="done") as server:
            provider = adapter(server.url, 120)
            try:
                await asyncio.wait_for(collect(provider), timeout=2)
                await asyncio.wait_for(server.cancelled.wait(), timeout=2)
                await collect(provider)
                assert server.connections == len(server.requests) == 2
            finally:
                await provider.aclose()

    asyncio.run(scenario())
