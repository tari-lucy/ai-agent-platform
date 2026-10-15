"""Single-host ownership and readiness, synthetic paths only; no external LLM or browser."""

import json
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest
from host_support import Server, command, free_port, init

from ai_agent_core.local_host.layout import HostError
from ai_agent_core.local_host.process import exclusive_host, loopback_socket, open_when_ready


def test_lock_rejects_parallel_root_and_recovers_after_release(tmp_path):
    with exclusive_host(tmp_path):
        with pytest.raises(HostError, match="HOST_ALREADY_RUNNING"):
            with exclusive_host(tmp_path):
                raise AssertionError("second runtime admitted")
    with exclusive_host(tmp_path):
        assert (tmp_path / "host.lock").exists()


def test_port_ownership_and_browser_readiness(monkeypatch):
    opened = []
    monkeypatch.setattr(
        "ai_agent_core.local_host.process.webbrowser.open",
        lambda *a, **k: opened.append(a[0]) or True,
    )
    port = free_port()
    with loopback_socket(port):
        with pytest.raises(HostError, match="PORT_UNAVAILABLE"):
            with loopback_socket(port):
                raise AssertionError("port shared")
    stopped = threading.Event()
    stopped.set()
    open_when_ready(SimpleNamespace(started=True), port, stopped)
    assert opened == []
    open_when_ready(SimpleNamespace(started=True), port, threading.Event())
    assert opened == [f"http://127.0.0.1:{port}/"]


def test_second_process_cannot_recover_active_owner(tmp_path):
    root = tmp_path / "host"
    assert init(root).returncode == 0
    config = json.loads((root / "host.json").read_text())
    query = dict(
        workspace_id=config["workspace_id"],
        persona_id=config["persona_id"],
        memory_space_id=config["work_space_id"],
        memory_mode="work",
    )
    password = (root / "secrets" / "operator-password.txt").read_text().strip()
    with Server(root, free_port(), tmp_path / "server.log") as server, server.client() as http:
        login = http.post(
            "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
        )
        assert login.status_code == 200
        headers = {"Origin": server.origin, "X-CSRF-Token": login.json()["csrf_token"]}
        run = http.get("/api/v1/sessions", params=query).json()["items"][0]
        response = http.post(
            "/api/v1/commands",
            headers=headers,
            json=dict(
                type="agent.start",
                request_id="one-owner",
                target="primary",
                scope=run["scope"],
                expected_version=run["version"],
                arguments={"autonomy": "observation"},
            ),
        )
        assert response.status_code == 200
        active = http.get("/api/v1/sessions", params=query).json()["items"][0]
        assert active["state"] == "ACTIVE"
        refused = subprocess.run(
            command("serve", "--root", str(root), "--provider", "mock", "--port", str(free_port())),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert refused.returncode == 78 and "HOST_ALREADY_RUNNING" in refused.stderr
        assert http.get("/api/v1/sessions", params=query).json()["items"][0] == active
    # Abrupt process termination releases the OS lock; stale file is harmless.
    with Server(root, free_port(), tmp_path / "restart.log") as server, server.client() as http:
        assert (
            http.post(
                "/api/v1/auth/login", json={"password": password}, headers={"Origin": server.origin}
            ).status_code
            == 200
        )
        assert http.get("/api/v1/sessions", params=query).json()["items"][0]["state"] == "STOPPED"


def test_occupied_port_is_refused_before_build(monkeypatch, tmp_path):
    from ai_agent_core.local_host import serve
    from ai_agent_core.local_host.layout import HostConfig

    config = HostConfig(
        workspace_id="w",
        persona_id="p",
        work_space_id="work",
        test_space_ids=("test",),
        port=8765,
        permission_expires_at="2099-01-01T00:00:00+00:00",
    )
    monkeypatch.setattr(serve, "load_config", lambda _: config)
    monkeypatch.setattr(
        serve, "build", lambda *a, **k: pytest.fail("opened runtime before owning port")
    )
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        with pytest.raises(HostError, match="PORT_UNAVAILABLE"):
            serve.serve(tmp_path, provider="mock", port=occupied.getsockname()[1])
