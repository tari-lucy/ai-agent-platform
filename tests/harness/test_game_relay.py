"""The game relay forwards to its one target, refuses other sources and caps connections."""

import asyncio
import importlib.util
import ipaddress
from pathlib import Path

PATH = Path(__file__).resolve().parents[2] / "deploy" / "game-tunnel" / "game_relay.py"
SPEC = importlib.util.spec_from_file_location("game_relay", PATH)
assert SPEC and SPEC.loader
relay_module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay_module)
LOCAL = [ipaddress.ip_network("127.0.0.0/8")]


async def echo(reader, writer):
    writer.write(b"echo:" + await reader.read(100))
    await writer.drain()
    writer.close()


async def started(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


async def ask(port, payload=b"ping"):
    """The answer, or b"" when the relay refused (a clean close or a reset)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        return await reader.read(100)
    except ConnectionError:
        return b""
    finally:
        writer.close()


def test_it_forwards_to_the_target_and_only_for_allowed_sources():
    async def scenario():
        target, target_port = await started(echo)
        relay = relay_module.Relay(("127.0.0.1", target_port), LOCAL, limit=8)
        front, port = await started(relay.handle)
        assert await ask(port) == b"echo:ping"

        refused = relay_module.Relay(
            ("127.0.0.1", target_port), [ipaddress.ip_network("10.0.0.0/8")], limit=8
        )
        shut, shut_port = await started(refused.handle)
        assert await ask(shut_port) == b""  # a source outside the allow-list gets nothing
        for server in (target, front, shut):
            server.close()

    asyncio.run(scenario())


def test_a_target_that_is_down_closes_the_caller_without_hanging():
    async def scenario():
        unused, free_port = await started(echo)
        unused.close()
        await unused.wait_closed()
        relay = relay_module.Relay(("127.0.0.1", free_port), LOCAL, limit=8)
        front, port = await started(relay.handle)
        assert await asyncio.wait_for(ask(port), 8) == b""
        front.close()

    asyncio.run(scenario())


def test_the_connection_limit_refuses_the_rest():
    async def scenario():
        async def hold(reader, writer):
            await asyncio.sleep(0.5)
            writer.close()

        target, target_port = await started(hold)
        relay = relay_module.Relay(("127.0.0.1", target_port), LOCAL, limit=1)
        front, port = await started(relay.handle)
        first = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.sleep(0.1)
        assert await ask(port) == b""  # the second one is over the limit
        first[1].close()
        for server in (target, front):
            server.close()

    asyncio.run(scenario())
