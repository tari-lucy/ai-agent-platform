"""Opt-in live workspace-v2 probe on a NEW host and public profile, hard HTTP call cap.

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
from datetime import UTC, datetime
from pathlib import Path
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
PROMPT = """Это вымышленная приёмочная задача, не подключение к настоящему аккаунту.
Самостоятельно осмотрись в /guide, в том числе tools.md. Работай в /workspace через bash.
Создай короткую заметку README о результате, при необходимости навык в skills/.
Посети https://example.com через Python Playwright с установленным Chromium по описанию
среды и сохрани заголовок страницы в share/windows-browser.txt. Никаких других сайтов.
Для процесса Python/Chromium задай XDG_CONFIG_HOME=/work/browser-config: каталог
настроек самого OpenCode read-only. В chromium.launch передай executable_path
'/usr/bin/chromium', args=['--no-sandbox', '--disable-dev-shm-usage'] и
proxy={'server': os.environ['HTTP_PROXY']}. Используй Python heredoc, а не сложные кавычки.
Затем СНАЧАЛА поставь себе wake_me на 3 минуты с when_done=windows-live-check и причиной
проверить результат фоновой работы. ПОСЛЕ постановки запусти фоновую команду: через 20
секунд записать файл /workspace/.platform/done/windows-live-check. Не жди её внутри хода.
При пробуждении проверь файл, запиши короткий итог в share/windows-followup.txt и ответь.
Новые разрешения не нужны: профиль рабочего места выдан. Секреты и реальные сервисы
не трогай, чужие файлы не удаляй. Не утверждай, что запустил инструмент, без результата.
"""


class Counted(httpx.AsyncBaseTransport):
    def __init__(self, limit):
        self.limit = limit
        self.calls = 0
        self.inner = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        if self.calls >= self.limit:
            raise httpx.TransportError("PROBE_CALL_LIMIT")
        self.calls += 1
        return await self.inner.handle_async_request(request)

    async def aclose(self):
        await self.inner.aclose()


async def run(a):
    if not a.workdir.is_absolute() or a.workdir.exists():
        raise ValueError("NEW_ABSOLUTE_WORKDIR_REQUIRED")
    private_dir(a.workdir)
    root = a.workdir / "host"
    report = {
        "paid_probe": True,
        "max_calls": a.max_calls,
        "calls": 0,
        "phases": {},
        "success": False,
        "browser_config_hint": True,
    }

    def save():
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
            max_prompt_chars=40000,
            timeout_seconds=90.0,
        ),
    )
    await enable_tickets(root)
    await configure_activity(root, enabled=True, daily_calls=20, own_goals=False)
    await enable_recovery(
        root, store_dir=a.workdir / "backups", ledger_path=a.workdir / "ledger/events.sqlite"
    )
    await enable_workplace_files(root, ceiling_days=365)
    await enable_work_sessions(
        root,
        image_id=a.image_id,
        areas=["inbox", "projects", "knowledge", "skills"],
        output_area="projects",
        ceiling_days=365,
        home_enabled=True,
        resident_enabled=True,
        egress_enabled=True,
        wake_enabled=True,
        wall_seconds=360,
        cpu_seconds=120,
        memory_mb=2048,
        pids=256,
        disk_mb=512,
        max_model_calls=a.max_calls,
        max_model_tokens=1024,
        egress_daily_mb=5,
        cpu_daily_minutes=1,
        resident_idle_seconds=600,
    )
    cfg = load_config(paths(root))
    scope = work_scope(cfg.workspace_id, cfg.persona_id, cfg.work_space_id)
    query = {
        k: getattr(scope, k)
        for k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
    }
    wire = Counted(a.max_calls)
    original = providers.OpenAICompatibleProvider
    async with httpx.AsyncClient(
        transport=wire, timeout=httpx.Timeout(90, connect=10), follow_redirects=False
    ) as client:
        providers.OpenAICompatibleProvider = lambda settings: OpenAICompatibleProvider(
            settings, client
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

                    async def post(kind, target, args, version=None, chosen=None):
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

                    async def view(session):
                        r = await http.get(
                            "/api/v1/work-session-transcript/" + session, params=query
                        )
                        assert r.status_code == 200
                        return r.json()

                    async def reply(session, timeout):
                        deadline = time.monotonic() + timeout
                        while time.monotonic() < deadline:
                            v = await view(session)
                            if v.get("pending_reply") and v["pending_reply"]["acknowledgeable"]:
                                return v
                            if v.get("progress") and v["progress"]["state"] in (
                                "failed",
                                "unknown",
                            ):
                                report["phases"]["turn_state"] = v["progress"]["state"]
                                save()
                                raise ValueError("TURN_" + v["progress"]["state"].upper())
                            await asyncio.sleep(0.5)
                        raise ValueError("REPLY_TIMEOUT")

                    async def ack(v):
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
                                    "duration_seconds": 86400,
                                    "constraints": "Synthetic live acceptance; example.com only.",
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
                                data={"title": "Synthetic Windows live acceptance"},
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
                        v = await reply(session, 360)
                        report["phases"]["first_reply"] = True
                        report["phases"]["tool_steps"] = len(
                            (v.get("progress") or {}).get("steps", [])
                        )
                        await ack(v)
                        save()
                        deadline = time.monotonic() + 300
                        diary = None
                        while time.monotonic() < deadline:
                            rows = (
                                await http.get(
                                    "/api/v1/work-sessions", params={**query, "limit": 200}
                                )
                            ).json()["items"]
                            diary = next(
                                (
                                    s
                                    for s in rows
                                    if s["title"] == "Самостоятельная работа"
                                    and s["last_sequence"] > 0
                                ),
                                None,
                            )
                            if diary:
                                break
                            await asyncio.sleep(1)
                        report["phases"]["wake_observed"] = diary is not None
                        save()
                        if diary:
                            w = await reply(diary["id"], 180)
                            await ack(w)
                            report["phases"]["wake_answered"] = True
                        files = HomeFiles(a.image_id)
                        for name, key in [
                            ("share/windows-browser.txt", "browser_file"),
                            ("share/windows-followup.txt", "followup_file"),
                        ]:
                            content = await files.read(volume(scope), name)
                            data = base64.b64decode(content.get("content", ""))
                            report["phases"][key] = content.get("status") == "ok" and (
                                b"Example Domain" in data if key == "browser_file" else bool(data)
                            )
                        day = datetime.now(UTC).strftime("%Y-%m-%d")
                        traffic = (await http.get("/api/v1/egress-log/" + day, params=query)).json()
                        report["phases"]["egress_requests"] = traffic.get("requests", 0)
                        report["phases"]["cpu_log"] = bool(
                            list((root / "data/usage").glob("*.jsonl"))
                        )
                        report["success"] = all(
                            report["phases"].get(k)
                            for k in (
                                "first_reply",
                                "wake_observed",
                                "wake_answered",
                                "browser_file",
                                "followup_file",
                                "cpu_log",
                            )
                        )
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
                        save()
        finally:
            close()
            providers.OpenAICompatibleProvider = original
    report["calls"] = wire.calls
    save()
    print(json.dumps(report))
    return 0 if report["success"] else 2


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--i-understand-this-makes-paid-api-calls", action="store_true", required=True)
    p.add_argument("--workdir", type=Path, required=True)
    p.add_argument("--env-file", type=Path, required=True)
    p.add_argument("--image-id", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--max-calls", type=int, default=8, choices=range(1, 11))
    a = p.parse_args()
    if not a.env_file.is_absolute():
        p.error("absolute env file required")
    logging.basicConfig(level=logging.WARNING)
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
