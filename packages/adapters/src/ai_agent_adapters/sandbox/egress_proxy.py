"""Trusted egress proxy: PID 1 of the per-turn gateway container (``python -I -u -c``).

Standalone, standard library only, no platform imports. Serves HTTP CONNECT and absolute-form
``http://`` requests. Resolves names itself (the agent's network has no DNS and no route out)
and connects only to public addresses outside the denied networks and ports. One JSON line per
request goes to stdout; the host keeps that log and removes this container at the end of the
turn or on STOP. HTTPS is tunnelled, so only its host and port are known here.
"""

import asyncio
import ipaddress
import json
import os
import socket
import sys
import time
from urllib.parse import urlsplit

PORT = 3128
MAX_HEAD = 16384
MAX_TARGET = 2048
IDLE_SECONDS = 300
DROPPED = {"proxy-connection", "proxy-authorization", "connection", "keep-alive"}


def allowed(address: str, deny: list) -> bool:
    ip = ipaddress.ip_address(address.split("%", 1)[0])
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not any(ip in network for network in deny)


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
    def __init__(self, config: dict) -> None:
        self.deny = [ipaddress.ip_network(item) for item in config["deny"]]
        self.deny_ports = set(config["deny_ports"])
        self.max_bytes = config["max_bytes"]
        self.timeout = config["connect_timeout"]
        self.slots = asyncio.Semaphore(config["max_connections"])
        self.used = 0

    def log(self, **row) -> None:
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

    async def pipe(self, source, sink, stats: dict, key: str) -> None:
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
        except (OSError, TimeoutError, asyncio.IncompleteReadError):
            pass
        sink.close()

    async def handle(self, reader, writer) -> None:
        async with self.slots:
            await self.serve(reader, writer)

    async def serve(self, reader, writer) -> None:
        started = time.monotonic()
        stats = {"method": "", "target": "", "status": "error", "up": 0, "down": 0}
        upstream = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30)
            line, *headers = head.decode("latin-1").split("\r\n")
            method, raw, version = line.split(" ")
            stats["method"], stats["target"] = method, raw[:MAX_TARGET]
            try:
                host, port, path = target(method, raw)
            except ValueError as error:
                stats["status"] = "denied"
                stats["reason"] = str(error)
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            reason = None
            if port in self.deny_ports:
                reason = "EGRESS_PORT_DENIED"
            elif self.used >= self.max_bytes:
                reason = "EGRESS_LIMIT"
            if reason is None:
                try:
                    stats["ip"] = await self.resolve(host, port)
                except PermissionError as error:
                    reason = str(error)
            if reason is not None:
                stats["status"], stats["reason"] = "denied", reason
                body = reason.encode()
                writer.write(
                    b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
                    + b"Content-Length: %d\r\n\r\n" % len(body)
                    + body
                )
                return
            upstream = await asyncio.wait_for(
                asyncio.open_connection(stats["ip"], port), self.timeout
            )
            if method == "CONNECT":
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
                stats["reason"] = type(error).__name__
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
