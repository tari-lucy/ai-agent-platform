"""Opt-in live four-minute lease probe on a NEW host; at most THREE paid HTTP calls.

Never called from pytest. Credentials are loaded only by ordinary local_host provider setup.
The report contains counts/codes/booleans, never model text, credentials or endpoint.
"""

import argparse
import asyncio
import base64
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
from ai_agent_adapters.llm import OpenAICompatibleProvider
from ai_agent_adapters.sandbox.home_files import HomeFiles

from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.activity import configure_activity
from ai_agent_core.local_host.init import initialise, work_scope
from ai_agent_core.local_host.layout import LiveConfig, load_config, paths, private_dir
from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.serve import build
from ai_agent_core.local_host.tickets import enable_tickets
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files
from ai_agent_core.m3_composition.work_session_home import volume

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://127.0.0.1:8765"
# Swapped for the probe's counted transport and restored; not a second credential path.
PROVIDER = "OpenAICompatibleProvider"
PROMPT = """Это диагностическая задача на вымышленных данных.
Нужно проверить один длинный рабочий ход, а не навыки исследования.
Не осматривай инструменты и файлы, не используй интернет и не заводи обращения.
Профиль рабочего места уже разрешён.
Выполни ОДИН вызов bash с timeout=300000 (миллисекунды), строго в переднем плане:
mkdir -p /workspace/share; sleep 240; printf lease_probe_ok > /workspace/share/lease-probe.done
Не переводи команду в фон, не ставь будильник и не дели ожидание на несколько вызовов.
Когда инструмент завершится успешно, ответь одной короткой фразой.
Ничего другого делать не нужно. Секреты и чужие файлы не трогай.
"""


class Counted(httpx.AsyncBaseTransport):
    def __init__(self, limit: int, keepalive_seconds: float = 120) -> None:
        self.limit = limit
        self.calls = 0
        self.inner = httpx.AsyncHTTPTransport(
            retries=0, limits=httpx.Limits(keepalive_expiry=keepalive_seconds)
        )
        self.headers: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.calls >= self.limit:
            raise httpx.TransportError("PROBE_CALL_LIMIT")
        self.calls += 1
        started = time.monotonic()
        print("LIVE_HTTP_STARTED call=" + str(self.calls), flush=True)
        response = await self.inner.handle_async_request(request)
        item = dict(
            call=self.calls,
            status=response.status_code,
            response_headers_seconds=round(time.monotonic() - started, 3),
        )
        self.headers.append(item)
        print("LIVE_HTTP_HEADERS " + json.dumps(item), flush=True)
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


async def run(a: argparse.Namespace) -> int:
    if not a.workdir.is_absolute() or a.workdir.exists():
        raise ValueError("NEW_ABSOLUTE_WORKDIR_REQUIRED")
    private_dir(a.workdir)
    root = a.workdir / "host"
    report: dict[str, Any] = {
        "paid_probe": True,
        "max_calls": a.max_calls,
        "calls": 0,
        "phases": {},
        "success": False,
        "stream_tools": a.stream_tools,
        "timeout_seconds": a.timeout_seconds,
        "keepalive_seconds": a.keepalive_seconds,
    }

    def save() -> None:
        (a.workdir / "result.json").write_text(json.dumps(report, indent=2), encoding="utf8")

    save()
    await initialise(
        root,
        profile_markdown=(ROOT / "profiles/examples/opechatka/profile.md").read_text(
            encoding="utf8"
        ),
        workspace_id="live-acceptance-" + uuid4().hex,
        persona_id="example-character",
        operator_controls=True,
        live=LiveConfig(
            model_id=a.model,
            attempt_limit=100,
            attempt_period="utc_day",
            max_output_tokens=1024,
            max_prompt_chars=400000,
            timeout_seconds=a.timeout_seconds,
            http_keepalive_seconds=a.keepalive_seconds,
        ),
    )
    await enable_tickets(root)
    await configure_activity(
        root, enabled=True, daily_calls=20, own_goals=False, stream_tools=a.stream_tools
    )
    await enable_recovery(
        root, store_dir=a.workdir / "backups", ledger_path=a.workdir / "ledger/events.sqlite"
    )
    await enable_workplace_files(root, ceiling_days=0)
    await enable_work_sessions(
        root,
        image_id=a.image_id,
        areas=["inbox", "projects", "knowledge", "skills"],
        output_area="projects",
        ceiling_days=0,
        home_enabled=True,
        resident_enabled=True,
        egress_enabled=True,
        wake_enabled=False,
        wall_seconds=600,
        cpu_seconds=120,
        memory_mb=2048,
        pids=256,
        disk_mb=512,
        max_model_calls=a.max_calls,
        max_model_tokens=1024,
        egress_daily_mb=5,
        cpu_daily_minutes=10,
        resident_idle_seconds=600,
    )
    cfg = load_config(paths(root))
    scope = work_scope(cfg.workspace_id, cfg.persona_id, cfg.work_space_id)
    query = {
        k: getattr(scope, k)
        for k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
    }
    wire = Counted(a.max_calls, a.keepalive_seconds)
    original = getattr(providers, PROVIDER)  # restored below
    async with httpx.AsyncClient(
        transport=wire,
        timeout=httpx.Timeout(a.timeout_seconds, connect=10),
        follow_redirects=False,
    ) as client:
        setattr(
            providers,
            PROVIDER,
            lambda settings: OpenAICompatibleProvider(settings, client),
        )
        app, close = build(paths(root), cfg, provider="live", env_file=a.env_file)
        rt = app.state.runtime
        try:
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=ORIGIN, timeout=30
                ) as http:
                    auth = await http.post(
                        "/api/v1/auth/login",
                        json={"password": paths(root).password.read_text().strip()},
                        headers={"Origin": ORIGIN},
                    )
                    assert auth.status_code == 200
                    headers = {"Origin": ORIGIN, "x-csrf-token": auth.json()["csrf_token"]}

                    async def post(
                        kind: str,
                        target: str,
                        args: dict[str, Any],
                        version: int | None = None,
                        chosen: Any = None,
                    ) -> Any:
                        body = dict(
                            type=kind,
                            request_id="probe-" + uuid4().hex,
                            target=target,
                            scope=(chosen or scope).model_dump(mode="json"),
                            arguments=args,
                        )
                        if version is not None:
                            body["expected_version"] = version
                        r = await http.post("/api/v1/commands", json=body, headers=headers)
                        if r.status_code != 200:
                            raise ValueError("COMMAND_" + str(r.status_code))
                        return r.json()

                    async def view(session: str) -> Any:
                        r = await http.get(
                            "/api/v1/work-session-transcript/" + session, params=query
                        )
                        assert r.status_code == 200
                        return r.json()

                    async def reply(session: str, timeout: float) -> Any:
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            v = await view(session)
                            if v.get("pending_reply") and v["pending_reply"]["acknowledgeable"]:
                                return v
                            if v.get("progress") and v["progress"]["state"] in (
                                "failed",
                                "unknown",
                                "cancelled",
                            ):
                                report["phases"]["turn_state"] = v["progress"]["state"]
                                save()
                                raise ValueError("TURN_" + v["progress"]["state"].upper())
                            await asyncio.sleep(0.5)
                        raise ValueError("REPLY_TIMEOUT")

                    async def ack(v: Any) -> None:
                        d = v["pending_reply"]
                        from ai_agent_contracts.common import Scope

                        await post(
                            "dialogue.acknowledge",
                            d["utterance_id"],
                            {"revision": d["revision"]},
                            d["version"],
                            Scope.model_validate(d["scope"]),
                        )

                    try:
                        plan = await post(
                            "workplace.access.preview",
                            "initial-access",
                            dict(
                                schema_id="workplace.access-args",
                                data={
                                    "duration_seconds": None,
                                    "constraints": "Synthetic lease probe; no Internet browsing.",
                                },
                            ),
                            1,
                        )
                        await post(
                            "workplace.access.execute",
                            "initial-access",
                            {"plan_id": plan["resource_id"], "plan_version": 1},
                            1,
                        )
                        report["phases"]["profile_granted"] = True
                        save()
                        opened = await post(
                            "work.session.open",
                            "work",
                            dict(
                                schema_id="work.session.open-args",
                                data={"title": "Synthetic live lease check"},
                            ),
                        )
                        session = opened["resource_id"]
                        await post(
                            "work.session.message",
                            session,
                            dict(
                                schema_id="work.session.message-args",
                                data={"session_id": session, "text": PROMPT},
                            ),
                            1,
                        )
                        run = rt.supervisor.snapshot
                        await post(
                            "agent.start",
                            "primary",
                            {"autonomy": "autonomous"},
                            run.version,
                            run.scope,
                        )
                        started = time.monotonic()
                        v = await reply(session, 600)
                        report["phases"]["reply_ready"] = True
                        report["phases"]["duration_seconds"] = round(time.monotonic() - started, 3)
                        report["phases"]["tool_steps"] = len(
                            (v.get("progress") or {}).get("steps", [])
                        )
                        files = HomeFiles(a.image_id)
                        content = await files.read(volume(scope), "share/lease-probe.done")
                        data = base64.b64decode(content.get("content", ""))
                        report["phases"]["long_command_marker"] = (
                            content.get("status") == "ok" and data == b"lease_probe_ok"
                        )
                        report["success"] = (
                            report["phases"]["long_command_marker"]
                            and report["phases"]["duration_seconds"] >= 240
                        )
                        await ack(v)
                        save()
                    finally:
                        run = rt.supervisor.snapshot
                        stopped = await http.post(
                            "/api/v1/agents/" + run.agent_id + "/stop",
                            json=dict(
                                type="agent.stop",
                                request_id="probe-stop-" + uuid4().hex,
                                target=run.agent_id,
                                scope=run.scope.model_dump(mode="json"),
                            ),
                            headers=headers,
                        )
                        report["phases"]["stop_accepted"] = stopped.status_code == 200
                        report["calls"] = wire.calls
                        report["http_headers"] = wire.headers
                        save()
        finally:
            close()
            setattr(providers, PROVIDER, original)
            snapshot = rt.supervisor.snapshot
            finalize(report, snapshot.state.value, len(snapshot.unconfirmed_resource_ids))
            save()
    report["calls"] = wire.calls
    save()
    print(json.dumps(report))
    return 0 if report["success"] else 2


def finalize(report: dict[str, Any], state: str, pending: int) -> None:
    report["turn_success"] = bool(report["success"])
    report["phases"]["final_state"] = state
    report["phases"]["unconfirmed_resource_count"] = pending
    report["success"] = report["turn_success"] and state == "STOPPED" and pending == 0


class Diagnostics(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith(
            (
                "Slow job lease renewal",
                "Slow runtime tick step",
                "Job admission lost",
                "Resource lease cancelled",
                "Run faulted",
                "Event loop blocked",
                "Runtime tick failed",
            )
        ):
            print("LIVE_LOG " + message[:500], flush=True)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--i-understand-this-makes-paid-api-calls", action="store_true", required=True)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--image-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--max-calls", type=int, default=3, choices=(1, 2, 3))
    p.add_argument("--stream-tools", choices=("on", "off"), required=True)
    p.add_argument("--timeout-seconds", type=float, default=300)
    p.add_argument("--keepalive-seconds", type=int, default=120)
    a = p.parse_args()
    a.stream_tools = a.stream_tools == "on"
    if not a.env_file.is_absolute():
        p.error("absolute env file required")
    logger = logging.getLogger()
    logger.handlers[:] = [Diagnostics()]
    logger.setLevel(logging.WARNING)
    try:
        return asyncio.run(run(a))
    except Exception as e:
        # No traceback: model transport may carry credentials; report only a fixed type/code.
        print(
            json.dumps(
                {
                    "probe_error_type": type(e).__name__,
                    "code": getattr(e, "code", getattr(getattr(e, "error", None), "code", None)),
                    "frames": [
                        (Path(f.filename).name, f.name, f.lineno)
                        for f in traceback.extract_tb(e.__traceback__)
                    ],
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
