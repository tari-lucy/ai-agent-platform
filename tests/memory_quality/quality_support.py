"""A production-built local host with the public profile and a synthetic, scripted model.

The "model" is an httpx MockTransport behind the real live adapter and the real private wire
protocol, so the request bodies are exactly what a provider would be sent. It answers by role
from queues the test prepares; an unexpected call or an empty queue fails the test loudly.
Scripted answers prove the route and the application rules, never the quality of free text.
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
from ai_agent_adapters.llm import OpenAICompatibleProvider
from ai_agent_application.storage import Owner
from ai_agent_contracts.control import Utterance
from ai_agent_contracts.memory import Source
from ai_agent_contracts.states import OperationState
from test_http import finish_job, post, scope_params, start
from test_live import answer, binding

from ai_agent_core.local_host import provider as providers
from ai_agent_core.local_host.init import initialise, work_scope
from ai_agent_core.local_host.layout import LiveConfig, load_config, paths
from ai_agent_core.local_host.memory import enable_memory
from ai_agent_core.local_host.serve import build
from ai_agent_core.m3_composition.state import PRINCIPALS

TERMINAL = (
    OperationState.SUCCEEDED,
    OperationState.FAILED,
    OperationState.CANCELLED,
    OperationState.OUTCOME_UNKNOWN,
)
PUBLIC_PROFILE = Path(__file__).resolve().parents[2] / "profiles/examples/opechatka/profile.md"
ORIGIN = "http://127.0.0.1:8765"
MARKERS = (
    ("Ты извлекаешь проверяемые утверждения", "extractor"),
    ("Кратко перескажи разговор", "consolidator"),
    ("осмыслить пережитое", "reflection"),
)
TURN = re.compile(r'<turn role="(person|character)" by="[^"]*">(.*?)</turn>', re.S)
MEMORY = re.compile(
    r'<memory by="([^"]+)" at="([^"]+)"( disputed="true")?( partial="true")?>(.*?)</memory>', re.S
)


class Model:
    """Synthetic provider. `calls` keeps (role, request body) for later inspection."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.queues: dict[str, list[dict]] = {
            role: [] for role in ("character", "extractor", "consolidator", "reflection")
        }

    @staticmethod
    def role(body: dict) -> str:
        system = body["messages"][0]["content"]
        return next((role for marker, role in MARKERS if marker in system), "character")

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "tools" not in body and body["model"] == "fixture-model"
        role = self.role(body)
        self.calls.append((role, body))
        assert self.queues[role], f"unscripted {role} call"
        payload = self.queues[role].pop(0)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            content=answer(json.dumps(payload, ensure_ascii=False)),
        )

    def count(self, role: str | None = None) -> int:
        return sum(1 for r, _ in self.calls if role in (None, r))

    def last(self, role: str) -> dict:
        return next(body for r, body in reversed(self.calls) if r == role)


class Host:
    """One running app on a prepared root; `Host.open` can be repeated for a restart."""

    def __init__(self, root: Path, tmp_path: Path, model: Model, monkeypatch) -> None:
        self.root, self.tmp_path, self.model, self.monkeypatch = root, tmp_path, model, monkeypatch
        self.config = load_config(paths(root))
        self.scope = work_scope(
            self.config.workspace_id, self.config.persona_id, self.config.work_space_id
        )
        self.env: SimpleNamespace | None = None

    @property
    def runtime(self):
        assert self.env is not None
        return self.env.runtime

    @asynccontextmanager
    async def open(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.model)) as wire:
            self.monkeypatch.setattr(
                providers,
                "OpenAICompatibleProvider",
                lambda settings: OpenAICompatibleProvider(settings, wire),
            )
            app, close = build(
                paths(self.root),
                load_config(paths(self.root)),
                provider="live",
                env_file=binding(self.tmp_path / "synthetic.env"),
            )
            try:
                async with app.router.lifespan_context(app):
                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
                    ) as client:
                        login = await client.post(
                            "/api/v1/auth/login",
                            json={"password": paths(self.root).password.read_text().strip()},
                            headers={"Origin": ORIGIN},
                        )
                        assert login.status_code == 200
                        self.env = SimpleNamespace(
                            runtime=app.state.runtime,
                            client=client,
                            headers={"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]},
                        )
                        yield self
            finally:
                close()
                self.env = None

    async def start(self):
        assert self.env is not None
        return await start(self.env)

    async def say(self, request_id: str, text: str, reply: str, **decision) -> Utterance:
        """A person message, the scripted character reply, and the person's confirmation."""
        assert self.env is not None
        self.model.queues["character"].append(dict(reply=reply, **decision))
        run = self.runtime.supervisor.snapshot
        made = await post(
            self.env,
            dict(
                type="dialogue.message",
                request_id=request_id,
                target=run.session_id,
                scope=run.scope.model_dump(mode="json"),
                arguments={"text": text},
            ),
        )
        assert made.status_code == 200, made.text
        await finish_job(self.env, made.json()["operation_id"])
        rows = await self.runtime.store.repository(
            Utterance, PRINCIPALS[Owner.GATEWAY], run.scope
        ).list(limit=200)
        row = next(u for u in rows if u.operation_id == made.json()["operation_id"])
        ack = await post(
            self.env,
            dict(
                type="dialogue.acknowledge",
                request_id="ack-" + request_id,
                target=row.id,
                scope=run.scope.model_dump(mode="json"),
                expected_version=row.version,
                arguments={"revision": row.revision},
            ),
        )
        assert ack.status_code == 200, ack.text
        return row

    async def get(self, resource: str) -> list[dict]:
        assert self.env is not None
        found = await self.env.client.get("/api/v1/" + resource, params=scope_params(self.scope))
        assert found.status_code == 200, found.text
        return found.json()["items"]

    async def sources(self) -> dict[str, str]:
        """Source id by content, read through the memory owner."""
        rows = await self.runtime.store.repository(
            Source, PRINCIPALS[Owner.MEMORY], self.scope
        ).list(limit=200)
        return {s.content: s.id for s in rows if s.content}

    async def process(self, request_id: str, episode_ids: list[str]):
        assert self.env is not None
        run = self.runtime.supervisor.snapshot
        return await post(
            self.env,
            dict(
                type="session.process-memory",
                request_id=request_id,
                target=run.session_id,
                scope=run.scope.model_dump(mode="json"),
                expected_version=run.version,
                arguments={"episode_ids": episode_ids},
            ),
        )

    async def finish(self, operation_id: str, *, succeeded: bool = True):
        """Wait for the lifespan executor; on a timeout say where the job and the model stand."""
        assert self.env is not None
        scope = self.runtime.supervisor.snapshot.scope
        for _ in range(1200):
            stored = await self.runtime.jobs.store.load(operation_id, scope)
            assert stored is not None
            if stored[0].status in TERMINAL:
                if succeeded:
                    assert stored[0].status == OperationState.SUCCEEDED, stored[0].status
                return stored[0]
            await asyncio.sleep(0.05)
        calls = {r: sum(1 for x, _ in self.model.calls if x == r) for r in self.model.queues}
        raise AssertionError(f"JOB_DID_NOT_FINISH status={stored[0].status} calls={calls}")


async def prepared_host(tmp_path: Path, model: Model, monkeypatch) -> Host:
    root = tmp_path / "host"
    await initialise(
        root,
        profile_markdown=PUBLIC_PROFILE.read_text(encoding="utf-8"),
        operator_controls=True,
        live=LiveConfig(model_id="fixture-model", attempt_limit=300, attempt_period="utc_day"),
    )
    await enable_memory(root)
    return Host(root, tmp_path, model, monkeypatch)


def view(body: dict) -> dict:
    """What one character request showed the model, in counts and short labels."""
    user = body["messages"][-1]["content"]
    return dict(
        system_chars=len(body["messages"][0]["content"]),
        turns=[(role, text) for role, text in TURN.findall(user)],
        memory=[
            dict(by=by, at=at, disputed=bool(disputed), partial=bool(partial), text=text)
            for by, at, disputed, partial, text in MEMORY.findall(user)
        ],
        total_chars=sum(len(m["content"]) for m in body["messages"]),
    )
