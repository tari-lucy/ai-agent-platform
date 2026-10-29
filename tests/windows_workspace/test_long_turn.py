"""A personal-like work turn longer than 4 minutes on real Docker/OpenCode (opt-in).

Home + resident sandbox + internet, the whole profile granted until revoked, and a scripted
tool-calling model that runs ``sleep`` in bash LONG_STEPS times. Passes on Linux (6 min, 15
calls); used to reproduce the Windows JOB_LEASE_EXPIRED / STALE_CONTEXT cancellations.
Needs AW_HOME_IMAGE_ID (and AW_EGRESS_LIVE=1); synthetic model, no paid calls.
"""

import asyncio
import json
import logging
import os
import time

import pytest
from test_activity import WorkModel, fast, prepared, until
from test_http import post
from test_initial_access import execute_command, preview_command
from test_real_scripts_launcher import ArchiveModel
from test_session_launcher import AREAS
from test_work_sessions_http import open_session, say, transcript

from ai_agent_core.local_host.recovery import enable_recovery
from ai_agent_core.local_host.work_sessions import enable_work_sessions
from ai_agent_core.local_host.workplace_files import enable_workplace_files

pytestmark = pytest.mark.skipif(
    not (os.getenv("AW_HOME_IMAGE_ID") and os.getenv("AW_EGRESS_LIVE")),
    reason="explicit workspace image and AW_EGRESS_LIVE=1 required",
)

STEPS = int(os.environ.get("LONG_STEPS", "14"))
SLEEP = int(os.environ.get("LONG_SLEEP", "20"))


class LongModel(WorkModel):
    async def __call__(self, request):
        body = json.loads(request.content)
        self.calls.append(("activity", None))
        n = len(self.calls)
        if n <= STEPS:
            call = dict(
                id=f"step-{n}",
                type="function",
                function=dict(
                    name="bash",
                    arguments=json.dumps(
                        {"command": f"sleep {SLEEP}; echo step-{n}", "description": f"step {n}"}
                    ),
                ),
            )
            return ArchiveModel.response(
                body, dict(role="assistant", content=None, tool_calls=[call]), "tool_calls"
            )
        return ArchiveModel.response(
            body, dict(role="assistant", content="Долгая синтетическая работа завершена."), "stop"
        )


def test_long_turn(tmp_path, monkeypatch, caplog):
    async def scenario():
        model = LongModel()
        host = await prepared(tmp_path, monkeypatch, model)
        await enable_recovery(
            host.root, store_dir=tmp_path / "b", ledger_path=tmp_path / "l/e.sqlite"
        )
        await enable_workplace_files(host.root, ceiling_days=0)
        await enable_work_sessions(
            host.root,
            image_id=os.environ["AW_HOME_IMAGE_ID"],
            areas=AREAS,
            output_area="projects",
            ceiling_days=0,
            home_enabled=True,
            resident_enabled=True,
            egress_enabled=True,
        )
        async with host.open():
            rt, _scope, env = host.runtime, host.scope, host.env
            fast(rt)
            planned = await post(env, preview_command(host, "profile", None, "Рабочее место."))
            assert planned.status_code == 200, planned.text
            done = await post(env, execute_command(host, "go", planned.json()["resource_id"]))
            assert done.status_code == 200, done.text
            session = (await open_session(host, "open", "Разговор")).json()["resource_id"]
            assert (await say(host, "say", session, 1, "Сделай долгую работу.")).status_code == 200
            t0 = time.monotonic()
            await host.start()
            try:
                view = await until(
                    lambda: transcript(host, session),
                    lambda v: (
                        (v.get("pending_reply") or {}).get("acknowledgeable")
                        or (v.get("progress") or {}).get("state")
                        in ("failed", "unknown", "cancelled")
                    ),
                    "draft or failure",
                    seconds=900,
                )
            finally:
                print(
                    "LONG result after",
                    round(time.monotonic() - t0, 1),
                    "s; calls",
                    model.count(),
                    "state",
                    rt.supervisor.snapshot.state.value,
                )
            print(
                "LONG progress",
                (view.get("progress") or {}).get("state"),
                (view.get("progress") or {}).get("error_code"),
            )

    with caplog.at_level(logging.WARNING):
        try:
            asyncio.run(scenario())
        finally:
            for r in caplog.records:
                if "timing" not in r.getMessage():
                    print("LOG", r.name, r.getMessage()[:250])
