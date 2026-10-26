"""W3 egress proxy logic in-process: address policy, request targets, byte limit; no Docker."""

import asyncio
import ipaddress
import json

import pytest
from ai_agent_adapters.sandbox import egress_proxy
from ai_agent_adapters.sandbox.egress import MAIL_PORTS, EgressLimits
from ai_agent_adapters.sandbox.egress_proxy import Proxy, allowed, target
from ai_agent_adapters.sandbox.policy import RunnerTemplate, create_args
from ai_agent_application.agent_workplace.ports import RunnerLimits, RunnerTask


def test_only_public_addresses_outside_the_deny_list():
    for address in ("93.184.215.14", "2606:4700::6810:85e5"):
        assert allowed(address, [])
    for address in (
        "127.0.0.1", "10.1.2.3", "172.17.0.1", "192.168.1.10", "169.254.169.254",
        "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1",
        "::ffff:192.168.1.10", "::ffff:127.0.0.1",
    ):  # fmt: skip
        assert not allowed(address, []), address
    deny = [ipaddress.ip_network("93.184.215.0/24")]
    assert not allowed("93.184.215.14", deny) and allowed("1.1.1.1", deny)


def test_request_targets():
    assert target("CONNECT", "example.com:443") == ("example.com", 443, "")
    assert target("CONNECT", "[2606:4700::1]:443") == ("2606:4700::1", 443, "")
    assert target("GET", "http://example.com/a?b=1") == ("example.com", 80, "/a?b=1")
    assert target("GET", "http://example.com:8080") == ("example.com", 8080, "/")
    for method, raw in (
        ("GET", "https://example.com/"),  # TLS goes through CONNECT, not plain forwarding
        ("GET", "/relative"),
        ("GET", "ftp://example.com/"),
        ("CONNECT", "example.com"),
        ("CONNECT", "example.com:0"),
        ("CONNECT", "example.com:99999"),
    ):
        with pytest.raises(ValueError):
            target(method, raw)


def test_limits_are_validated_and_mail_is_closed_by_default():
    assert set(MAIL_PORTS) <= set(EgressLimits().deny_ports)
    for bad in (
        dict(deny=("not-a-network",)),
        dict(max_bytes=0),
        dict(max_connections=0),
        dict(deny_ports=(70000,)),
    ):
        with pytest.raises(ValueError):
            EgressLimits(**bad)
    assert EgressLimits(deny=("203.0.113.0/24",)).wire()["deny"] == ["203.0.113.0/24"]


def test_agent_joins_only_the_turns_network():
    cap = RunnerLimits(3, 128, 16, 3, 8192, 1024 * 1024)
    template = RunnerTemplate("t", "sha256:" + "a" * 64, ("/usr/local/bin/python",), cap, 1)
    task = RunnerTask("t", ("/usr/local/bin/python",), (), cap)
    name = "aw-task-" + "b" * 32
    assert "--network=none" in create_args(template, task, name, "pass")
    net = "aw-net-" + "c" * 32
    assert "--network=" + net in create_args(template, task, name, "pass", network=net)
    for bad in ("bridge", "host", "aw-net-x", "container:other"):
        with pytest.raises(ValueError):
            create_args(template, task, name, "pass", network=bad)


def test_proxy_refuses_denied_destinations_and_stops_at_the_byte_limit(monkeypatch, capsys):
    """A local upstream stands in for the internet; only the address policy is relaxed."""

    async def scenario():
        body = b"x" * 5000

        async def upstream(reader, writer):
            await reader.readuntil(b"\r\n\r\n")
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n" + body)
            await writer.drain()
            writer.close()

        origin = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = origin.sockets[0].getsockname()[1]
        config = dict(
            deny=[], deny_ports=[25], max_bytes=3000, connect_timeout=5, max_connections=4
        )
        proxy = Proxy(config)
        server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
        proxy_port = server.sockets[0].getsockname()[1]

        async def ask(line: str) -> bytes:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(line.encode() + b"\r\nHost: x\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            writer.close()
            return data

        # Loopback is refused by the real policy.
        refused = await ask(f"GET http://127.0.0.1:{port}/ HTTP/1.1")
        assert refused.startswith(b"HTTP/1.1 403") and b"EGRESS_DESTINATION_DENIED" in refused
        assert (await ask("GET http://example.com:25/ HTTP/1.1")).startswith(b"HTTP/1.1 403")
        # With the policy relaxed for the stand-in, the transfer stops at the byte limit.
        monkeypatch.setattr(egress_proxy, "allowed", lambda address, deny: True)
        cut = await ask(f"GET http://127.0.0.1:{port}/path?q=1 HTTP/1.1")
        assert body not in cut  # cut off: nothing past the limit reaches the agent
        late = await ask(f"GET http://127.0.0.1:{port}/ HTTP/1.1")
        assert late.startswith(b"HTTP/1.1 403") and b"EGRESS_LIMIT" in late
        server.close()
        origin.close()

    asyncio.run(scenario())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    statuses = [(row["status"], row.get("reason")) for row in rows]
    assert statuses == [
        ("denied", "EGRESS_DESTINATION_DENIED"),
        ("denied", "EGRESS_PORT_DENIED"),
        ("limit", None),
        ("denied", "EGRESS_LIMIT"),
    ]
    assert rows[2]["target"].endswith("/path?q=1") and rows[2]["down"] <= 3000 + 65536


def test_proxy_names_the_daily_limit_when_the_host_lowered_it(capsys):
    async def scenario():
        config = dict(
            deny=[], deny_ports=[25], max_bytes=0, connect_timeout=5, max_connections=4,
            limit_reason="EGRESS_DAILY_LIMIT",
        )  # fmt: skip
        server = await asyncio.start_server(Proxy(config).handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        await writer.drain()
        answer = await reader.read()
        writer.close()
        server.close()
        return answer

    answer = asyncio.run(scenario())
    assert answer.startswith(b"HTTP/1.1 403") and b"EGRESS_DAILY_LIMIT" in answer
    (row,) = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert row["status"] == "denied" and row["reason"] == "EGRESS_DAILY_LIMIT"


def test_socks5_connect_follows_the_same_policy_and_log(monkeypatch, capsys):
    async def scenario():
        async def upstream(reader, writer):
            writer.write(b"pong:" + await reader.readexactly(4))
            await writer.drain()
            writer.close()

        origin = await asyncio.start_server(upstream, "127.0.0.1", 0)
        port = origin.sockets[0].getsockname()[1]
        config = dict(
            deny=[], deny_ports=[25], max_bytes=10_000, connect_timeout=5, max_connections=4
        )
        server = await asyncio.start_server(Proxy(config).handle, "127.0.0.1", 0)
        proxy_port = server.sockets[0].getsockname()[1]

        async def socks(request: bytes, greeting=b"\x05\x01\x00", payload=b""):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            writer.write(greeting + request + payload)
            await writer.drain()
            data = await reader.read()
            writer.close()
            return data

        name = b"localhost"
        by_name = b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2)
        # Loopback by name is refused by the real policy: reply 2 (not allowed by ruleset).
        assert (await socks(by_name)) == b"\x05\x00" + b"\x05\x02" + b"\x00\x01" + bytes(6)
        mail = b"\x05\x01\x00\x01" + bytes([93, 184, 215, 14]) + (25).to_bytes(2)
        assert (await socks(mail))[2:4] == b"\x05\x02"
        bind = b"\x05\x02\x00\x01" + bytes(4) + (80).to_bytes(2)
        assert (await socks(bind))[2:4] == b"\x05\x07"
        assert (await socks(b"", greeting=b"\x05\x01\x02")) == b"\x05\xff"  # password only
        monkeypatch.setattr(egress_proxy, "allowed", lambda address, deny: True)
        ok = await socks(by_name, payload=b"ping")
        assert ok == b"\x05\x00\x05\x00\x00\x01" + bytes(6) + b"pong:ping"
        server.close()
        origin.close()

    asyncio.run(scenario())
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(row["method"], row["status"], row.get("reason")) for row in rows] == [
        ("SOCKS", "denied", "EGRESS_DESTINATION_DENIED"),
        ("SOCKS", "denied", "EGRESS_PORT_DENIED"),
        ("SOCKS", "error", "EGRESS_SOCKS_COMMAND"),
        ("SOCKS", "error", "EGRESS_SOCKS_AUTH"),
        ("SOCKS", "ok", None),
    ]
    assert rows[-1]["target"].startswith("localhost:") and rows[-1]["up"] == 4
