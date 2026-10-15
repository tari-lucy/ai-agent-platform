"""Synthetic host from the real `initialise`, owner-path writes, the real CLI, a real `serve`."""

import asyncio
import contextlib
import hashlib
import io
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests" / "usage"))

from ai_agent_adapters.recall import SQLiteRecallIndex  # noqa: E402
from ai_agent_adapters.retention import SQLiteDeletionLedger, SQLitePhysicalStore  # noqa: E402
from ai_agent_adapters.storage.database import SQLiteStore  # noqa: E402
from ai_agent_application.laboratory.profile import REFERENCE_PROFILE  # noqa: E402
from ai_agent_application.memory import (  # noqa: E402
    IndexHealth,
    MemoryService,
    RecallService,
    SourceDraft,
)
from ai_agent_application.retention import RetentionService  # noqa: E402
from ai_agent_application.storage import Owner  # noqa: E402
from ai_agent_application.usage import UsageLedger  # noqa: E402
from ai_agent_contracts.common import Scope  # noqa: E402
from ai_agent_contracts.memory import Provenance, RecallRequest  # noqa: E402
from usage_support import report as attempt_report  # noqa: E402

from ai_agent_core.local_backup.cli import main as backup_main  # noqa: E402
from ai_agent_core.local_host.init import initialise, work_scope  # noqa: E402
from ai_agent_core.local_host.layout import HostConfig, LiveConfig, load_config, paths  # noqa: E402
from ai_agent_core.m3_composition import new_registry  # noqa: E402
from ai_agent_core.m3_composition.state import PRINCIPALS, command  # noqa: E402

SECRET = "Янтарный-шифр-СеребристыйКлюч"
KEPT = "Долгая беседа о парусах и ветре над бухтой."
MEMORY = PRINCIPALS[Owner.MEMORY]
FOREIGN_NAMES = (".env", "debug.log", "operator-password.txt")


def run(coro):
    return asyncio.run(coro)


@dataclass
class Host:
    root: Path
    store_dir: Path
    ledger: Path
    config: HostConfig

    @property
    def paths(self):
        return paths(self.root)

    @property
    def scope(self) -> Scope:
        c = self.config
        return work_scope(c.workspace_id, c.persona_id, c.work_space_id)

    def args(self, *more: str, ledger: bool = True) -> list[str]:
        base = ["--root", str(self.root), "--store", str(self.store_dir)]
        if ledger:
            base += ["--ledger", str(self.ledger)]
        return [*base, *more]


async def make_host(
    base: Path,
    name: str = "host",
    *,
    live_model: str | None = None,
    retention: bool = True,
    ledger: bool = True,
) -> Host:
    """`initialise` as an operator would run it, then the retention owner's own admin step."""
    root = base / name
    previous = os.umask(0o077)
    try:
        live = None if live_model is None else LiveConfig(model_id=live_model)
        config = await initialise(
            root, profile_markdown=REFERENCE_PROFILE, port=1024, test_scopes=2, live=live
        )
    finally:
        os.umask(previous)
    host = Host(root, base / f"{name}-copies", base / f"{name}-ledger" / "deletions.sqlite", config)
    if retention:
        store = SQLiteStore(host.paths.database, extensions=new_registry())
        await SQLitePhysicalStore(store).install()
        await store.close()
    if ledger:
        host.ledger.parent.mkdir(mode=0o700)
        SQLiteDeletionLedger(host.ledger).close()
    return host


class Live:
    """The host's own stores opened as `serve` would, plus the retention owner (a fixture)."""

    def __init__(self, host: Host) -> None:
        self.host = host
        self.registry = new_registry()
        self.store = SQLiteStore(host.paths.database, extensions=self.registry)
        self.index = SQLiteRecallIndex(host.paths.dialogue_index)
        self.ledger = SQLiteDeletionLedger(host.ledger)
        health = IndexHealth()
        self.memory = MemoryService(
            self.store, MEMORY, self.registry, index=self.index, health=health
        )
        self.recall = RecallService(self.store, MEMORY, self.index, health=health)
        self.retention = RetentionService(
            self.store,
            MEMORY,
            self.memory,
            SQLitePhysicalStore(self.store),
            self.ledger,
            index=self.index,
        )
        self.scope = host.scope

    async def add(self, text: str, external_id: str) -> str:
        draft = SourceDraft(
            provenance=Provenance.THIRD_PARTY_MESSAGE,
            source_type="synthetic-chat",
            adapter_id="fixture",
            adapter_version="1",
            occurred_at=datetime.now(UTC) - timedelta(days=1),
            content=text,
            read_allowed=True,
            api_transfer_allowed=True,
            public_disclosure_allowed=False,
            external_id=external_id,
            author_id="person-1",
        )
        receipt = await self.memory.register_source(
            command(self.scope, "src-" + external_id), draft
        )
        return str(receipt.source_id)

    async def delete(self, source_id: str) -> None:
        preview = await self.retention.preview_delete(self.scope, [source_id])
        await self.retention.delete(self.scope, preview)

    async def hears(self, query: str) -> list[str]:
        request = RecallRequest(scope=self.scope, query=query, purpose="private_context")
        result = await self.recall.recall(request)
        return [fragment.content for fragment in result.fragments]

    async def attempt(self, name: str, *, connection: str = "conn-live") -> None:
        """A durable API attempt through the usage owner."""
        ledger = UsageLedger(self.store, PRINCIPALS[Owner.LLM], self.registry)
        item = attempt_report(f"{name}.a1", operation=name, at=datetime.now(UTC))
        item = item.__class__(**{**item.__dict__, "connection_id": connection})
        await ledger.begin_attempt(command(self.scope, "begin-" + name), item)

    async def close(self) -> None:
        await self.store.close()
        self.index.close()
        self.ledger.close()


async def with_live(host: Host, work):
    live = Live(host)
    try:
        return await work(live)
    finally:
        await live.close()


def cli(*arguments: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = backup_main(list(arguments))
    return code, out.getvalue(), err.getvalue()


def digest_of(output: str) -> str:
    return next(line.split()[1] for line in output.splitlines() if line.startswith("digest:"))


def copy_id(output: str) -> str:
    return next(line.split()[1] for line in output.splitlines() if line.startswith("created:"))


def tree_digest(base: Path) -> str:
    """Names and bytes of everything under a directory (a live host must stay byte-identical)."""
    digest = hashlib.sha256()
    for item in sorted(base.rglob("*")):
        digest.update(str(item.relative_to(base)).encode())
        if item.is_file() and item.name != "host.lock":
            digest.update(item.read_bytes())
    return digest.hexdigest()


def residue(host: Host, needle: str) -> list[str]:
    """Every file of the host where the text is still physically present (db, WAL, SHM, index)."""
    found = []
    raw = needle.encode()
    for base in (host.paths.database, host.paths.dialogue_index):
        for suffix in ("", "-wal", "-shm"):
            path = Path(str(base) + suffix)
            if path.exists() and raw in path.read_bytes():
                found.append(path.name)
    return found


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Serve:
    """A real `serve` child on this checkout's sources (the venv's editable install may differ)."""

    def __init__(self, host: Host, log: Path) -> None:
        self.host, self.log, self.port = host, log, free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.process: subprocess.Popen | None = None

    def __enter__(self) -> Serve:
        sources = [
            str(p) for p in sorted(ROOT.glob("apps/*/src")) + sorted(ROOT.glob("packages/*/src"))
        ]
        environment = {**os.environ, "PYTHONPATH": os.pathsep.join(sources)}
        handle = self.log.open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "ai_agent_core.local_host",
                "serve",
                "--root",
                str(self.host.root),
                "--provider",
                "mock",
                "--port",
                str(self.port),
            ],
            stdout=handle,
            stderr=handle,
            env=environment,
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

    def run_state(self) -> str:
        """Log in with the generated local password and read the work scope's run state."""
        password = self.host.paths.password.read_text().strip()
        config = load_config(self.host.paths)
        origin = {"Origin": self.origin}
        with httpx.Client(base_url=self.origin, timeout=15) as http:
            login = http.post("/api/v1/auth/login", json={"password": password}, headers=origin)
            assert login.status_code == 200, login.text
            query = {
                "workspace_id": config.workspace_id,
                "persona_id": config.persona_id,
                "memory_space_id": config.work_space_id,
                "memory_mode": "work",
            }
            response = http.get("/api/v1/sessions", params=query)
            assert response.status_code == 200, response.text
            return str(response.json()["items"][0]["state"])
