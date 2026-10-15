"""Real subprocess `init`/`serve` of the local host and a plain HTTP client. No ASGI shortcuts."""

import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

MODULE = "ai_agent_core.local_host"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def command(*arguments: str) -> list[str]:
    return [sys.executable, "-I", "-m", MODULE, *arguments]


def init(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        command("init", "--root", str(root), *arguments),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


class Server:
    """`serve` as a child process; stdout/stderr go to a file the test can inspect."""

    def __init__(
        self,
        root: Path,
        port: int,
        log: Path,
        provider: str = "mock",
        panel_dir: Path | None = None,
    ) -> None:
        self.root, self.port, self.log = root, port, log
        self.origin = f"http://127.0.0.1:{port}"
        self.provider, self.panel_dir = provider, panel_dir
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> Server:
        handle = self.log.open("ab")
        self.process = subprocess.Popen(
            command(
                *(("serve", "--panel-dir", str(self.panel_dir)) if self.panel_dir else ("serve",)),
                "--root",
                str(self.root),
                "--provider",
                self.provider,
                "--port",
                str(self.port),
            ),
            stdout=handle,
            stderr=handle,
        )
        handle.close()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"serve exited early: {self.process.returncode}")
            try:
                httpx.get(self.origin + "/api/v1/auth/session", timeout=1)
                return self
            except httpx.TransportError:
                time.sleep(0.2)
        raise AssertionError("serve did not become reachable")

    def __exit__(self, *_) -> None:
        assert self.process is not None
        self.process.terminate()
        try:
            self.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()

    def client(self) -> httpx.Client:
        return httpx.Client(base_url=self.origin, timeout=15)


def scope_query(scope: dict) -> dict:
    return {
        "workspace_id": scope["workspace_id"],
        "persona_id": scope["persona_id"],
        "memory_space_id": scope["memory_space_id"],
        "memory_mode": scope["memory_mode"],
    }


def wait_for(read, done, what: str, seconds: float = 30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = read()
        if done(value):
            return value
        time.sleep(0.2)
    raise AssertionError(f"timeout waiting for {what}")
