"""Relay from the Docker bridge to the game tunnel's loopback listener (stdlib only).

The persona's gateway container reaches ``host.docker.internal`` which on Linux is the default
bridge address (172.17.0.1). The reverse SSH tunnel from the operator's computer can only listen
on the server's loopback. This relay bridges the two and nothing else: one fixed target, a
source allow-list (the default bridge only, so other containers such as the n8n stack, which
sit on their own networks, are refused), a connection limit, no logging of content.
"""

import argparse
import asyncio
import ipaddress
import logging

LOG = logging.getLogger("game_relay")
CHUNK = 64 * 1024


async def copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(CHUNK):
            writer.write(data)
            await writer.drain()
    except ConnectionError, OSError:
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


class Relay:
    def __init__(self, target: tuple[str, int], allowed: list[ipaddress.IPv4Network], limit: int):
        self.target, self.allowed, self.limit = target, allowed, limit
        self.active = 0

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        source = ipaddress.ip_address(peer[0]) if peer else None
        if source is None or not any(source in network for network in self.allowed):
            LOG.warning("Refused a source outside the allow-list")
            writer.close()
            return
        if self.active >= self.limit:
            LOG.warning("Refused: too many connections")
            writer.close()
            return
        self.active += 1
        try:
            try:
                far_reader, far_writer = await asyncio.wait_for(
                    asyncio.open_connection(*self.target), timeout=5
                )
            except OSError, TimeoutError:
                writer.close()  # the tunnel (or the game) is down: the caller sees a refusal
                return
            await asyncio.gather(copy(reader, far_writer), copy(far_reader, writer))
        finally:
            self.active -= 1


async def serve(listen: tuple[str, int], relay: Relay) -> None:
    server = await asyncio.start_server(relay.handle, *listen, reuse_address=True)
    LOG.warning("Game relay %s:%d -> %s:%d", *listen, *relay.target)
    async with server:
        await server.serve_forever()


def endpoint(text: str) -> tuple[str, int]:
    host, _, port = text.rpartition(":")
    return host, int(port)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="172.17.0.1:25350")
    parser.add_argument("--target", default="127.0.0.1:25350")
    parser.add_argument("--allow", action="append", default=None, help="source network (CIDR)")
    parser.add_argument("--limit", type=int, default=8)
    options = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    allowed = [ipaddress.ip_network(n) for n in (options.allow or ["172.17.0.0/16"])]
    relay = Relay(endpoint(options.target), allowed, options.limit)  # type: ignore[arg-type]
    asyncio.run(serve(endpoint(options.listen), relay))


if __name__ == "__main__":
    main()
