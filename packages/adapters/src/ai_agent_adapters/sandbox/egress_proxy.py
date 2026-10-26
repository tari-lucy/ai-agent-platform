"""Trusted egress proxy: PID 1 of the per-turn gateway container (``python -I -u -c``).

Standalone, standard library only, no platform imports. Serves HTTP CONNECT, absolute-form
``http://`` requests and SOCKS5 CONNECT on the same port (a first byte 0x05 is SOCKS).
Resolves names itself (the agent's network has no DNS and no route out) and connects only to
public addresses outside the denied networks and ports. One JSON line per request goes to
stdout; the host keeps that log and removes this container at the end of the turn or on STOP.
HTTPS is tunnelled, so only its host and port are known here.
"""

import asyncio
import contextlib
import ipaddress
import json
import os
import socket
import sys
import time
from typing import Any
from urllib.parse import urlsplit

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

PORT = 3128
MAX_HEAD = 16384
MAX_TARGET = 2048
IDLE_SECONDS = 300
# SOCKS5 reply with a code; the bound address is not meaningful here and stays zero.
SOCKS_REPLY = b"\x05%c\x00\x01\x00\x00\x00\x00\x00\x00"
DROPPED = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


def allowed(address: str, deny: list[Network]) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and not any(ip in network for network in deny)


def target(method: str, raw: str) -> tuple[str, int, str]:
    """(host, port, origin-form path); the path is empty for CONNECT."""
    if method == "CONNECT":
        host, _, port = raw.rpartition(":")
        host = host.strip("[]")
        path = ""
    else:
        parts = urlsplit(raw)
        if parts.scheme != "http" or parts.hostname is None:
            raise ValueError("EGRESS_SCHEME")
        host, port = parts.hostname, str(parts.port or 80)
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
    if not host or len(host) > 253 or not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError("EGRESS_TARGET")
    return host, int(port), path


class Proxy:
    def __init__(self, config: dict[str, Any]) -> None:
        self.deny = [ipaddress.ip_network(item) for item in config["deny"]]
        self.deny_ports = set(config["deny_ports"])
        self.max_bytes = config["max_bytes"]
        # The host lowers max_bytes to what is left of the persona's day and says so here.
        self.limit_reason = config.get("limit_reason", "EGRESS_LIMIT")
        self.timeout = config["connect_timeout"]
        self.slots = asyncio.Semaphore(config["max_connections"])
        self.used = 0

    def log(self, **row: object) -> None:
        print(json.dumps({"kind": "egress", **row}, ensure_ascii=False), flush=True)

    async def resolve(self, host: str, port: int) -> str:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), self.timeout
        )
        for info in infos:
            if allowed(info[4][0], self.deny):
                return info[4][0]
        raise PermissionError("EGRESS_DESTINATION_DENIED")

    async def pipe(
        self,
        source: asyncio.StreamReader,
        sink: asyncio.StreamWriter,
        stats: dict[str, Any],
        key: str,
    ) -> None:
        try:
            while data := await asyncio.wait_for(source.read(65536), IDLE_SECONDS):
                self.used += len(data)
                stats[key] += len(data)
                if self.used > self.max_bytes:
                    stats["status"] = "limit"
                    break
                sink.write(data)
                await sink.drain()
            else:
                if sink.can_write_eof():
                    sink.write_eof()
                return
        except OSError, TimeoutError, asyncio.IncompleteReadError:
            pass
        sink.close()

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        async with self.slots:
            await self.serve(reader, writer)

    async def socks(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> str:
        """SOCKS5 greeting and request (no authentication, CONNECT only) -> ``host:port``."""
        methods = await reader.readexactly((await reader.readexactly(1))[0])
        if 0 not in methods:
            writer.write(b"\x05\xff")
            raise ValueError("EGRESS_SOCKS_AUTH")
        writer.write(b"\x05\x00")
        version, command, _, kind = await reader.readexactly(4)
        if kind == 1:
            host = socket.inet_ntop(socket.AF_INET, await reader.readexactly(4))
        elif kind == 4:
            host = "[" + socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16)) + "]"
        elif kind == 3:
            host = (await reader.readexactly((await reader.readexactly(1))[0])).decode("ascii")
        else:
            raise ValueError("EGRESS_TARGET")
        port = int.from_bytes(await reader.readexactly(2))
        if version != 5 or command != 1:
            writer.write(SOCKS_REPLY % 7)  # command not supported: no BIND/UDP
            raise ValueError("EGRESS_SOCKS_COMMAND")
        return f"{host}:{port}"

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        started = time.monotonic()
        stats: dict[str, Any] = {"method": "", "target": "", "status": "error", "up": 0, "down": 0}
        address = ""
        upstream = None
        socks = False
        try:
            first = await asyncio.wait_for(reader.readexactly(1), 30)
            if first == b"\x05":
                socks = True
                stats["method"] = "SOCKS"
                raw = await asyncio.wait_for(self.socks(reader, writer), 30)
                method, version = "SOCKS", ""
            else:
                head = first + await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
                line, *headers = head.decode("latin-1").split("\r\n")
                method, raw, version = line.split(" ")
            stats["method"], stats["target"] = method, raw[:MAX_TARGET]
            try:
                host, port, path = target("CONNECT" if socks else method, raw)
            except ValueError as error:
                stats["status"] = "denied"
                stats["reason"] = str(error)
                writer.write(
                    SOCKS_REPLY % 1
                    if socks
                    else b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n"
                )
                return
            reason = None
            if port in self.deny_ports:
                reason = "EGRESS_PORT_DENIED"
            elif self.used >= self.max_bytes:
                reason = self.limit_reason
            if reason is None:
                try:
                    stats["ip"] = address = await self.resolve(host, port)
                except PermissionError as error:
                    reason = str(error)
            if reason is not None:
                stats["status"], stats["reason"] = "denied", reason
                body = reason.encode()
                writer.write(
                    SOCKS_REPLY % 2  # connection not allowed by ruleset
                    if socks
                    else b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                    + b"Content-Length: %d\r\n\r\n" % len(body)
                    + body
                )
                return
            upstream = await asyncio.wait_for(asyncio.open_connection(address, port), self.timeout)
            if socks:
                writer.write(SOCKS_REPLY % 0)
                await writer.drain()
            elif method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                kept = [h for h in headers if h and h.split(":", 1)[0].lower() not in DROPPED]
                origin = "\r\n".join([f"{method} {path} {version}", *kept, "Connection: close"])
                upstream[1].write(origin.encode("latin-1") + b"\r\n\r\n")
            stats["status"] = "ok"
            await asyncio.gather(
                self.pipe(reader, upstream[1], stats, "up"),
                self.pipe(upstream[0], writer, stats, "down"),
            )
        except (OSError, ValueError, TimeoutError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError) as error:  # fmt: skip
            if stats["status"] == "error":
                stats["reason"] = (
                    str(error) if str(error).startswith("EGRESS_") else type(error).__name__
                )
                # The greeting errors already answered; others get "general failure".
                if socks and upstream is None and not stats["reason"].startswith("EGRESS_SOCKS"):
                    with contextlib.suppress(OSError):
                        writer.write(SOCKS_REPLY % 1)
        finally:
            stats["seconds"] = round(time.monotonic() - started, 3)
            self.log(**stats)
            if upstream is not None:
                upstream[1].close()
            writer.close()


async def main() -> None:
    config = json.loads(sys.stdin.buffer.readline())
    proxy = Proxy(config)
    server = await asyncio.start_server(proxy.handle, "0.0.0.0", PORT, limit=MAX_HEAD)
    print(json.dumps({"kind": "ready"}), flush=True)
    loop = asyncio.get_running_loop()
    # The host holds stdin open for the turn; its end (host gone) ends the gateway too.
    await loop.run_in_executor(None, sys.stdin.buffer.read)
    server.close()
    os._exit(0)


if __name__ == "__main__":
    asyncio.run(main())
